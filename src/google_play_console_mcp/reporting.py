"""Play Developer Reporting API v1beta1 — app vitals, errors and anomalies.

This is a second Google API, distinct from the Android Publisher API the rest of
this package speaks. It answers the question publishing cannot: *is the release
we just shipped actually healthy?* Without it a server can push to production
and have nothing to say about the crash rate afterwards.

The API is organised around **metric sets** — normalized singleton resources,
one per class of vital. Each supports two operations:

``get``
    Metadata: which aggregation periods exist and how fresh each one is.
``query``
    A time series, sliced by dimensions and filtered.

Two design choices differ deliberately from a thin transport wrapper:

1. **Callers pass a package name, not a resource name.** The API wants
   ``apps/com.example/crashRateMetricSet``; an agent should not have to know
   that, nor guess the camel-case spelling of ``stuckBackgroundWakelockRate``.
   :data:`METRIC_SETS` owns the mapping and every public method takes
   ``package_name`` plus a short key like ``crash_rate``.

2. **Freshness is surfaced, not buried.** Play vitals lag reality by hours to
   days. A query for "the last 7 days" that silently returns 5 is the most
   likely way to draw a wrong conclusion from this API, so
   :meth:`ReportingMixin.get_metric_set_freshness` exists as a first-class call
   and every query result can be checked against it.

Access is granted separately from publishing access in Play Console. A service
account that deploys fine can still get 403 here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from googleapiclient.discovery import build

from google_play_console_mcp.credentials import REPORTING_SCOPES, load_service_account_credentials
from google_play_console_mcp.models import (
    Anomaly,
    ErrorIssue,
    ErrorReport,
    MetricQueryResult,
    MetricRow,
    MetricSetFreshness,
    MetricSetInfo,
    ReleaseFilterOptions,
    ReportingApp,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger(__name__)


class ReportingClientError(Exception):
    """Raised when the Reporting API cannot be reached or used."""


# Short key -> (resource suffix, googleapiclient accessor path, human label).
#
# The accessor path is a tuple of method names walked from the service root:
# ("vitals", "crashrate") means service.vitals().crashrate(). googleapiclient
# lowercases the resource names from the discovery document, which is why these
# read as one word.
METRIC_SETS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "crash_rate": (
        "crashRateMetricSet",
        ("vitals", "crashrate"),
        "Crash rate",
    ),
    "anr_rate": (
        "anrRateMetricSet",
        ("vitals", "anrrate"),
        "ANR (Application Not Responding) rate",
    ),
    "slow_start_rate": (
        "slowStartRateMetricSet",
        ("vitals", "slowstartrate"),
        "Slow cold/warm/hot start rate",
    ),
    "slow_rendering_rate": (
        "slowRenderingRateMetricSet",
        ("vitals", "slowrenderingrate"),
        "Slow rendering rate (janky frames)",
    ),
    "excessive_wakeup_rate": (
        "excessiveWakeupRateMetricSet",
        ("vitals", "excessivewakeuprate"),
        "Excessive background wake-up rate",
    ),
    "stuck_background_wakelock_rate": (
        "stuckBackgroundWakelockRateMetricSet",
        ("vitals", "stuckbackgroundwakelockrate"),
        "Stuck background wakelock rate",
    ),
    "lmk_rate": (
        "lmkRateMetricSet",
        ("vitals", "lmkrate"),
        "Low Memory Killer (LMK) rate",
    ),
    "error_counts": (
        "errorCountMetricSet",
        ("vitals", "errors", "counts"),
        "Error counts (crashes and ANRs, grouped)",
    ),
}

# Aggregation periods accepted by timelineSpec.
AGGREGATION_PERIODS = frozenset({"HOURLY", "DAILY", "FULL_RANGE"})

# User cohorts accepted by a metric-set query.
USER_COHORTS = frozenset({"OS_PUBLIC", "OS_BETA", "APP_TESTERS"})


def metric_set_keys() -> list[str]:
    """Return the short keys of every supported metric set."""
    return sorted(METRIC_SETS)


def _metric_set_resource(package_name: str, metric_set: str) -> str:
    """Build the metric-set resource name for a package.

    Args:
        package_name: App package name, e.g. com.example.app.
        metric_set: Short metric set key, e.g. crash_rate.

    Returns:
        Resource name, e.g. apps/com.example.app/crashRateMetricSet.

    Raises:
        ReportingClientError: if the metric set key is not recognised.
    """
    entry = METRIC_SETS.get(metric_set)
    if entry is None:
        raise ReportingClientError(
            f"Unknown metric set '{metric_set}'. Supported: {', '.join(metric_set_keys())}"
        )
    return f"apps/{package_name}/{entry[0]}"


def _to_datetime(value: dict[str, Any] | str | None) -> datetime | None:
    """Parse a Reporting API timestamp to a datetime, or None.

    The API returns two shapes depending on the field: a google.type.DateTime
    object ({year, month, day, hours, ...}) for timeline buckets, and an RFC3339
    string for freshness and event times.
    """
    if not value:
        return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    try:
        return datetime(
            year=int(value["year"]),
            month=int(value["month"]),
            day=int(value["day"]),
            hour=int(value.get("hours", 0) or 0),
            minute=int(value.get("minutes", 0) or 0),
            second=int(value.get("seconds", 0) or 0),
            tzinfo=UTC,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _date_to_api(value: datetime) -> dict[str, int]:
    """Render a datetime as a google.type.DateTime body for timelineSpec."""
    return {
        "year": value.year,
        "month": value.month,
        "day": value.day,
        "hours": value.hour,
        "minutes": value.minute,
        "seconds": value.second,
    }


def _dimension_value(entry: dict[str, Any]) -> Any:
    """Extract whichever typed value a DimensionValue carries."""
    for key in ("stringValue", "int64Value", "valueLabel"):
        if key in entry:
            return entry[key]
    return None


def _metric_value(entry: dict[str, Any]) -> Any:
    """Extract whichever typed value a MetricValue carries."""
    decimal = entry.get("decimalValue")
    if isinstance(decimal, dict) and "value" in decimal:
        raw = decimal["value"]
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    return entry.get("int64Value") or entry.get("value")


def _parse_row(row: dict[str, Any]) -> MetricRow:
    """Parse one timeline row into a MetricRow."""
    dimensions: dict[str, Any] = {}
    for entry in row.get("dimensions", []):
        name = entry.get("dimension")
        if name:
            dimensions[name] = _dimension_value(entry)

    metrics: dict[str, Any] = {}
    for entry in row.get("metrics", []):
        name = entry.get("metric")
        if name:
            metrics[name] = _metric_value(entry)

    return MetricRow(
        start_time=_to_datetime(row.get("startTime")),
        dimensions=dimensions,
        metrics=metrics,
    )


def _parse_freshness(metric_set_body: dict[str, Any]) -> list[MetricSetFreshness]:
    """Parse the freshnessInfo block of a metric-set resource."""
    info = metric_set_body.get("freshnessInfo") or {}
    return [
        MetricSetFreshness(
            aggregation_period=item.get("aggregationPeriod"),
            latest_end_time=_to_datetime(item.get("latestEndTime")),
        )
        for item in info.get("freshnesses", [])
    ]


def _nested(value: Any, key: str) -> Any:
    """Read ``key`` out of a nested object, or return the value itself.

    Several Reporting fields arrive either as a scalar or as a small object
    ({"versionCode": 42}, {"marketingName": "Pixel 8"}) depending on the
    endpoint and API revision. Callers want the scalar either way.
    """
    if isinstance(value, dict):
        inner = value.get(key)
        return None if inner in (None, "") else str(inner)
    return value


def _parse_app(item: dict[str, Any]) -> ReportingApp:
    """Parse an apps.search entry."""
    return ReportingApp(
        name=item.get("name", ""),
        package_name=item.get("packageName", ""),
        display_name=item.get("displayName"),
    )


def _parse_anomaly(item: dict[str, Any]) -> Anomaly:
    """Parse an anomalies.list entry."""
    dimensions = {
        entry.get("dimension"): _dimension_value(entry)
        for entry in item.get("dimensions", [])
        if entry.get("dimension")
    }
    metric_entry = item.get("metric") or {}
    return Anomaly(
        name=item.get("name"),
        metric_set=item.get("metricSet"),
        dimensions=dimensions,
        metric=metric_entry.get("metric") if isinstance(metric_entry, dict) else None,
        metric_value=_metric_value(metric_entry) if isinstance(metric_entry, dict) else None,
    )


def _parse_error_issue(item: dict[str, Any]) -> ErrorIssue:
    """Parse a vitals.errors.issues.search entry."""
    return ErrorIssue(
        name=item.get("name"),
        issue_type=item.get("type") or item.get("errorIssueType"),
        cause=item.get("cause"),
        location=item.get("location"),
        error_report_count=item.get("errorReportCount"),
        distinct_users=item.get("distinctUsers"),
        first_os_version=_nested(item.get("firstOsVersion"), "apiLevel"),
        last_os_version=_nested(item.get("lastOsVersion"), "apiLevel"),
        first_app_version=_nested(item.get("firstAppVersion"), "versionCode"),
        last_app_version=_nested(item.get("lastAppVersion"), "versionCode"),
        sample_error_reports=item.get("sampleErrorReports", []),
    )


def _parse_error_report(item: dict[str, Any]) -> ErrorReport:
    """Parse a vitals.errors.reports.search entry."""
    return ErrorReport(
        name=item.get("name"),
        report_type=item.get("type") or item.get("reportType"),
        issue=item.get("issue"),
        event_time=_to_datetime(item.get("eventTime")),
        device_model=_nested(item.get("deviceModel"), "marketingName"),
        os_version=_nested(item.get("osVersion"), "apiLevel"),
        app_version=_nested(item.get("appVersion"), "versionCode"),
        report_text=item.get("reportText"),
    )


class ReportingMixin:
    """Play Developer Reporting API operations.

    Mixed into :class:`google_play_console_mcp.client.PlayStoreClient` so that credential
    resolution, retry/backoff and the transport lock are shared with the
    Publisher API rather than reimplemented. The mixin relies on three members
    the host class provides: ``_credentials_json``, ``_credentials_path`` and
    ``_execute``.
    """

    # Provided by PlayStoreClient.
    _credentials_json: str | dict[str, Any] | None
    _credentials_path: str | None
    _logger: Any

    _reporting_service: Any = None

    def _execute(self, request: Any) -> Any:  # pragma: no cover - provided by host
        raise NotImplementedError

    # =========================================================================
    # Service
    # =========================================================================

    def _get_reporting_service(self) -> Any:
        """Get or create the Play Developer Reporting service.

        Built separately from the Publisher service because it needs its own
        OAuth scope. Play Console grants reporting access independently of
        publishing access, so a service account that deploys successfully can
        still be refused here with a 403.

        Raises:
            ReportingClientError: if credentials cannot be resolved or the
                service cannot be built.
        """
        if self._reporting_service is not None:
            return self._reporting_service

        self._logger.info("Initializing Play Developer Reporting API client")

        try:
            credentials = load_service_account_credentials(
                self._credentials_json, self._credentials_path, REPORTING_SCOPES
            )

            if not credentials:
                raise ReportingClientError(
                    "No valid credentials found. Set GOOGLE_APPLICATION_CREDENTIALS (path) or GOOGLE_PLAY_STORE_CREDENTIALS (JSON or path), store a key with `google-play-console-mcp credentials set`, or enable user OAuth with GOOGLE_PLAY_MCP_USE_ADC=1 (`google-play-console-mcp credentials adc`)."
                )

            self._reporting_service = build(
                "playdeveloperreporting",
                "v1beta1",
                credentials=credentials,
                cache_discovery=False,
            )
            self._logger.info("Reporting API client initialized successfully")
            return self._reporting_service
        except Exception as e:
            if isinstance(e, ReportingClientError):
                raise
            self._logger.exception("Failed to initialize Reporting API client", error=str(e))
            raise ReportingClientError(f"Failed to initialize Reporting API client: {e}") from e

    def _metric_set_resource_api(self, metric_set: str) -> Any:
        """Walk the accessor path for a metric set to its googleapiclient resource."""
        entry = METRIC_SETS.get(metric_set)
        if entry is None:
            raise ReportingClientError(
                f"Unknown metric set '{metric_set}'. Supported: {', '.join(metric_set_keys())}"
            )
        node = self._get_reporting_service()
        for step in entry[1]:
            node = getattr(node, step)()
        return node

    # =========================================================================
    # Apps
    # =========================================================================

    def list_reporting_apps(self, max_results: int = 100) -> list[ReportingApp]:
        """List every app the service account can see through the Reporting API.

        The Publisher API has no "list my apps" endpoint; this is the only way
        to enumerate a portfolio from a service account.

        Args:
            max_results: Maximum number of apps to return.

        Returns:
            List of apps.
        """
        service = self._get_reporting_service()
        apps: list[ReportingApp] = []
        token: str | None = None

        while len(apps) < max_results:
            kwargs: dict[str, Any] = {"pageSize": min(max_results - len(apps), 50)}
            if token:
                kwargs["pageToken"] = token
            result = self._execute(service.apps().search(**kwargs))
            apps.extend(_parse_app(item) for item in result.get("apps", []))
            token = result.get("nextPageToken")
            if not token:
                break

        return apps[:max_results]

    def get_release_filter_options(self, package_name: str) -> ReleaseFilterOptions:
        """List the release dimensions usable as query filters for an app.

        Args:
            package_name: App package name.

        Returns:
            Tracks and version codes available as filters.
        """
        service = self._get_reporting_service()
        result = self._execute(
            service.apps().fetchReleaseFilterOptions(name=f"apps/{package_name}")
        )
        return ReleaseFilterOptions(
            package_name=package_name,
            tracks=result.get("tracks", []),
            version_codes=result.get("versionCodes", []),
        )

    # =========================================================================
    # Metric sets
    # =========================================================================

    def get_metric_set(self, package_name: str, metric_set: str) -> MetricSetInfo:
        """Get metadata for one metric set, including freshness.

        Args:
            package_name: App package name.
            metric_set: Short metric set key, e.g. crash_rate.

        Returns:
            Metric set metadata.
        """
        name = _metric_set_resource(package_name, metric_set)
        api = self._metric_set_resource_api(metric_set)
        result = self._execute(api.get(name=name))
        return MetricSetInfo(
            name=result.get("name", name),
            metric_set=metric_set,
            package_name=package_name,
            freshness=_parse_freshness(result),
        )

    def get_metric_set_freshness(
        self, package_name: str, metric_set: str | None = None
    ) -> list[MetricSetInfo]:
        """Report how current each metric set's data is.

        Call this before acting on a vitals number. Play data lags reality by
        hours to days, so a query covering "the last 7 days" can silently return
        five — the absent days look like an improvement rather than a gap.

        Args:
            package_name: App package name.
            metric_set: Short metric set key, or None for every metric set.

        Returns:
            Freshness metadata, one entry per metric set queried. A metric set
            the caller cannot access is skipped rather than failing the batch.
        """
        keys = [metric_set] if metric_set else metric_set_keys()
        results: list[MetricSetInfo] = []
        for key in keys:
            try:
                results.append(self.get_metric_set(package_name, key))
            except ReportingClientError:
                raise
            except Exception as e:  # noqa: BLE001 - one inaccessible set must not fail the batch
                self._logger.warning(
                    "Skipping metric set freshness",
                    package_name=package_name,
                    metric_set=key,
                    error=str(e),
                )
        return results

    def query_metric_set(
        self,
        package_name: str,
        metric_set: str,
        metrics: Sequence[str] | None = None,
        dimensions: Sequence[str] | None = None,
        days_back: int = 7,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        aggregation_period: str = "DAILY",
        filter_expression: str | None = None,
        user_cohort: str | None = None,
        max_results: int = 1000,
    ) -> MetricQueryResult:
        """Query a metric set timeline.

        Args:
            package_name: App package name.
            metric_set: Short metric set key, e.g. crash_rate.
            metrics: Metric names to return, e.g. ["crashRate", "distinctUsers"].
                Defaults to whatever the API returns for the set.
            dimensions: Dimensions to slice by, e.g. ["versionCode", "countryCode"].
            days_back: Window size ending now, used when start_time is not given.
            start_time: Explicit window start. Overrides days_back.
            end_time: Explicit window end. Defaults to now.
            aggregation_period: HOURLY, DAILY or FULL_RANGE.
            filter_expression: API filter string, e.g. 'versionCode = 42'.
            user_cohort: OS_PUBLIC, OS_BETA or APP_TESTERS.
            max_results: Maximum rows to return across pages.

        Returns:
            Timeline rows.

        Raises:
            ReportingClientError: on an unknown metric set, aggregation period
                or user cohort.
        """
        if aggregation_period not in AGGREGATION_PERIODS:
            raise ReportingClientError(
                f"Unknown aggregation_period '{aggregation_period}'. "
                f"Supported: {', '.join(sorted(AGGREGATION_PERIODS))}"
            )
        if user_cohort is not None and user_cohort not in USER_COHORTS:
            raise ReportingClientError(
                f"Unknown user_cohort '{user_cohort}'. Supported: {', '.join(sorted(USER_COHORTS))}"
            )

        name = _metric_set_resource(package_name, metric_set)
        api = self._metric_set_resource_api(metric_set)

        window_end = end_time or datetime.now(UTC)
        window_start = start_time or (window_end - timedelta(days=days_back))

        body: dict[str, Any] = {
            "timelineSpec": {
                "aggregationPeriod": aggregation_period,
                "startTime": _date_to_api(window_start),
                "endTime": _date_to_api(window_end),
            },
        }
        if metrics:
            body["metrics"] = list(metrics)
        if dimensions:
            body["dimensions"] = list(dimensions)
        if filter_expression:
            body["filter"] = filter_expression
        if user_cohort:
            body["userCohort"] = user_cohort

        rows: list[MetricRow] = []
        token: str | None = None
        next_token: str | None = None

        while len(rows) < max_results:
            page_body = dict(body)
            page_body["pageSize"] = min(max_results - len(rows), 1000)
            if token:
                page_body["pageToken"] = token
            result = self._execute(api.query(name=name, body=page_body))
            rows.extend(_parse_row(row) for row in result.get("rows", []))
            token = result.get("nextPageToken")
            next_token = token
            if not token:
                break

        return MetricQueryResult(
            package_name=package_name,
            metric_set=metric_set,
            rows=rows[:max_results],
            next_page_token=next_token,
        )

    # =========================================================================
    # Anomalies
    # =========================================================================

    def list_anomalies(
        self,
        package_name: str,
        filter_expression: str | None = None,
        max_results: int = 100,
    ) -> list[Anomaly]:
        """List statistical anomalies Play detected across the metric sets.

        Args:
            package_name: App package name.
            filter_expression: Optional API filter string.
            max_results: Maximum number of anomalies to return.

        Returns:
            Detected anomalies.
        """
        service = self._get_reporting_service()
        anomalies: list[Anomaly] = []
        token: str | None = None

        while len(anomalies) < max_results:
            kwargs: dict[str, Any] = {
                "parent": f"apps/{package_name}",
                "pageSize": min(max_results - len(anomalies), 50),
            }
            if filter_expression:
                kwargs["filter"] = filter_expression
            if token:
                kwargs["pageToken"] = token
            result = self._execute(service.anomalies().list(**kwargs))
            anomalies.extend(_parse_anomaly(item) for item in result.get("anomalies", []))
            token = result.get("nextPageToken")
            if not token:
                break

        return anomalies[:max_results]

    # =========================================================================
    # Errors
    # =========================================================================

    def search_error_issues(
        self,
        package_name: str,
        filter_expression: str | None = None,
        days_back: int = 7,
        order_by: str | None = None,
        sample_report_limit: int | None = None,
        max_results: int = 50,
    ) -> list[ErrorIssue]:
        """Search grouped error issues — clusters of similar crashes or ANRs.

        Args:
            package_name: App package name.
            filter_expression: Optional API filter string, e.g.
                'errorIssueType = CRASH'.
            days_back: Window size ending now.
            order_by: Sort expression, e.g. 'errorReportCount desc'.
            sample_report_limit: How many sample report names to attach per issue.
            max_results: Maximum number of issues to return.

        Returns:
            Grouped error issues.
        """
        service = self._get_reporting_service()
        end = datetime.now(UTC)
        start = end - timedelta(days=days_back)

        issues: list[ErrorIssue] = []
        token: str | None = None

        while len(issues) < max_results:
            kwargs: dict[str, Any] = {
                "parent": f"apps/{package_name}",
                "pageSize": min(max_results - len(issues), 50),
                "interval_startTime_year": start.year,
                "interval_startTime_month": start.month,
                "interval_startTime_day": start.day,
                "interval_endTime_year": end.year,
                "interval_endTime_month": end.month,
                "interval_endTime_day": end.day,
            }
            if filter_expression:
                kwargs["filter"] = filter_expression
            if order_by:
                kwargs["orderBy"] = order_by
            if sample_report_limit is not None:
                kwargs["sampleErrorReportLimit"] = sample_report_limit
            if token:
                kwargs["pageToken"] = token

            result = self._execute(service.vitals().errors().issues().search(**kwargs))
            issues.extend(_parse_error_issue(item) for item in result.get("errorIssues", []))
            token = result.get("nextPageToken")
            if not token:
                break

        return issues[:max_results]

    def search_error_reports(
        self,
        package_name: str,
        filter_expression: str | None = None,
        days_back: int = 7,
        max_results: int = 50,
    ) -> list[ErrorReport]:
        """Search raw error reports — individual crashes and ANRs with stack traces.

        Args:
            package_name: App package name.
            filter_expression: Optional API filter string, e.g.
                'errorIssueId = abc123'.
            days_back: Window size ending now.
            max_results: Maximum number of reports to return.

        Returns:
            Individual error reports.
        """
        service = self._get_reporting_service()
        end = datetime.now(UTC)
        start = end - timedelta(days=days_back)

        reports: list[ErrorReport] = []
        token: str | None = None

        while len(reports) < max_results:
            kwargs: dict[str, Any] = {
                "parent": f"apps/{package_name}",
                "pageSize": min(max_results - len(reports), 50),
                "interval_startTime_year": start.year,
                "interval_startTime_month": start.month,
                "interval_startTime_day": start.day,
                "interval_endTime_year": end.year,
                "interval_endTime_month": end.month,
                "interval_endTime_day": end.day,
            }
            if filter_expression:
                kwargs["filter"] = filter_expression
            if token:
                kwargs["pageToken"] = token

            result = self._execute(service.vitals().errors().reports().search(**kwargs))
            reports.extend(_parse_error_report(item) for item in result.get("errorReports", []))
            token = result.get("nextPageToken")
            if not token:
                break

        return reports[:max_results]
