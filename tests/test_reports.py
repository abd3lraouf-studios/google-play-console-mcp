"""Tests for Cloud Storage bulk-report parsing and fetching."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
from google.cloud.exceptions import Forbidden, NotFound

import google_play_console_mcp.reports_bucket as bucket_mod
import google_play_console_mcp.server as server
from google_play_console_mcp.client import PlayStoreClient
from google_play_console_mcp.errors import PlayConsoleError, ValidationError
from google_play_console_mcp.models import Report, ReportRow
from google_play_console_mcp.reports import (
    ACCOUNT_LEVEL_KINDS,
    FAMILIES,
    ReportKind,
    belongs_to_package,
    decode_report_bytes,
    earnings_by_app,
    earnings_by_currency,
    find_column,
    group_rows,
    iter_report_rows,
    list_families,
    month_bounds,
    normalize_header,
    normalize_month,
    parse_date,
    parse_earnings,
    parse_float,
    parse_stats_report,
    read_headers,
    report_freshness,
    report_object_prefix,
    summarize,
)

PKG = "com.example.app"
TODAY = date(2026, 8, 20)


def _utf16(text: str) -> bytes:
    """Encode as UTF-16 with a BOM, the way Play actually writes these files."""
    return text.encode("utf-16")


INSTALLS_CSV = (
    "Date,Package Name,Daily Device Installs,Daily Device Uninstalls,Active Device Installs\n"
    "2026-07-01,com.example.app,120,5,1000\n"
    "2026-07-02,com.example.app,140,8,1132\n"
)

COUNTRY_CSV = (
    "Date,Package Name,Country,Daily Device Installs\n"
    "2026-07-01,com.example.app,US,80\n"
    "2026-07-01,com.example.app,DE,40\n"
    "2026-07-02,com.example.app,US,90\n"
)

# Play quotes the transaction date because it contains a comma.
EARNINGS_CSV = (
    "Transaction Date,Product id,Amount (Merchant Currency),Merchant Currency\n"
    '"Jul 1, 2026",com.example.app,4.99,EUR\n'
    '"Jul 2, 2026",com.example.app.pro_upgrade,9.99,EUR\n'
    '"Jul 3, 2026",com.example.app,1200,JPY\n'
    '"Jul 4, 2026",com.other.app,50.00,EUR\n'
)


def _blob(name: str) -> MagicMock:
    """A blob mock whose .name is the object path.

    MagicMock(name=...) sets the mock's own repr name, not an attribute, so it
    has to be assigned afterwards.
    """
    blob = MagicMock()
    blob.name = name
    return blob


def _client(storage_client: MagicMock | None = None) -> PlayStoreClient:
    client = PlayStoreClient(credentials_json={"type": "service_account"})
    client._storage_client = storage_client or MagicMock()
    client._reports_bucket = "pubsite_prod_rev_123"
    return client


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def test_decode_handles_utf16_with_bom():
    """The whole point: Play writes UTF-16, and naive UTF-8 decoding fails."""
    assert decode_report_bytes(_utf16("Date,Value\n")) == "Date,Value\n"


def test_decode_handles_utf16_big_endian_and_utf8_bom():
    assert decode_report_bytes(b"\xfe\xff" + "hi".encode("utf-16-be")) == "hi"
    assert decode_report_bytes(b"\xef\xbb\xbfhi") == "hi"


def test_decode_handles_bomless_utf16():
    """Dense NUL bytes mean 16-bit code units with no BOM to announce them."""
    assert decode_report_bytes("Date,Value\n".encode("utf-16-le")) == "Date,Value\n"
    assert decode_report_bytes("Date,Value\n".encode("utf-16-be")) == "Date,Value\n"


def test_decode_falls_back_to_utf8():
    assert decode_report_bytes(b"Date,Value\n") == "Date,Value\n"


def test_decode_rejects_undecodable_bytes_with_a_remedy():
    with pytest.raises(ValidationError, match="neither valid UTF-16 nor UTF-8") as exc:
        decode_report_bytes(b"\xc3\x28\xa0\xa1")
    assert "Re-download" in exc.value.remedy


# ---------------------------------------------------------------------------
# Headers and rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Daily Device Installs", "daily_device_installs"),
        ("Amount (Merchant Currency)", "amount_merchant_currency"),
        ("  Country / Region  ", "country_region"),
    ],
)
def test_normalize_header(raw, expected):
    assert normalize_header(raw) == expected


def test_iter_report_rows_keys_by_normalized_header():
    rows = list(iter_report_rows(_utf16(INSTALLS_CSV)))
    assert len(rows) == 2
    assert rows[0]["daily_device_installs"] == "120"


def test_iter_report_rows_skips_blank_lines_and_pads_short_rows():
    rows = list(iter_report_rows("A,B,C\n1,2\n\n   ,  ,  \n"))
    assert rows == [{"a": "1", "b": "2", "c": ""}]


def test_iter_report_rows_on_an_empty_file():
    assert list(iter_report_rows(b"")) == []
    assert read_headers(b"") == []


def test_iter_report_rows_accepts_a_binary_file_object(tmp_path):
    path = tmp_path / "r.csv"
    path.write_bytes(_utf16(INSTALLS_CSV))
    with path.open("rb") as handle:
        assert len(list(iter_report_rows(handle))) == 2


def test_read_headers_reports_what_is_actually_there():
    assert "daily_device_installs" in read_headers(_utf16(INSTALLS_CSV))


# ---------------------------------------------------------------------------
# Column resolution
# ---------------------------------------------------------------------------


def test_find_column_exact_match():
    row = {"daily_device_installs": "1"}
    assert find_column(row, ["Daily Device Installs"]) == "daily_device_installs"


def test_find_column_survives_a_google_rename():
    """"Daily Device Installs" -> "Daily Device Installs (new)" must still resolve."""
    row = {"daily_device_installs_new": "1"}
    assert find_column(row, ["Daily Device Installs"]) == "daily_device_installs_new"


def test_find_column_never_matches_on_substrings():
    """Substring matching would confuse Install events with Uninstall events."""
    row = {"uninstall_events": "5"}
    assert find_column(row, ["Install events"], required=False) is None


def test_find_column_requires_exact_match_for_single_token_candidates():
    """"Device" is too ambiguous to fuzzy-match against a wider header."""
    row = {"device_model_name": "Pixel"}
    assert find_column(row, ["Device"], required=False) is None


def test_find_column_raises_with_the_headers_actually_present():
    row = {"some_other_column": "1"}
    with pytest.raises(ValidationError) as exc:
        find_column(row, ["Daily Device Installs"], source="installs.csv")
    assert exc.value.details["available_headers"] == ["some_other_column"]
    assert "Google changes report columns" in exc.value.message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,234", 1234.0), ("12.5%", 12.5), ("", None), ("-", None), ("N/A", None), ("x", None)],
)
def test_parse_float(raw, expected):
    assert parse_float(raw) == expected
    assert parse_float(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-01", date(2026, 7, 1)),
        ("Jul 1, 2026", date(2026, 7, 1)),
        ("07/01/2026", date(2026, 7, 1)),
        ("20260701", date(2026, 7, 1)),
        ("not a date", None),
        ("", None),
    ],
)
def test_parse_date_handles_every_format_play_uses(raw, expected):
    assert parse_date(raw) == expected


# ---------------------------------------------------------------------------
# Months and object paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["2026-07", "202607", "2026/07"])
def test_normalize_month_accepts_the_usual_spellings(raw):
    assert normalize_month(raw) == "202607"


def test_normalize_month_from_a_date():
    assert normalize_month(date(2026, 7, 15)) == "202607"


@pytest.mark.parametrize("raw", ["2026", "2026-13", "nonsense"])
def test_normalize_month_rejects_nonsense(raw):
    with pytest.raises(ValidationError, match="Invalid month"):
        normalize_month(raw)


def test_month_bounds_handles_february_and_december():
    assert month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))


def test_report_object_prefix_per_app_and_account_level():
    assert (
        report_object_prefix("installs", "2026-07", package_name=PKG)
        == f"stats/installs/installs_{PKG}_202607"
    )
    assert report_object_prefix("earnings", "2026-07") == "earnings/earnings_202607"
    assert report_object_prefix("earnings") == "earnings/"
    assert report_object_prefix("installs") == "stats/installs/installs_"


def test_store_conversion_and_store_performance_share_a_directory():
    """They are told apart only by the file prefix, which is easy to get wrong."""
    perf = report_object_prefix("store_performance", "2026-07", package_name=PKG)
    conv = report_object_prefix("store_conversion", "2026-07", package_name=PKG)
    assert perf.startswith("stats/store_performance/total_store_performance_")
    assert conv.startswith("stats/store_performance/store_performance_")
    assert perf != conv


def test_families_are_all_described_and_discontinued_ones_say_so():
    families = list_families()
    assert len(families) == len(ReportKind)
    assert all(f.description for f in families)
    retired = {f.name: f.discontinued for f in families if f.discontinued}
    assert "retained_installers" in retired
    assert "2021-06" in retired["retained_installers"]
    assert "buyers_7d" in retired


def test_account_level_kinds_are_the_money_reports():
    assert {ReportKind.EARNINGS, ReportKind.SALES} == ACCOUNT_LEVEL_KINDS


# ---------------------------------------------------------------------------
# Freshness — the guard against reporting an absence as a zero
# ---------------------------------------------------------------------------


def test_freshness_flags_unpublished_earnings_rather_than_reporting_zero():
    fresh = report_freshness("earnings", "2026-08", today=date(2026, 9, 1))
    assert fresh.is_complete is False
    assert "not published yet" in fresh.caveat
    assert "not because revenue was zero" in fresh.caveat


def test_freshness_accepts_earnings_after_the_publication_day():
    assert report_freshness("earnings", "2026-07", today=date(2026, 8, 20)).is_complete is True


def test_freshness_flags_a_month_that_has_not_started():
    fresh = report_freshness("installs", "2026-12", today=TODAY)
    assert fresh.is_complete is False
    assert "has not started yet" in fresh.caveat
    assert fresh.as_of is None


def test_freshness_flags_a_month_still_filling_in():
    fresh = report_freshness("installs", "2026-08", today=date(2026, 8, 15))
    assert fresh.is_complete is False
    assert "still filling in" in fresh.caveat
    assert fresh.lag_days == 7


def test_freshness_of_a_closed_month_with_rows_is_complete():
    fresh = report_freshness("installs", "2026-07", today=TODAY, row_count=10)
    assert fresh.is_complete is True
    assert fresh.caveat is None
    assert fresh.as_of == date(2026, 7, 31)


def test_freshness_of_a_closed_month_with_no_rows_says_the_report_may_be_missing():
    fresh = report_freshness("installs", "2026-07", today=TODAY, row_count=0)
    assert fresh.is_complete is False
    assert "reporting window has closed" in fresh.caveat


# ---------------------------------------------------------------------------
# Stats parsing
# ---------------------------------------------------------------------------


def test_parse_installs_totals_every_metric():
    report = parse_stats_report(
        _utf16(INSTALLS_CSV), kind="installs", package_name=PKG, month="2026-07", today=TODAY
    )
    assert report.totals["daily_device_installs"] == pytest.approx(260.0)
    assert report.totals["daily_device_uninstalls"] == pytest.approx(13.0)
    assert report.totals["active_device_installs"] == pytest.approx(2132.0)
    assert report.freshness.is_complete is True


def test_parse_installs_by_country_carries_the_dimension():
    report = parse_stats_report(
        _utf16(COUNTRY_CSV),
        kind="installs",
        package_name=PKG,
        month="2026-07",
        dimension="country",
        today=TODAY,
    )
    assert {r.dimension for r in report.rows} == {"country"}
    assert {r.dimension_value for r in report.rows} == {"US", "DE"}
    assert report.totals["daily_device_installs"] == pytest.approx(210.0)


def test_parse_skips_rows_without_a_usable_date():
    csv_text = "Date,Daily Device Installs\n,50\n2026-07-01,10\n"
    report = parse_stats_report(
        csv_text, kind="installs", package_name=PKG, month="2026-07", today=TODAY
    )
    assert report.totals["daily_device_installs"] == pytest.approx(10.0)


def test_parse_raises_when_no_metric_column_resolves():
    csv_text = "Date,Something Unrelated\n2026-07-01,5\n"
    with pytest.raises(ValidationError) as exc:
        parse_stats_report(csv_text, kind="installs", package_name=PKG, month="2026-07")
    assert "available_headers" in exc.value.details


def test_parse_stats_report_rejects_a_non_stats_kind():
    with pytest.raises(ValidationError, match="not a stats-shaped report"):
        parse_stats_report("a,b\n1,2\n", kind="earnings", package_name=PKG, month="2026-07")


def test_summarize_and_group_rows():
    report = parse_stats_report(
        _utf16(COUNTRY_CSV),
        kind="installs",
        package_name=PKG,
        month="2026-07",
        dimension="country",
        today=TODAY,
    )
    assert summarize(report.rows)["daily_device_installs"] == pytest.approx(210.0)
    grouped = group_rows(report, top=1)
    assert len(grouped) == 1
    assert grouped[0]["dimension_value"] == "US"
    assert grouped[0]["total"] == pytest.approx(170.0)


# ---------------------------------------------------------------------------
# Earnings — the money path
# ---------------------------------------------------------------------------


def test_belongs_to_package_stops_at_a_segment_boundary():
    """A bare startswith would give com.acme.free the revenue of com.acme.freemium."""
    assert belongs_to_package("com.acme.free", "com.acme.free") is True
    assert belongs_to_package("com.acme.free.pro", "com.acme.free") is True
    assert belongs_to_package("com.acme.free:sub", "com.acme.free") is True
    assert belongs_to_package("com.acme.freemium", "com.acme.free") is False


def test_parse_earnings_uses_merchant_currency_and_keeps_currencies_apart():
    report = parse_earnings(_utf16(EARNINGS_CSV), month="2026-07", today=TODAY)
    by_currency = earnings_by_currency(report)
    assert by_currency["EUR"] == pytest.approx(64.98)
    assert by_currency["JPY"] == pytest.approx(1200.0)
    assert "total" not in by_currency  # never summed across currencies


def test_parse_earnings_filters_to_one_app_including_its_iap_skus():
    report = parse_earnings(_utf16(EARNINGS_CSV), month="2026-07", package_name=PKG, today=TODAY)
    assert len(report.rows) == 3  # app + pro_upgrade + JPY row, not com.other.app
    assert earnings_by_currency(report)["EUR"] == pytest.approx(14.98)


def test_earnings_by_app_groups_then_splits_by_currency():
    report = parse_earnings(_utf16(EARNINGS_CSV), month="2026-07", today=TODAY)
    by_app = earnings_by_app(report)
    assert by_app["com.other.app"]["EUR"] == pytest.approx(50.0)
    assert by_app[PKG]["JPY"] == pytest.approx(1200.0)


def test_parse_earnings_falls_back_to_the_month_start_without_a_date_column():
    csv_text = "Product id,Amount (Merchant Currency),Merchant Currency\ncom.example.app,5,EUR\n"
    report = parse_earnings(csv_text, month="2026-07", today=TODAY)
    assert report.rows[0].period == date(2026, 7, 1)


def test_parse_earnings_requires_an_amount_column():
    with pytest.raises(ValidationError):
        parse_earnings("Product id,Currency\ncom.example.app,EUR\n", month="2026-07")


def test_parse_earnings_skips_rows_with_no_amount():
    csv_text = (
        "Product id,Amount (Merchant Currency),Merchant Currency\n"
        "com.example.app,,EUR\ncom.example.app,3,EUR\n"
    )
    assert len(parse_earnings(csv_text, month="2026-07", today=TODAY).rows) == 1


def test_earnings_helpers_ignore_non_earnings_rows():
    report = Report(
        rows=[ReportRow(package_name=PKG, period=date(2026, 7, 1), metric="installs", value=5)]
    )
    assert earnings_by_currency(report) == {}
    assert earnings_by_app(report) == {}


def test_earnings_without_a_currency_column_is_labelled_unknown():
    csv_text = "Product id,Amount (Merchant Currency)\ncom.example.app,5\n"
    report = parse_earnings(csv_text, month="2026-07", today=TODAY)
    assert earnings_by_currency(report) == {"unknown": pytest.approx(5.0)}


# ---------------------------------------------------------------------------
# Bucket resolution
# ---------------------------------------------------------------------------


def test_bucket_name_strips_the_gs_prefix(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_REPORTS_BUCKET", "gs://pubsite_prod_rev_123/")
    assert bucket_mod.bucket_name() == "pubsite_prod_rev_123"


def test_bucket_name_explains_where_to_find_it_when_unset(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLAY_MCP_REPORTS_BUCKET", raising=False)
    with pytest.raises(ValidationError) as exc:
        bucket_mod.bucket_name()
    assert "Copy Cloud Storage URI" in exc.value.remedy
    assert "ACCOUNT-level" in exc.value.remedy


def test_month_is_settled_only_after_the_publication_window():
    assert bucket_mod._month_is_settled(None) is False
    assert bucket_mod._month_is_settled("2020-01") is True
    assert bucket_mod._month_is_settled("2099-01") is False


def test_classify_maps_storage_errors_to_actionable_remedies():
    forbidden = bucket_mod._classify(Forbidden("no"), "b", "o")
    assert "ACCOUNT-level" in forbidden.remedy
    missing = bucket_mod._classify(NotFound("no"), "b", "o")
    assert "bucket id is wrong" in missing.remedy
    other = bucket_mod._classify(RuntimeError("boom"), "b", "o")
    assert "storage.googleapis.com" in other.remedy


# ---------------------------------------------------------------------------
# Bucket fetching
# ---------------------------------------------------------------------------


def test_list_report_objects_returns_names():
    storage = MagicMock()
    storage.list_blobs.return_value = [_blob("stats/installs/a.csv")]
    assert _client(storage).list_report_objects("stats/") == ["stats/installs/a.csv"]


def test_list_report_objects_wraps_a_permission_error():
    storage = MagicMock()
    storage.list_blobs.side_effect = Forbidden("denied")
    with pytest.raises(PlayConsoleError, match="Access denied"):
        _client(storage).list_report_objects("stats/")


def test_fetch_report_object_caches_a_settled_month(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    storage = MagicMock()
    storage.bucket.return_value.blob.return_value.download_as_bytes.return_value = b"data"
    client = _client(storage)

    assert client.fetch_report_object("o.csv", month="2020-01") == b"data"
    assert client.fetch_report_object("o.csv", month="2020-01") == b"data"
    storage.bucket.return_value.blob.return_value.download_as_bytes.assert_called_once()


def test_fetch_report_object_wraps_a_missing_object(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    storage = MagicMock()
    storage.bucket.return_value.blob.return_value.download_as_bytes.side_effect = NotFound("gone")
    with pytest.raises(PlayConsoleError, match="No object at"):
        _client(storage).fetch_report_object("o.csv")


def test_get_stats_report_selects_the_requested_dimension_slice(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    storage = MagicMock()
    storage.list_blobs.return_value = [
        _blob(f"stats/installs/installs_{PKG}_202607_overview.csv"),
        _blob(f"stats/installs/installs_{PKG}_202607_country.csv"),
    ]
    storage.bucket.return_value.blob.return_value.download_as_bytes.return_value = _utf16(
        COUNTRY_CSV
    )

    report = _client(storage).get_stats_report("installs", PKG, "2026-07", dimension="country")

    assert storage.bucket.return_value.blob.call_args.args[0].endswith("_country.csv")
    assert report.totals["daily_device_installs"] == pytest.approx(210.0)


def test_get_stats_report_excludes_the_total_sibling_for_store_conversion(monkeypatch, tmp_path):
    """total_store_performance_ lives in the same directory and matches the prefix."""
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    storage = MagicMock()
    storage.list_blobs.return_value = [
        _blob(f"stats/store_performance/store_performance_{PKG}_202607_country.csv"),
        _blob(f"stats/store_performance/total_store_performance_{PKG}_202607_country.csv"),
    ]
    storage.bucket.return_value.blob.return_value.download_as_bytes.return_value = _utf16(
        "Date,Country,Store Listing Visitors\n2026-07-01,US,500\n"
    )

    _client(storage).get_stats_report("store_conversion", PKG, "2026-07", dimension="country")

    assert "total_store_performance_" not in storage.bucket.return_value.blob.call_args.args[0]


def test_get_stats_report_returns_an_explained_empty_report_when_nothing_is_published():
    """An absent object is not an error — but the zero must not read as a measurement."""
    storage = MagicMock()
    storage.list_blobs.return_value = []
    report = _client(storage).get_stats_report("installs", PKG, "2026-12")
    assert report.rows == []
    assert report.freshness.is_complete is False
    assert report.freshness.caveat


def test_get_stats_report_rejects_an_unknown_dimension():
    with pytest.raises(ValidationError, match="not a dimension"):
        _client().get_stats_report("installs", PKG, "2026-07", dimension="phase-of-moon")


def test_get_stats_report_merges_multiple_objects(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    storage = MagicMock()
    storage.list_blobs.return_value = [
        _blob("stats/installs/a_overview.csv"),
        _blob("stats/installs/b_overview.csv"),
    ]
    storage.bucket.return_value.blob.return_value.download_as_bytes.return_value = _utf16(
        INSTALLS_CSV
    )
    report = _client(storage).get_stats_report("installs", PKG, "2026-07")
    assert report.totals["daily_device_installs"] == pytest.approx(520.0)


def test_get_earnings_merges_and_returns_empty_when_unpublished(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    storage = MagicMock()
    storage.list_blobs.return_value = []
    empty = _client(storage).get_earnings("2026-08")
    assert empty.rows == []
    assert empty.freshness.caveat

    storage.list_blobs.return_value = [_blob("earnings/earnings_202607_1.csv")]
    storage.bucket.return_value.blob.return_value.download_as_bytes.return_value = _utf16(
        EARNINGS_CSV
    )
    report = _client(storage).get_earnings("2026-07")
    assert len(report.rows) == 4


def test_check_reports_access_reports_the_problem_rather_than_raising(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLAY_MCP_REPORTS_BUCKET", raising=False)
    client = PlayStoreClient(credentials_json={"type": "service_account"})
    client._reports_bucket = None
    result = client.check_reports_access()
    assert result["ok"] is False
    assert "Copy Cloud Storage URI" in result["remedy"]


def test_check_reports_access_reports_a_permission_failure():
    storage = MagicMock()
    storage.list_blobs.side_effect = Forbidden("denied")
    result = _client(storage).check_reports_access()
    assert result["ok"] is False
    assert result["bucket"] == "pubsite_prod_rev_123"


def test_check_reports_access_succeeds_and_summarises_prefixes():
    storage = MagicMock()
    storage.list_blobs.return_value = [
        _blob("stats/installs/a.csv"),
        _blob("stats/ratings/b.csv"),
    ]
    result = _client(storage).check_reports_access()
    assert result["ok"] is True
    assert result["prefixes_seen"] == ["stats/installs/", "stats/ratings/"]


def test_storage_client_needs_credentials():
    client = PlayStoreClient()
    client._credentials_json = None
    client._credentials_path = None
    client._storage_client = None
    with pytest.raises(PlayConsoleError, match="No valid credentials"):
        client._get_storage_client()


# ---------------------------------------------------------------------------
# Server tools
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock(spec=PlayStoreClient)
    monkeypatch.setattr(server, "get_client_from_context", lambda: client)
    return client


def _stats_report() -> Report:
    return parse_stats_report(
        _utf16(COUNTRY_CSV),
        kind="installs",
        package_name=PKG,
        month="2026-07",
        dimension="country",
        today=TODAY,
    )


def test_tool_list_report_families_flags_the_discontinued_ones():
    families = server.list_report_families()
    assert len(families) == len(ReportKind)
    retired = [f for f in families if f["discontinued"]]
    assert {f["name"] for f in retired} == {"retained_installers", "buyers_7d"}


def test_tool_check_reports_access(mock_client: MagicMock):
    mock_client.check_reports_access.return_value = {"ok": True, "bucket": "b"}
    assert server.check_reports_access()["ok"] is True


def test_tool_list_report_objects(mock_client: MagicMock):
    mock_client.list_report_objects.return_value = ["stats/installs/a.csv"]
    assert server.list_report_objects(prefix="stats/")["objects"] == ["stats/installs/a.csv"]


def test_tool_list_report_objects_returns_the_error_object(mock_client: MagicMock):
    mock_client.list_report_objects.side_effect = ValidationError("no bucket", remedy="set it")
    assert server.list_report_objects(prefix="stats/")["remedy"] == "set it"


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        (lambda: server.get_installs_report, "installs"),
        (lambda: server.get_ratings_report, "ratings"),
        (lambda: server.get_crashes_report, "crashes"),
        (lambda: server.get_store_performance_report, "store_performance"),
        (lambda: server.get_store_conversion_report, "store_conversion"),
    ],
)
def test_stats_tools_pass_their_kind_and_carry_freshness(tool, kind, mock_client: MagicMock):
    mock_client.get_stats_report.return_value = _stats_report()
    result = tool()(package_name=PKG, month="2026-07")
    assert mock_client.get_stats_report.call_args.args[0] == kind
    assert "freshness" in result
    assert result["totals"]["daily_device_installs"] == pytest.approx(210.0)
    assert result["breakdown"]


def test_stats_tool_returns_the_error_object(mock_client: MagicMock):
    mock_client.get_stats_report.side_effect = ValidationError("bad month", remedy="use YYYY-MM")
    assert server.get_installs_report(package_name=PKG, month="nope")["remedy"] == "use YYYY-MM"


def test_tool_get_subscriptions_report_uses_the_country_dimension(mock_client: MagicMock):
    mock_client.get_stats_report.return_value = _stats_report()
    server.get_subscriptions_report(package_name=PKG, month="2026-07")
    assert mock_client.get_stats_report.call_args.kwargs["dimension"] == "country"


def test_tool_get_search_terms_ranks_by_acquisitions(mock_client: MagicMock):
    mock_client.get_stats_report.return_value = _stats_report()
    result = server.get_search_terms(package_name=PKG, month="2026-07", top=1)
    assert mock_client.get_stats_report.call_args.kwargs["dimension"] == "search_term"
    assert len(result["search_terms"]) == 1
    assert "freshness" in result


def test_tool_get_search_terms_returns_the_error_object(mock_client: MagicMock):
    mock_client.get_stats_report.side_effect = ValidationError("nope", remedy="fix")
    assert server.get_search_terms(package_name=PKG, month="2026-07")["remedy"] == "fix"


def test_tool_get_earnings_report_never_sums_across_currencies(mock_client: MagicMock):
    mock_client.get_earnings.return_value = parse_earnings(
        _utf16(EARNINGS_CSV), month="2026-07", today=TODAY
    )
    result = server.get_earnings_report(month="2026-07")
    assert result["by_currency"] == {
        "EUR": pytest.approx(64.98),
        "JPY": pytest.approx(1200.0),
    }
    assert result["transaction_count"] == 4
    assert "by_app" in result


def test_tool_get_earnings_report_returns_the_error_object(mock_client: MagicMock):
    mock_client.get_earnings.side_effect = ValidationError("no bucket", remedy="set it")
    assert server.get_earnings_report(month="2026-07")["remedy"] == "set it"


def test_report_tools_are_reads_and_work_in_read_only_mode(mock_client: MagicMock):
    mock_client.get_stats_report.return_value = _stats_report()
    server.set_read_only(True)
    try:
        assert "totals" in server.get_installs_report(package_name=PKG, month="2026-07")
    finally:
        server.set_read_only(False)


def test_families_registry_covers_every_kind():
    assert set(FAMILIES) == set(ReportKind)


def test_report_row_and_freshness_defaults():
    row = ReportRow(package_name=PKG, period=date(2026, 7, 1), metric="m", value=1.0)
    assert row.dimension is None
    assert row.currency is None
    assert Report().freshness.is_complete is True


def test_report_family_model_defaults():
    assert FAMILIES[ReportKind.INSTALLS].discontinued is None


def test_any_stats_kind_can_be_parsed_by_the_shared_helper():
    csv_text = "Date,Country,New Subscriptions\n2026-07-01,US,12\n"
    report = parse_stats_report(
        csv_text,
        kind="subscriptions",
        package_name=PKG,
        month="2026-07",
        dimension="country",
        today=TODAY,
    )
    assert report.totals["new_subscriptions"] == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# Remaining edge paths
# ---------------------------------------------------------------------------


def test_report_object_prefix_without_a_month_for_a_per_app_kind():
    assert (
        report_object_prefix("installs", package_name=PKG) == f"stats/installs/installs_{PKG}_"
    )


def test_parse_skips_a_metric_cell_that_is_not_numeric():
    csv_text = "Date,Daily Device Installs,Daily Device Uninstalls\n2026-07-01,N/A,4\n"
    report = parse_stats_report(
        csv_text, kind="installs", package_name=PKG, month="2026-07", today=TODAY
    )
    assert "daily_device_installs" not in report.totals
    assert report.totals["daily_device_uninstalls"] == pytest.approx(4.0)


def test_storage_client_is_built_once_and_cached(monkeypatch):
    built = MagicMock()
    monkeypatch.setattr(bucket_mod.storage, "Client", MagicMock(return_value=built))
    monkeypatch.setattr(
        bucket_mod, "load_service_account_credentials", MagicMock(return_value=MagicMock())
    )
    client = PlayStoreClient(credentials_json={"type": "service_account"})
    client._storage_client = None
    assert client._get_storage_client() is built
    assert client._get_storage_client() is built
    bucket_mod.storage.Client.assert_called_once()


def test_a_recent_month_is_refetched_once_the_cache_entry_ages(monkeypatch, tmp_path):
    """A settled month caches forever; a month still filling in must not."""
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    storage = MagicMock()
    storage.bucket.return_value.blob.return_value.download_as_bytes.return_value = b"fresh"
    client = _client(storage)

    assert client.fetch_report_object("o.csv", month="2099-01") == b"fresh"
    monkeypatch.setattr(bucket_mod, "CACHE_TTL_RECENT", bucket_mod.timedelta(seconds=-1))
    assert client.fetch_report_object("o.csv", month="2099-01") == b"fresh"
    assert storage.bucket.return_value.blob.return_value.download_as_bytes.call_count == 2


def test_an_unwritable_cache_never_breaks_a_fetch(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(blocker / "state"))
    storage = MagicMock()
    storage.bucket.return_value.blob.return_value.download_as_bytes.return_value = b"data"
    assert _client(storage).fetch_report_object("o.csv") == b"data"


def test_get_earnings_merges_rows_from_several_objects(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    storage = MagicMock()
    storage.list_blobs.return_value = [
        _blob("earnings/earnings_202607_1.csv"),
        _blob("earnings/earnings_202607_2.csv"),
    ]
    storage.bucket.return_value.blob.return_value.download_as_bytes.return_value = _utf16(
        EARNINGS_CSV
    )
    assert len(_client(storage).get_earnings("2026-07").rows) == 8
