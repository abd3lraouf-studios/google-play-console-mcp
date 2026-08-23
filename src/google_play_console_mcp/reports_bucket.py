"""Reading Play's bulk reports out of the private Cloud Storage bucket.

Play Console writes monthly CSVs to a bucket named ``pubsite_prod_rev_<id>`` (or
``pubsite_prod_<accountId>``) that only the developer account can read. This
module owns finding, listing and downloading those objects;
``google_play_console_mcp/reports.py`` owns parsing them and does no I/O at all.

Two things are configured separately from everything else in this server:

``GOOGLE_PLAY_MCP_REPORTS_BUCKET``
    The bucket id. There is no API that discovers it — you copy it out of Play
    Console.
The ``devstorage.read_only`` scope
    A third OAuth scope, distinct from ``androidpublisher`` and
    ``playdeveloperreporting``. The account permission that grants it is
    "View app information and download bulk reports", which is an **account**
    level permission: app-level grants do not reach the bucket.

Objects are cached on disk under the guard state directory, because a month's
CSV is immutable once published and re-downloading a 40 MB earnings file on
every question is wasteful.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from google.cloud import storage  # type: ignore[attr-defined]
from google.cloud.exceptions import Forbidden, NotFound

from google_play_console_mcp.credentials import STORAGE_SCOPES, load_service_account_credentials
from google_play_console_mcp.errors import PlayConsoleError, ValidationError
from google_play_console_mcp.guards import ensure_private_dir, ensure_private_file, state_dir
from google_play_console_mcp.models import Report
from google_play_console_mcp.reports import (
    ACCOUNT_LEVEL_KINDS,
    DOCS_URL,
    FAMILIES,
    ReportKind,
    normalize_month,
    parse_earnings,
    parse_stats_report,
    report_freshness,
    report_object_prefix,
    summarize,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)

#: Cap on how many objects are merged for one month. Google normally writes one
#: per month, occasionally a couple; more than this means a wrong prefix rather
#: than real data, and downloading them all would be slow.
MAX_OBJECTS_PER_MONTH = 12

#: A month older than this is immutable — cache it forever. A recent month is
#: still filling in, so its cache entry expires.
CACHE_TTL_RECENT = timedelta(hours=6)


class ReportsBucketError(PlayConsoleError):
    """The reports bucket could not be reached or read."""

    kind = "reports_bucket_error"


_BUCKET_ENV = "GOOGLE_PLAY_MCP_REPORTS_BUCKET"

_SETUP_REMEDY = (
    "In Play Console open Download reports -> Statistics (or Financial reports) and "
    "click 'Copy Cloud Storage URI'. It looks like gs://pubsite_prod_rev_0123456789. "
    f"Set {_BUCKET_ENV} to it (the gs:// prefix is optional). Then, in Play Console -> "
    "Users and permissions, give the service account the ACCOUNT-level permission "
    "'View app information and download bulk reports' — app-level grants do not reach "
    "the bucket."
)


def bucket_name(explicit: str | None = None) -> str:
    """Resolve the reports bucket id.

    Raises:
        ValidationError: when no bucket is configured, with the steps to find it.
    """
    raw = explicit or os.environ.get(_BUCKET_ENV, "")
    cleaned = raw.strip().removeprefix("gs://").strip("/")
    if not cleaned:
        raise ValidationError(
            "No Play reports bucket is configured, so installs, ratings, earnings and "
            "search terms cannot be read — no Play REST API exposes them.",
            remedy=_SETUP_REMEDY,
            doc_url=DOCS_URL,
        )
    return cleaned


def _cache_dir() -> Path:
    path = state_dir() / "reports-cache"
    ensure_private_dir(path)
    return path


def _cache_path(bucket: str, object_path: str) -> Path:
    digest = hashlib.sha256(f"{bucket}/{object_path}".encode()).hexdigest()[:32]
    return _cache_dir() / f"{digest}.csv"


def _month_is_settled(month: str | date | None) -> bool:
    """True when a month is old enough that its report will not change again."""
    if month is None:
        return False
    ym = normalize_month(month)
    year, mon = int(ym[:4]), int(ym[4:6])
    # Settled once the following month is fully past plus the publication lag.
    settle = date(year + (mon == 12), (mon % 12) + 1, 1) + timedelta(days=10)
    return datetime.now(UTC).date() > settle


def _classify(exc: Exception, bucket: str, object_path: str) -> PlayConsoleError:
    """Turn a storage exception into an error that says what to do."""
    if isinstance(exc, Forbidden):
        return ReportsBucketError(
            f"Access denied reading gs://{bucket}/{object_path}.",
            remedy=(
                "The service account can authenticate but is not permitted to read the "
                "reports bucket. In Play Console -> Users and permissions, grant it the "
                "ACCOUNT-level permission 'View app information and download bulk "
                "reports'. App-level grants do not reach the bucket, and permission "
                "changes can take up to 24 hours to propagate."
            ),
            doc_url=DOCS_URL,
        )
    if isinstance(exc, NotFound):
        return ReportsBucketError(
            f"No object at gs://{bucket}/{object_path}.",
            remedy=(
                "Either the bucket id is wrong, or Play has not published this report "
                "yet. Check the month is in the past and that the package name matches "
                "the app exactly, then run list_report_objects to see what is there."
            ),
            doc_url=DOCS_URL,
        )
    return ReportsBucketError(
        f"Could not read gs://{bucket}/{object_path}: {type(exc).__name__}: {exc}",
        remedy="Check network access to storage.googleapis.com and the bucket id.",
    )


class ReportsBucketMixin:
    """Cloud Storage bulk-report operations.

    Mixed into :class:`google_play_console_mcp.client.PlayStoreClient` so the same credentials
    resolve for a third scope. Relies on ``_credentials_json``,
    ``_credentials_path`` and ``_logger`` from the host class.
    """

    _credentials_json: str | dict[str, Any] | None
    _credentials_path: str | None
    _logger: Any

    _storage_client: Any = None
    _reports_bucket: str | None = None

    # =========================================================================
    # Client
    # =========================================================================

    def _get_storage_client(self) -> Any:
        """Get or create the Cloud Storage client, scoped read-only."""
        if self._storage_client is not None:
            return self._storage_client

        credentials = load_service_account_credentials(
            self._credentials_json, self._credentials_path, STORAGE_SCOPES
        )
        if not credentials:
            raise ReportsBucketError(
                "No valid credentials found for the reports bucket.",
                remedy=(
                    "Set GOOGLE_APPLICATION_CREDENTIALS (path) or "
                    "GOOGLE_PLAY_STORE_CREDENTIALS (JSON or path)."
                ),
            )
        self._storage_client = storage.Client(
            credentials=credentials, project=getattr(credentials, "project_id", None)
        )
        self._logger.info("Cloud Storage client initialized for Play reports")
        return self._storage_client

    def reports_bucket(self) -> str:
        """The configured reports bucket id."""
        return bucket_name(self._reports_bucket)

    # =========================================================================
    # Objects
    # =========================================================================

    def list_report_objects(self, prefix: str, *, max_results: int = 100) -> list[str]:
        """List object names under ``prefix`` in the reports bucket."""
        bucket = self.reports_bucket()
        client = self._get_storage_client()
        try:
            blobs = client.list_blobs(bucket, prefix=prefix, max_results=max_results)
            return [blob.name for blob in blobs]
        except Exception as exc:
            raise _classify(exc, bucket, prefix) from exc

    def fetch_report_object(self, object_path: str, *, month: str | date | None = None) -> bytes:
        """Download one report object, using the on-disk cache when it is settled."""
        bucket = self.reports_bucket()
        cache = _cache_path(bucket, object_path)
        settled = _month_is_settled(month)

        if cache.exists():
            age = datetime.now(UTC) - datetime.fromtimestamp(cache.stat().st_mtime, UTC)
            if settled or age < CACHE_TTL_RECENT:
                return cache.read_bytes()

        client = self._get_storage_client()
        try:
            blob = client.bucket(bucket).blob(object_path)
            data: bytes = blob.download_as_bytes()
        except Exception as exc:
            raise _classify(exc, bucket, object_path) from exc

        try:
            cache.write_bytes(data)
            ensure_private_file(cache)
        except OSError:
            pass  # the cache is an optimisation, never a requirement
        return data

    def _resolve_objects(
        self,
        kind: ReportKind,
        month: str | date,
        *,
        package_name: str | None,
        dimension: str,
    ) -> list[str]:
        """Find the object(s) holding one month of a report.

        Listing rather than constructing an exact name: earnings objects carry a
        trailing account id that is not derivable, and Google has changed the
        dimension suffix on stats objects before.
        """
        prefix = report_object_prefix(kind, month, package_name=package_name)
        names = self.list_report_objects(prefix, max_results=MAX_OBJECTS_PER_MONTH * 4)

        if kind not in ACCOUNT_LEVEL_KINDS and dimension:
            # stats objects end _<dimension>.csv; keep only the requested slice.
            suffix = f"_{dimension}.csv"
            sliced = [n for n in names if n.endswith(suffix)]
            if sliced:
                names = sliced

        if kind is ReportKind.STORE_CONVERSION:
            # store_conversion and store_performance share a directory, and the
            # 'total_' sibling would otherwise match this prefix too.
            names = [n for n in names if "/total_store_performance_" not in n]

        return names[:MAX_OBJECTS_PER_MONTH]

    # =========================================================================
    # Reports
    # =========================================================================

    def get_stats_report(
        self,
        kind: ReportKind | str,
        package_name: str,
        month: str | date,
        dimension: str | None = None,
    ) -> Report:
        """Fetch and parse one month of a per-app stats report.

        Args:
            kind: Report family key (installs, ratings, crashes, ...).
            package_name: App package name.
            month: Month as YYYY-MM.
            dimension: Slice to read; defaults to the family's default.

        Returns:
            The parsed report, always carrying freshness.
        """
        kind = ReportKind(kind)
        family = FAMILIES[kind]
        slice_name = dimension or family.default_dimension or "overview"

        if family.dimensions and slice_name not in family.dimensions:
            raise ValidationError(
                f"'{slice_name}' is not a dimension of the {kind.value} report.",
                remedy=f"Use one of: {', '.join(family.dimensions)}.",
            )

        objects = self._resolve_objects(
            kind, month, package_name=package_name, dimension=slice_name
        )
        if not objects:
            # No object is not an error: it usually means the month has not been
            # published. Return an empty report whose freshness explains that,
            # rather than a bare zero that reads as a measurement.
            return Report(
                rows=[],
                freshness=report_freshness(kind, month, row_count=0),
                source_object=report_object_prefix(kind, month, package_name=package_name),
                totals={},
            )

        merged: Report | None = None
        for name in objects:
            data = self.fetch_report_object(name, month=month)
            parsed = parse_stats_report(
                data,
                kind=kind,
                package_name=package_name,
                month=month,
                dimension=slice_name,
                source_object=name,
            )
            if merged is None:
                merged = parsed
            else:
                merged.rows.extend(parsed.rows)
        assert merged is not None

        merged.totals = summarize(merged.rows)
        return merged

    def get_earnings(
        self,
        month: str | date,
        package_name: str | None = None,
    ) -> Report:
        """Fetch and parse a month of account-wide earnings.

        Args:
            month: Month as YYYY-MM.
            package_name: Filter to one app; omit for the whole account.

        Returns:
            The parsed report. Amounts are in merchant currency and the currency
            is carried per row — never sum across currencies.
        """
        objects = self._resolve_objects(ReportKind.EARNINGS, month, package_name=None, dimension="")
        if not objects:
            return Report(
                rows=[],
                freshness=report_freshness(ReportKind.EARNINGS, month, row_count=0),
                source_object=report_object_prefix(ReportKind.EARNINGS, month),
                totals={},
            )

        merged: Report | None = None
        for name in objects:
            data = self.fetch_report_object(name, month=month)
            parsed = parse_earnings(
                data, month=month, package_name=package_name, source_object=name
            )
            if merged is None:
                merged = parsed
            else:
                merged.rows.extend(parsed.rows)
        assert merged is not None

        merged.totals = summarize(merged.rows)
        return merged

    def check_reports_access(self) -> dict[str, Any]:
        """Verify the bucket is configured and readable. Run this first.

        Returns:
            A diagnosis, never an exception: the point is to report what is
            wrong, not to raise it.
        """
        try:
            bucket = self.reports_bucket()
        except ValidationError as exc:
            return {"ok": False, **exc.to_dict()}

        try:
            names = self.list_report_objects("stats/", max_results=5)
        except PlayConsoleError as exc:
            return {"ok": False, "bucket": bucket, **exc.to_dict()}

        prefixes = sorted({re.sub(r"^(stats/[^/]+/).*", r"\1", n) for n in names})
        return {
            "ok": True,
            "bucket": bucket,
            "sample_objects": names,
            "prefixes_seen": prefixes,
        }
