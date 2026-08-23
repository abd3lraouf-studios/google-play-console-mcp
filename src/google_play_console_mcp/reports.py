"""Google Play bulk-report parsing. No network I/O.

Installs, ratings, crashes, earnings, store acquisitions and search terms are
**not available from any Play REST API**. They exist only as CSV files that Play
Console writes to a private Cloud Storage bucket, ``gs://pubsite_prod_rev_<id>``.
Without this module, "how much did this app earn?" and "what did users search to
find us?" are simply unanswerable.

Two properties of those files drive the whole design:

1. **They are UTF-16 with a BOM, not UTF-8.** Decoding naively yields either
   mojibake or a ``UnicodeDecodeError``.
2. **Google adds, renames and reorders columns without notice** (the earnings
   columns changed in July 2026). Nothing here may depend on column position or
   column count — every lookup is by header name, whitespace- and case-tolerant,
   and a miss raises an error listing the headers actually present so the fix is
   obvious instead of archaeological.

This module deliberately performs no network I/O: it accepts bytes or a binary
file object, so it is unit-testable offline. ``google_play_console_mcp/reports_bucket.py``
owns fetching and calls in here.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import IO, TYPE_CHECKING, Any

from google_play_console_mcp.errors import ValidationError
from google_play_console_mcp.models import Freshness, Report, ReportFamily, ReportRow

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"
_UTF8_BOM = b"\xef\xbb\xbf"

#: Play stats land 3-7 days after the fact.
STATS_LAG_DAYS = 7
#: A month's earnings are published around the 5th of the following month.
EARNINGS_PUBLISH_DAY = 5

SOURCE = "play_gcs_reports"

DOCS_URL = "https://support.google.com/googleplay/android-developer/answer/6135870"


class ReportKind(StrEnum):
    """Report families this server can read."""

    INSTALLS = "installs"
    RATINGS = "ratings"
    CRASHES = "crashes"
    EARNINGS = "earnings"
    SALES = "sales"
    REVIEWS = "reviews"
    STORE_PERFORMANCE = "store_performance"
    STORE_CONVERSION = "store_conversion"
    SUBSCRIPTIONS = "subscriptions"
    RETAINED_INSTALLERS = "retained_installers"
    BUYERS_7D = "buyers_7d"


#: Report kinds that are account-wide rather than per-app.
ACCOUNT_LEVEL_KINDS = frozenset({ReportKind.EARNINGS, ReportKind.SALES})

#: The bucket layout, with the file prefix and dimensions for each family.
#:
#: ``discontinued`` matters: several families that appear in older tooling stopped
#: being published years ago, and a tool that silently returns nothing for them
#: reads as "no data" rather than "this report no longer exists".
FAMILIES: dict[ReportKind, ReportFamily] = {
    ReportKind.INSTALLS: ReportFamily(
        name="installs",
        prefix="stats/installs/",
        dimensions=[
            "overview",
            "country",
            "device",
            "language",
            "app_version",
            "os_version",
            "carrier",
        ],
        default_dimension="overview",
        description="Daily and active-device installs, uninstalls and upgrades by dimension.",
    ),
    ReportKind.RATINGS: ReportFamily(
        name="ratings",
        prefix="stats/ratings/",
        dimensions=[
            "overview",
            "country",
            "device",
            "language",
            "app_version",
            "os_version",
            "carrier",
        ],
        default_dimension="overview",
        description="Daily and total average rating by dimension.",
    ),
    ReportKind.CRASHES: ReportFamily(
        name="crashes",
        prefix="stats/crashes/",
        dimensions=["overview", "device", "os_version", "app_version"],
        default_dimension="overview",
        description=(
            "Daily crashes and ANRs by dimension. Distinct from the Reporting API "
            "vitals tools: this is a raw daily count, not a user-weighted rate."
        ),
    ),
    ReportKind.STORE_PERFORMANCE: ReportFamily(
        name="store_performance",
        prefix="stats/store_performance/",
        dimensions=["traffic_source", "country"],
        default_dimension="traffic_source",
        description=(
            "Store acquisitions split by traffic source (Explore / Search / Ads and "
            "referrals) or by country. Acquisitions only — for visitors and "
            "conversion rate use store_conversion."
        ),
    ),
    ReportKind.STORE_CONVERSION: ReportFamily(
        name="store_conversion",
        prefix="stats/store_performance/",
        dimensions=["traffic_source", "country"],
        default_dimension="traffic_source",
        description=(
            "Store-listing visitors, acquisitions and visitor-to-install conversion "
            "rate. The raw rows also break down by SEARCH TERM and UTM campaign — the "
            "only place either is available. Live replacement for retained_installers."
        ),
    ),
    ReportKind.SUBSCRIPTIONS: ReportFamily(
        name="subscriptions",
        prefix="financial-stats/subscriptions/",
        dimensions=["country"],
        default_dimension="country",
        description="Subscription activity (new / cancelled / active) by country.",
    ),
    ReportKind.EARNINGS: ReportFamily(
        name="earnings",
        prefix="earnings/",
        dimensions=[],
        default_dimension=None,
        description=(
            "Account-wide transaction-level earnings in merchant currency. One row "
            "per transaction across every app."
        ),
    ),
    ReportKind.SALES: ReportFamily(
        name="sales",
        prefix="sales/",
        dimensions=[],
        default_dimension=None,
        description="Account-wide sales report: orders, taxes and refunds.",
    ),
    ReportKind.REVIEWS: ReportFamily(
        name="reviews",
        prefix="reviews/",
        dimensions=[],
        default_dimension=None,
        description=(
            "Individual user reviews with star rating and text. Unlike the Reviews "
            "API, which returns only about a week, these files go back years."
        ),
    ),
    ReportKind.RETAINED_INSTALLERS: ReportFamily(
        name="retained_installers",
        prefix="acquisition/retained_installers/",
        dimensions=["channel", "country", "play_country", "utm_tagged"],
        default_dimension="channel",
        description=(
            "Store-listing visitors, installers, conversion rate and 1/7/15/30-day "
            "retention by acquisition channel."
        ),
        discontinued=(
            "Google stopped publishing this report after 2021-06. Use store_conversion "
            "for current acquisition and conversion data."
        ),
    ),
    ReportKind.BUYERS_7D: ReportFamily(
        name="buyers_7d",
        prefix="acquisition/buyers_7d/",
        dimensions=["channel", "country", "play_country"],
        default_dimension="channel",
        description="Users who purchased within 7 days of installing, by channel or country.",
        discontinued="Google stopped publishing this report after 2021-06.",
    ),
}

#: File-name prefix per family. Two families share one directory and are told
#: apart only by this: ``total_store_performance_`` holds acquisitions, while its
#: ``store_performance_`` sibling holds visitors, conversion and search terms.
FILE_PREFIXES: dict[ReportKind, str] = {
    ReportKind.INSTALLS: "installs_",
    ReportKind.RATINGS: "ratings_",
    ReportKind.CRASHES: "crashes_",
    ReportKind.STORE_PERFORMANCE: "total_store_performance_",
    ReportKind.STORE_CONVERSION: "store_performance_",
    ReportKind.SUBSCRIPTIONS: "subscriptions_",
    ReportKind.EARNINGS: "earnings_",
    ReportKind.SALES: "salesreport_",
    ReportKind.REVIEWS: "reviews_",
    ReportKind.RETAINED_INSTALLERS: "retained_installers_",
    ReportKind.BUYERS_7D: "buyers_7d_",
}


def missing_column_error(
    wanted: Sequence[str], available: Sequence[str], *, source: str
) -> ValidationError:
    """Build the error for a report column Google renamed or removed."""
    return ValidationError(
        f"{source}: no column matching {' / '.join(wanted)!r}. Google changes report "
        f"columns without notice (the earnings columns changed in July 2026).",
        remedy=(
            "Inspect the available headers listed in details and map the metric to the "
            "new column name, or open an issue with that header list attached."
        ),
        doc_url=DOCS_URL,
        details={"available_headers": list(available), "expected_one_of": list(wanted)},
    )


def list_families() -> list[ReportFamily]:
    """Every report family, including the discontinued ones."""
    return [FAMILIES[kind] for kind in ReportKind]


# --- Months and object paths -------------------------------------------------


def normalize_month(month: str | date) -> str:
    """Normalize "2026-07", "202607", "2026/07" or a date into "202607"."""
    if isinstance(month, date):
        return f"{month.year:04d}{month.month:02d}"
    digits = re.sub(r"\D", "", str(month))
    if len(digits) != 6:
        raise ValidationError(
            f"Invalid month {month!r}.",
            remedy="Pass the month as 'YYYY-MM', for example '2026-07'.",
        )
    mon = int(digits[4:6])
    if not 1 <= mon <= 12:
        raise ValidationError(
            f"Invalid month {month!r}: the month component must be 01-12.",
            remedy="Pass the month as 'YYYY-MM', for example '2026-07'.",
        )
    return digits


def month_bounds(month: str | date) -> tuple[date, date]:
    """First and last day of the month, inclusive."""
    ym = normalize_month(month)
    year, mon = int(ym[:4]), int(ym[4:6])
    first = date(year, mon, 1)
    last = date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
    return first, last


def report_object_prefix(
    kind: ReportKind | str,
    month: str | date | None = None,
    *,
    package_name: str | None = None,
) -> str:
    """Build the object prefix to list under for a report.

    Listing rather than constructing an exact name is deliberate: earnings
    objects carry a trailing account id that is not derivable, and Google has
    changed the dimension suffix on stats objects before.
    """
    kind = ReportKind(kind)
    family = FAMILIES[kind]
    ym = normalize_month(month) if month is not None else ""
    base = f"{family.prefix}{FILE_PREFIXES[kind]}"

    if kind in ACCOUNT_LEVEL_KINDS:
        return f"{base}{ym}" if ym else family.prefix
    if package_name:
        base += f"{package_name}_"
        if ym:
            base += ym
    return base


# --- Decoding and row iteration ----------------------------------------------


def decode_report_bytes(data: bytes) -> str:
    """Decode a Play report to text.

    Play writes these as UTF-16 with a BOM. UTF-8 and BOM-less UTF-16 are handled
    too, so a future format change does not break every report tool at once.
    """
    if data.startswith((_UTF16_LE_BOM, _UTF16_BE_BOM)):
        return data.decode("utf-16")
    if data.startswith(_UTF8_BOM):
        return data.decode("utf-8-sig")
    head = data[:512]
    if head.count(b"\x00") > len(head) // 4:
        # Dense NUL bytes mean 16-bit code units without a BOM.
        return data.decode("utf-16-be" if head[:1] == b"\x00" else "utf-16-le")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "Report file is neither valid UTF-16 nor UTF-8.",
            remedy=(
                "Re-download the object from the reports bucket; a truncated or "
                "partially written download is the usual cause."
            ),
            details={"decode_error": str(exc)},
        ) from exc


def normalize_header(name: str) -> str:
    """Fold a CSV header to a stable lookup key.

    "Daily Device Installs" -> "daily_device_installs"
    "Amount (Merchant Currency)" -> "amount_merchant_currency"
    """
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return cleaned.strip("_")


def iter_report_rows(source: bytes | IO[bytes] | str) -> Iterator[dict[str, str]]:
    """Yield each CSV row as a dict keyed by normalized header name.

    Accepts raw bytes, a binary file object, or already-decoded text. Column
    order and count are irrelevant to consumers of this iterator by design.
    """
    if isinstance(source, str):
        text = source
    else:
        raw = source if isinstance(source, bytes) else source.read()
        text = decode_report_bytes(raw)

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        return
    keys = [normalize_header(h) for h in header]
    for values in reader:
        if not any(v.strip() for v in values):
            continue
        yield {k: (values[i].strip() if i < len(values) else "") for i, k in enumerate(keys)}


def read_headers(source: bytes | IO[bytes] | str) -> list[str]:
    """Normalized header names of a report, for diagnostics."""
    for row in iter_report_rows(source):
        return list(row)
    return []


# --- Column resolution -------------------------------------------------------


def find_column(
    row: dict[str, str],
    candidates: Sequence[str],
    *,
    source: str = "report",
    required: bool = True,
    exclude: Sequence[str] = (),
) -> str | None:
    """Resolve the first candidate header present in ``row``.

    Exact normalized match first, then a whole-token subset match so a Google
    rename from "Daily Device Installs" to "Daily Device Installs (new)" still
    resolves.

    Matching is on whole tokens, never substrings. Substring matching silently
    confuses "Date" with "Update events", and "Install events" with "Uninstall
    events" — which attributes real numbers to the wrong metric while looking
    entirely plausible. For the same reason single-token candidates ("Country",
    "Device") must match exactly: they are too ambiguous to fuzzy-match.

    ``exclude`` names columns another field has already claimed. Without it,
    looking for "Merchant Currency" in a file that only has "Amount (Merchant
    Currency)" resolves to the amount column by token subset, and the earnings
    tool then reports "4.99" as the currency code.
    """
    blocked = set(exclude)
    normalized = [normalize_header(c) for c in candidates]
    for key in normalized:
        if key in row and key not in blocked:
            return key
    for key in normalized:
        wanted = set(key.split("_"))
        if len(wanted) < 2:
            continue
        for actual in row:
            if actual not in blocked and wanted <= set(actual.split("_")):
                return actual
    if required:
        raise missing_column_error(list(candidates), sorted(row), source=source)
    return None


def parse_float(value: str | None) -> float | None:
    """Parse a numeric cell, tolerating thousands separators, percents and blanks."""
    if value is None:
        return None
    cleaned = value.strip().replace(",", "").replace("%", "")
    if not cleaned or cleaned in {"-", "N/A", "NA"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y%m%d")


def parse_date(value: str | None) -> date | None:
    """Parse a date cell. Stats reports use ISO; earnings use "Jul 1, 2026"."""
    if not value:
        return None
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            # Report cells are calendar dates with no zone; attaching one would
            # invent information that is not in the file.
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# --- Freshness ---------------------------------------------------------------


def report_freshness(
    kind: ReportKind | str,
    month: str | date,
    *,
    today: date | None = None,
    row_count: int = 0,
) -> Freshness:
    """Describe how complete a month's report can possibly be right now.

    Prevents the worst failure in this domain: reporting "0 installs" or "$0
    earned" for a month whose CSV has not been published yet, as if the app had
    collapsed. A zero has to be distinguishable from an absence.
    """
    kind = ReportKind(kind)
    today = today or datetime.now(UTC).date()
    ym = normalize_month(month)
    first, last = month_bounds(ym)
    period = f"{ym[:4]}-{ym[4:]}"

    if kind in ACCOUNT_LEVEL_KINDS:
        available_from = date(
            last.year + (last.month == 12),
            (last.month % 12) + 1,
            EARNINGS_PUBLISH_DAY,
        )
        if today < available_from:
            return Freshness(
                as_of=today,
                requested_period=period,
                source=SOURCE,
                lag_days=(available_from - today).days,
                is_complete=False,
                caveat=(
                    f"{kind.value} for {period} are not published yet — Google releases "
                    f"them around {available_from.isoformat()}. Any total shown is zero "
                    f"because the report does not exist, not because revenue was zero."
                ),
            )
        return Freshness(as_of=today, requested_period=period, source=SOURCE, is_complete=True)

    if first > today:
        return Freshness(
            as_of=None,
            requested_period=period,
            source=SOURCE,
            lag_days=(first - today).days,
            is_complete=False,
            caveat=(
                f"{period} has not started yet ({first.isoformat()} is in the future), so "
                f"no {kind.value} data exists for it. Any total shown is the absence of a "
                f"report, not a measurement."
            ),
        )

    complete_after = last + timedelta(days=STATS_LAG_DAYS)
    if today <= complete_after:
        covered_through = min(last, today - timedelta(days=3))
        incomplete = covered_through < last
        return Freshness(
            as_of=covered_through if covered_through >= first else None,
            requested_period=period,
            source=SOURCE,
            lag_days=STATS_LAG_DAYS,
            is_complete=not incomplete,
            caveat=(
                f"{kind.value} data for {period} is still filling in. Play stats land 3-7 "
                f"days late, so figures are complete only after {complete_after.isoformat()}."
            )
            if incomplete
            else None,
        )

    if row_count == 0:
        return Freshness(
            as_of=today,
            requested_period=period,
            source=SOURCE,
            lag_days=STATS_LAG_DAYS,
            is_complete=False,
            caveat=(
                f"No rows found for {period} even though the reporting window has closed. "
                f"The app likely had no data that month, or the report object is missing "
                f"from the bucket."
            ),
        )
    return Freshness(as_of=last, requested_period=period, source=SOURCE, is_complete=True)


# --- Normalization into ReportRow --------------------------------------------

_DATE_COLUMNS = ("Date", "Transaction Date", "Order Charged Date", "Day")

#: Header candidates per emitted metric. Order matters: first match wins.
INSTALLS_METRICS: dict[str, tuple[str, ...]] = {
    "daily_device_installs": ("Daily Device Installs",),
    "daily_device_uninstalls": ("Daily Device Uninstalls",),
    "daily_device_upgrades": ("Daily Device Upgrades",),
    "active_device_installs": ("Active Device Installs",),
    "daily_user_installs": ("Daily User Installs",),
    "daily_user_uninstalls": ("Daily User Uninstalls",),
    "total_user_installs": ("Total User Installs",),
    "install_events": ("Install events",),
    "uninstall_events": ("Uninstall events",),
    "update_events": ("Update events",),
}

RATINGS_METRICS: dict[str, tuple[str, ...]] = {
    "daily_average_rating": ("Daily Average Rating",),
    "total_average_rating": ("Total Average Rating",),
}

CRASHES_METRICS: dict[str, tuple[str, ...]] = {
    "daily_crashes": ("Daily Crashes",),
    "daily_anrs": ("Daily ANRs",),
}

STORE_PERFORMANCE_METRICS: dict[str, tuple[str, ...]] = {
    "store_listing_acquisitions": ("Store Listing Acquisitions",),
    "store_listing_visitors": ("Store Listing Visitors",),
    "store_listing_conversion_rate": ("Store Listing Conversion Rate",),
}

SUBSCRIPTION_METRICS: dict[str, tuple[str, ...]] = {
    "new_subscriptions": ("New Subscriptions",),
    "cancelled_subscriptions": ("Cancelled Subscriptions",),
    "active_subscriptions": ("Active Subscriptions",),
}

#: Dimension column names keyed by the dimension slug used in the object path.
DIMENSION_COLUMNS: dict[str, tuple[str, ...]] = {
    "country": ("Country", "Country / Region", "Buyer Country"),
    "device": ("Device",),
    "os_version": ("Android OS Version", "OS Version"),
    "app_version": ("App Version Code", "App Version"),
    "language": ("Language",),
    "carrier": ("Carrier",),
    "traffic_source": ("Acquisition Channel", "Traffic Source", "Channel"),
    "search_term": ("Search Term",),
    "utm_source": ("UTM Source",),
    "utm_campaign": ("UTM Campaign",),
    "channel": ("Acquisition Channel", "Channel"),
}

METRICS_BY_KIND: dict[ReportKind, dict[str, tuple[str, ...]]] = {
    ReportKind.INSTALLS: INSTALLS_METRICS,
    ReportKind.RATINGS: RATINGS_METRICS,
    ReportKind.CRASHES: CRASHES_METRICS,
    ReportKind.STORE_PERFORMANCE: STORE_PERFORMANCE_METRICS,
    ReportKind.STORE_CONVERSION: STORE_PERFORMANCE_METRICS,
    ReportKind.SUBSCRIPTIONS: SUBSCRIPTION_METRICS,
    ReportKind.RETAINED_INSTALLERS: STORE_PERFORMANCE_METRICS,
    ReportKind.BUYERS_7D: STORE_PERFORMANCE_METRICS,
}


def _rows_for_metrics(
    source: bytes | IO[bytes] | str,
    *,
    package_name: str,
    metrics: dict[str, tuple[str, ...]],
    dimension: str,
    source_label: str,
) -> list[ReportRow]:
    """Normalize a stats-shaped CSV into ReportRows."""
    out: list[ReportRow] = []
    resolved: dict[str, str | None] | None = None
    dimension_column: str | None = None

    for raw in iter_report_rows(source):
        if resolved is None:
            resolved = {
                metric: find_column(raw, cands, source=source_label, required=False)
                for metric, cands in metrics.items()
            }
            if not any(resolved.values()):
                raise missing_column_error(
                    [c[0] for c in metrics.values()], sorted(raw), source=source_label
                )
            if dimension != "overview":
                dimension_column = find_column(
                    raw,
                    DIMENSION_COLUMNS.get(dimension, (dimension,)),
                    source=source_label,
                    required=False,
                )

        date_column = find_column(raw, _DATE_COLUMNS, source=source_label, required=False)
        period = parse_date(raw.get(date_column or "", ""))
        if period is None:
            continue
        dim_value = raw.get(dimension_column or "") or None

        for metric, column in resolved.items():
            if column is None:
                continue
            value = parse_float(raw.get(column))
            if value is None:
                continue
            out.append(
                ReportRow(
                    package_name=package_name,
                    period=period,
                    metric=metric,
                    value=value,
                    dimension=None if dimension == "overview" else dimension,
                    dimension_value=dim_value,
                )
            )
    return out


def summarize(rows: Iterable[ReportRow]) -> dict[str, float]:
    """Sum every metric across rows — the shape most tools return."""
    totals: dict[str, float] = {}
    for row in rows:
        totals[row.metric] = totals.get(row.metric, 0.0) + row.value
    return totals


def parse_stats_report(
    source: bytes | IO[bytes] | str,
    *,
    kind: ReportKind | str,
    package_name: str,
    month: str | date,
    dimension: str = "overview",
    today: date | None = None,
    source_object: str | None = None,
) -> Report:
    """Parse any per-app stats-shaped report into a normalized Report."""
    kind = ReportKind(kind)
    metrics = METRICS_BY_KIND.get(kind)
    if metrics is None:
        raise ValidationError(
            f"{kind.value} is not a stats-shaped report.",
            remedy="Use parse_earnings for earnings, or read the rows directly.",
        )
    label = source_object or report_object_prefix(kind, month, package_name=package_name)
    rows = _rows_for_metrics(
        source,
        package_name=package_name,
        metrics=metrics,
        dimension=dimension,
        source_label=label,
    )
    return Report(
        rows=rows,
        freshness=report_freshness(kind, month, today=today, row_count=len(rows)),
        source_object=label,
        totals=summarize(rows),
    )


# --- Earnings ----------------------------------------------------------------


def belongs_to_package(product_id: str, package_name: str) -> bool:
    """True when an earnings row's product id belongs to ``package_name``.

    The product id is the package for an app sale and ``<package>.<sku>`` for an
    in-app product, so a prefix test is needed — but it has to stop at a segment
    boundary. A bare ``startswith`` attributes ``com.acme.freemium``'s revenue to
    ``com.acme.free``, which is a wrong money answer that looks entirely
    plausible. Only ``.`` and ``:`` count as boundaries; both appear as SKU
    separators, and neither can occur inside a package segment.
    """
    if product_id == package_name:
        return True
    return product_id.startswith((f"{package_name}.", f"{package_name}:"))


_EARNINGS_AMOUNT_COLUMNS = (
    "Amount (Merchant Currency)",
    "Merchant Amount",
    "Amount Merchant Currency",
)
_EARNINGS_CURRENCY_COLUMNS = ("Merchant Currency", "Currency")
_EARNINGS_PRODUCT_COLUMNS = ("Product id", "Product ID", "Package Name", "Sku Id")


def parse_earnings(
    source: bytes | IO[bytes] | str,
    *,
    month: str | date,
    package_name: str | None = None,
    today: date | None = None,
    source_object: str | None = None,
) -> Report:
    """Parse an ``earnings/earnings_<YYYYMM>*.csv`` object.

    Earnings are account-wide: one row per transaction across every app. Rows
    carry the product id as ``package_name`` so callers can group by app; pass
    ``package_name`` to filter to one app.

    Amounts come from the **merchant-currency** column — what actually lands in
    the payout — and the currency is carried per row rather than assumed to be
    USD. Callers must never sum across currencies.
    """
    label = source_object or f"{report_object_prefix(ReportKind.EARNINGS, month)}*.csv"
    rows: list[ReportRow] = []
    amount_column: str | None = None
    currency_column: str | None = None
    product_column: str | None = None
    date_column: str | None = None
    resolved = False

    for raw in iter_report_rows(source):
        if not resolved:
            amount_column = find_column(raw, _EARNINGS_AMOUNT_COLUMNS, source=label)
            claimed = [c for c in (amount_column,) if c]
            currency_column = find_column(
                raw, _EARNINGS_CURRENCY_COLUMNS, source=label, required=False, exclude=claimed
            )
            claimed += [c for c in (currency_column,) if c]
            product_column = find_column(
                raw, _EARNINGS_PRODUCT_COLUMNS, source=label, required=False, exclude=claimed
            )
            claimed += [c for c in (product_column,) if c]
            date_column = find_column(
                raw, _DATE_COLUMNS, source=label, required=False, exclude=claimed
            )
            resolved = True

        value = parse_float(raw.get(amount_column or ""))
        if value is None:
            continue
        product_id = raw.get(product_column or "") or "account"
        if package_name and not belongs_to_package(product_id, package_name):
            continue
        period = parse_date(raw.get(date_column or "")) or month_bounds(month)[0]
        rows.append(
            ReportRow(
                package_name=package_name or product_id,
                period=period,
                metric="earnings",
                value=value,
                currency=(raw.get(currency_column or "") or None),
            )
        )

    return Report(
        rows=rows,
        freshness=report_freshness(ReportKind.EARNINGS, month, today=today, row_count=len(rows)),
        source_object=label,
        totals=summarize(rows),
    )


def earnings_by_currency(report: Report) -> dict[str, float]:
    """Total earnings per currency.

    Deliberately never returns one number. Summing 1000 JPY and 10 EUR into
    "1010" is a wrong answer that looks like a right one, and this module has no
    exchange rates and no business inventing any.
    """
    totals: dict[str, float] = {}
    for row in report.rows:
        if row.metric != "earnings":
            continue
        code = row.currency or "unknown"
        totals[code] = totals.get(code, 0.0) + row.value
    return totals


def earnings_by_app(report: Report) -> dict[str, dict[str, float]]:
    """Earnings per app, then per currency within each app."""
    out: dict[str, dict[str, float]] = {}
    for row in report.rows:
        if row.metric != "earnings":
            continue
        per_app = out.setdefault(row.package_name, {})
        code = row.currency or "unknown"
        per_app[code] = per_app.get(code, 0.0) + row.value
    return out


def group_rows(report: Report, *, top: int = 25) -> list[dict[str, Any]]:
    """Collapse rows to per-dimension-value totals, largest first.

    A month of country-sliced installs is a few thousand rows; an agent needs the
    shape, not the raw file.
    """
    grouped: dict[tuple[str, str], float] = {}
    for row in report.rows:
        key = (row.metric, row.dimension_value or "(overview)")
        grouped[key] = grouped.get(key, 0.0) + row.value
    ordered = sorted(grouped.items(), key=lambda kv: abs(kv[1]), reverse=True)
    return [
        {"metric": metric, "dimension_value": value, "total": total}
        for (metric, value), total in ordered[:top]
    ]
