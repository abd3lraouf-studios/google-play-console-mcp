"""Tests for the Play Developer Reporting API surface (app vitals)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

import google_play_console_mcp.server as server
from google_play_console_mcp.client import PlayStoreClient
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
from google_play_console_mcp.reporting import (
    AGGREGATION_PERIODS,
    METRIC_SETS,
    USER_COHORTS,
    ReportingClientError,
    _date_to_api,
    _dimension_value,
    _metric_set_resource,
    _metric_value,
    _nested,
    _parse_freshness,
    _parse_row,
    _to_datetime,
    metric_set_keys,
)

PKG = "com.example.app"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(reporting_service: MagicMock) -> PlayStoreClient:
    client = PlayStoreClient(credentials_json={"type": "service_account"})
    client._reporting_service = reporting_service
    return client


def _vitals(service: MagicMock, *path: str) -> MagicMock:
    node = service
    for step in path:
        node = getattr(node, step).return_value
    return node


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_reporting_app_model():
    app = ReportingApp(name="apps/com.example.app", package_name=PKG)
    assert app.display_name is None


def test_metric_set_freshness_model():
    fresh = MetricSetFreshness()
    assert fresh.aggregation_period is None
    assert fresh.latest_end_time is None


def test_metric_set_info_model():
    info = MetricSetInfo(name="n", metric_set="crash_rate", package_name=PKG)
    assert info.freshness == []


def test_metric_row_model():
    row = MetricRow()
    assert row.start_time is None
    assert row.dimensions == {}
    assert row.metrics == {}


def test_metric_query_result_model():
    result = MetricQueryResult(package_name=PKG, metric_set="crash_rate")
    assert result.rows == []
    assert result.next_page_token is None


def test_anomaly_model():
    anomaly = Anomaly()
    assert anomaly.dimensions == {}
    assert anomaly.metric_value is None


def test_error_issue_model():
    issue = ErrorIssue()
    assert issue.sample_error_reports == []
    assert issue.error_report_count is None


def test_error_report_model():
    report = ErrorReport()
    assert report.report_text is None


def test_release_filter_options_model():
    options = ReleaseFilterOptions(package_name=PKG)
    assert options.tracks == []
    assert options.version_codes == []


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_metric_set_keys_sorted_and_complete():
    keys = metric_set_keys()
    assert keys == sorted(METRIC_SETS)
    assert "crash_rate" in keys
    assert "stuck_background_wakelock_rate" in keys


def test_metric_set_resource_builds_name():
    assert _metric_set_resource(PKG, "crash_rate") == f"apps/{PKG}/crashRateMetricSet"
    assert _metric_set_resource(PKG, "error_counts") == f"apps/{PKG}/errorCountMetricSet"


def test_metric_set_resource_rejects_unknown_key():
    with pytest.raises(ReportingClientError, match="Unknown metric set 'nope'"):
        _metric_set_resource(PKG, "nope")


def test_to_datetime_from_structured_object():
    parsed = _to_datetime({"year": 2026, "month": 8, "day": 23, "hours": 5, "minutes": 6})
    assert parsed == datetime(2026, 8, 23, 5, 6, tzinfo=UTC)


def test_to_datetime_from_rfc3339_string():
    assert _to_datetime("2026-08-23T05:06:07Z") == datetime(2026, 8, 23, 5, 6, 7, tzinfo=UTC)


def test_to_datetime_handles_missing_and_malformed():
    assert _to_datetime(None) is None
    assert _to_datetime("") is None
    assert _to_datetime("not-a-date") is None
    assert _to_datetime({"year": 2026}) is None
    assert _to_datetime({"year": "x", "month": 1, "day": 1}) is None


def test_date_to_api_roundtrip():
    body = _date_to_api(datetime(2026, 8, 23, 5, 6, 7, tzinfo=UTC))
    assert body == {"year": 2026, "month": 8, "day": 23, "hours": 5, "minutes": 6, "seconds": 7}


def test_dimension_value_prefers_typed_fields():
    assert _dimension_value({"stringValue": "US"}) == "US"
    assert _dimension_value({"int64Value": "42"}) == "42"
    assert _dimension_value({"valueLabel": "Pixel"}) == "Pixel"
    assert _dimension_value({"dimension": "countryCode"}) is None


def test_metric_value_parses_decimal_and_fallbacks():
    assert _metric_value({"decimalValue": {"value": "0.0125"}}) == pytest.approx(0.0125)
    assert _metric_value({"decimalValue": {"value": "NaN-ish"}}) == "NaN-ish"
    assert _metric_value({"int64Value": "7"}) == "7"
    assert _metric_value({"value": 3}) == 3
    assert _metric_value({}) is None
    assert _metric_value({"decimalValue": "not-a-dict"}) is None


def test_nested_unwraps_objects_and_passes_scalars():
    assert _nested({"versionCode": 42}, "versionCode") == "42"
    assert _nested({"versionCode": None}, "versionCode") is None
    assert _nested({"other": 1}, "versionCode") is None
    assert _nested("already-a-string", "versionCode") == "already-a-string"
    assert _nested(None, "versionCode") is None


def test_parse_row_maps_dimensions_and_metrics():
    row = _parse_row(
        {
            "startTime": {"year": 2026, "month": 8, "day": 1},
            "dimensions": [
                {"dimension": "countryCode", "stringValue": "US"},
                {"stringValue": "orphan-without-name"},
            ],
            "metrics": [
                {"metric": "crashRate", "decimalValue": {"value": "0.01"}},
                {"decimalValue": {"value": "0.02"}},
            ],
        }
    )
    assert row.dimensions == {"countryCode": "US"}
    assert row.metrics == {"crashRate": pytest.approx(0.01)}
    assert row.start_time == datetime(2026, 8, 1, tzinfo=UTC)


def test_parse_freshness_handles_missing_block():
    assert _parse_freshness({}) == []
    parsed = _parse_freshness(
        {"freshnessInfo": {"freshnesses": [{"aggregationPeriod": "DAILY"}]}}
    )
    assert parsed[0].aggregation_period == "DAILY"
    assert parsed[0].latest_end_time is None


def test_aggregation_periods_and_cohorts_are_the_documented_sets():
    assert {"HOURLY", "DAILY", "FULL_RANGE"} == AGGREGATION_PERIODS
    assert {"OS_PUBLIC", "OS_BETA", "APP_TESTERS"} == USER_COHORTS


# ---------------------------------------------------------------------------
# Service construction
# ---------------------------------------------------------------------------


def test_get_reporting_service_is_cached():
    service = MagicMock()
    client = _client(service)
    assert client._get_reporting_service() is service
    assert client._get_reporting_service() is service


def test_get_reporting_service_builds_with_reporting_scope():
    built = MagicMock()
    client = PlayStoreClient(credentials_json={"type": "service_account"})
    with (
        patch("google_play_console_mcp.credentials.service_account.Credentials.from_service_account_info") as sa,
        patch("google_play_console_mcp.reporting.build", return_value=built) as build_mock,
    ):
        sa.return_value = MagicMock()
        assert client._get_reporting_service() is built

    build_mock.assert_called_once()
    assert build_mock.call_args.args == ("playdeveloperreporting", "v1beta1")
    assert sa.call_args.kwargs["scopes"] == [
        "https://www.googleapis.com/auth/playdeveloperreporting"
    ]


def test_get_reporting_service_without_credentials_raises():
    client = PlayStoreClient()
    client._credentials_json = None
    client._credentials_path = None
    with pytest.raises(ReportingClientError, match="No valid credentials found"):
        client._get_reporting_service()


def test_get_reporting_service_wraps_build_failure():
    client = PlayStoreClient(credentials_json={"type": "service_account"})
    with (
        patch("google_play_console_mcp.credentials.service_account.Credentials.from_service_account_info"),
        patch("google_play_console_mcp.reporting.build", side_effect=RuntimeError("discovery down")),
        pytest.raises(ReportingClientError, match="Failed to initialize Reporting API client"),
    ):
        client._get_reporting_service()


def test_metric_set_resource_api_walks_accessor_path():
    service = MagicMock()
    client = _client(service)
    assert client._metric_set_resource_api("crash_rate") is _vitals(service, "vitals", "crashrate")
    assert client._metric_set_resource_api("error_counts") is _vitals(
        service, "vitals", "errors", "counts"
    )


def test_metric_set_resource_api_rejects_unknown_key():
    client = _client(MagicMock())
    with pytest.raises(ReportingClientError, match="Unknown metric set"):
        client._metric_set_resource_api("nope")


# ---------------------------------------------------------------------------
# Client: apps
# ---------------------------------------------------------------------------


def test_list_reporting_apps_paginates():
    service = MagicMock()
    service.apps.return_value.search.return_value.execute.side_effect = [
        {
            "apps": [{"name": "apps/a", "packageName": "a", "displayName": "A"}],
            "nextPageToken": "t1",
        },
        {"apps": [{"name": "apps/b", "packageName": "b"}]},
    ]
    apps = _client(service).list_reporting_apps(max_results=10)

    assert [a.package_name for a in apps] == ["a", "b"]
    assert apps[0].display_name == "A"
    assert apps[1].display_name is None
    second_call = service.apps.return_value.search.call_args_list[1]
    assert second_call.kwargs["pageToken"] == "t1"


def test_list_reporting_apps_respects_max_results():
    service = MagicMock()
    service.apps.return_value.search.return_value.execute.return_value = {
        "apps": [{"name": f"apps/{i}", "packageName": str(i)} for i in range(5)],
        "nextPageToken": "more",
    }
    apps = _client(service).list_reporting_apps(max_results=3)
    assert len(apps) == 3


def test_get_release_filter_options():
    service = MagicMock()
    service.apps.return_value.fetchReleaseFilterOptions.return_value.execute.return_value = {
        "tracks": [{"displayName": "production"}],
        "versionCodes": [{"versionCode": 42}],
    }
    options = _client(service).get_release_filter_options(PKG)

    assert options.package_name == PKG
    assert options.tracks == [{"displayName": "production"}]
    assert options.version_codes == [{"versionCode": 42}]
    kwargs = service.apps.return_value.fetchReleaseFilterOptions.call_args.kwargs
    assert kwargs["name"] == f"apps/{PKG}"


# ---------------------------------------------------------------------------
# Client: metric sets
# ---------------------------------------------------------------------------


def test_get_metric_set_returns_freshness():
    service = MagicMock()
    _vitals(service, "vitals", "crashrate").get.return_value.execute.return_value = {
        "name": f"apps/{PKG}/crashRateMetricSet",
        "freshnessInfo": {
            "freshnesses": [
                {"aggregationPeriod": "DAILY", "latestEndTime": "2026-08-21T00:00:00Z"}
            ]
        },
    }
    info = _client(service).get_metric_set(PKG, "crash_rate")

    assert info.metric_set == "crash_rate"
    assert info.freshness[0].aggregation_period == "DAILY"
    assert info.freshness[0].latest_end_time == datetime(2026, 8, 21, tzinfo=UTC)


def test_get_metric_set_freshness_covers_every_set_by_default():
    service = MagicMock()
    for _suffix, accessor, _label in METRIC_SETS.values():
        _vitals(service, *accessor).get.return_value.execute.return_value = {"name": "n"}

    results = _client(service).get_metric_set_freshness(PKG)
    assert len(results) == len(METRIC_SETS)


def test_get_metric_set_freshness_single_set():
    service = MagicMock()
    _vitals(service, "vitals", "anrrate").get.return_value.execute.return_value = {"name": "n"}
    results = _client(service).get_metric_set_freshness(PKG, "anr_rate")
    assert len(results) == 1
    assert results[0].metric_set == "anr_rate"


def test_get_metric_set_freshness_skips_inaccessible_sets():
    """One metric set the account cannot read must not fail the whole batch."""
    service = MagicMock()
    for _suffix, accessor, _label in METRIC_SETS.values():
        _vitals(service, *accessor).get.return_value.execute.return_value = {"name": "n"}
    _vitals(service, "vitals", "lmkrate").get.return_value.execute.side_effect = RuntimeError(
        "403 permission denied"
    )

    results = _client(service).get_metric_set_freshness(PKG)
    assert len(results) == len(METRIC_SETS) - 1
    assert "lmk_rate" not in {r.metric_set for r in results}


def test_get_metric_set_freshness_propagates_unknown_key():
    with pytest.raises(ReportingClientError, match="Unknown metric set"):
        _client(MagicMock()).get_metric_set_freshness(PKG, "nope")


def test_query_metric_set_builds_timeline_and_parses_rows():
    service = MagicMock()
    api = _vitals(service, "vitals", "crashrate")
    api.query.return_value.execute.return_value = {
        "rows": [
            {
                "startTime": {"year": 2026, "month": 8, "day": 20},
                "dimensions": [{"dimension": "versionCode", "int64Value": "42"}],
                "metrics": [{"metric": "crashRate", "decimalValue": {"value": "0.013"}}],
            }
        ]
    }

    result = _client(service).query_metric_set(
        PKG,
        "crash_rate",
        metrics=["crashRate"],
        dimensions=["versionCode"],
        days_back=14,
        filter_expression="versionCode = 42",
        user_cohort="OS_PUBLIC",
    )

    assert result.rows[0].metrics == {"crashRate": pytest.approx(0.013)}
    assert result.rows[0].dimensions == {"versionCode": "42"}
    assert result.next_page_token is None

    kwargs = api.query.call_args.kwargs
    assert kwargs["name"] == f"apps/{PKG}/crashRateMetricSet"
    body = kwargs["body"]
    assert body["metrics"] == ["crashRate"]
    assert body["dimensions"] == ["versionCode"]
    assert body["filter"] == "versionCode = 42"
    assert body["userCohort"] == "OS_PUBLIC"
    assert body["timelineSpec"]["aggregationPeriod"] == "DAILY"
    assert body["timelineSpec"]["startTime"]["year"] >= 2020


def test_query_metric_set_explicit_window_overrides_days_back():
    service = MagicMock()
    api = _vitals(service, "vitals", "anrrate")
    api.query.return_value.execute.return_value = {"rows": []}

    _client(service).query_metric_set(
        PKG,
        "anr_rate",
        start_time=datetime(2026, 1, 1, tzinfo=UTC),
        end_time=datetime(2026, 1, 8, tzinfo=UTC),
        aggregation_period="FULL_RANGE",
    )

    spec = api.query.call_args.kwargs["body"]["timelineSpec"]
    assert spec["startTime"]["year"] == 2026
    assert spec["startTime"]["month"] == 1
    assert spec["startTime"]["day"] == 1
    assert spec["endTime"]["day"] == 8
    assert spec["aggregationPeriod"] == "FULL_RANGE"


def test_query_metric_set_paginates_and_reports_token():
    service = MagicMock()
    api = _vitals(service, "vitals", "crashrate")
    api.query.return_value.execute.side_effect = [
        {"rows": [{"metrics": []}], "nextPageToken": "p2"},
        {"rows": [{"metrics": []}]},
    ]
    result = _client(service).query_metric_set(PKG, "crash_rate", max_results=10)

    assert len(result.rows) == 2
    assert result.next_page_token is None
    assert api.query.call_args_list[1].kwargs["body"]["pageToken"] == "p2"


def test_query_metric_set_stops_at_max_results_and_keeps_token():
    service = MagicMock()
    api = _vitals(service, "vitals", "crashrate")
    api.query.return_value.execute.return_value = {
        "rows": [{"metrics": []}, {"metrics": []}],
        "nextPageToken": "more",
    }
    result = _client(service).query_metric_set(PKG, "crash_rate", max_results=2)

    assert len(result.rows) == 2
    assert result.next_page_token == "more"


def test_query_metric_set_rejects_bad_aggregation_period():
    with pytest.raises(ReportingClientError, match="Unknown aggregation_period"):
        _client(MagicMock()).query_metric_set(PKG, "crash_rate", aggregation_period="WEEKLY")


def test_query_metric_set_rejects_bad_user_cohort():
    with pytest.raises(ReportingClientError, match="Unknown user_cohort"):
        _client(MagicMock()).query_metric_set(PKG, "crash_rate", user_cohort="EVERYONE")


def test_query_metric_set_rejects_unknown_metric_set():
    with pytest.raises(ReportingClientError, match="Unknown metric set"):
        _client(MagicMock()).query_metric_set(PKG, "nope")


# ---------------------------------------------------------------------------
# Client: anomalies
# ---------------------------------------------------------------------------


def test_list_anomalies_parses_and_paginates():
    service = MagicMock()
    service.anomalies.return_value.list.return_value.execute.side_effect = [
        {
            "anomalies": [
                {
                    "name": "apps/x/anomalies/1",
                    "metricSet": "crashRateMetricSet",
                    "dimensions": [{"dimension": "countryCode", "stringValue": "DE"}],
                    "metric": {"metric": "crashRate", "decimalValue": {"value": "0.2"}},
                }
            ],
            "nextPageToken": "n1",
        },
        {"anomalies": []},
    ]
    anomalies = _client(service).list_anomalies(PKG, filter_expression="x = 1")

    assert anomalies[0].metric == "crashRate"
    assert anomalies[0].metric_value == pytest.approx(0.2)
    assert anomalies[0].dimensions == {"countryCode": "DE"}
    first = service.anomalies.return_value.list.call_args_list[0].kwargs
    assert first["parent"] == f"apps/{PKG}"
    assert first["filter"] == "x = 1"


def test_list_anomalies_handles_missing_metric_block():
    service = MagicMock()
    service.anomalies.return_value.list.return_value.execute.return_value = {
        "anomalies": [{"name": "a"}]
    }
    anomalies = _client(service).list_anomalies(PKG)
    assert anomalies[0].metric is None
    assert anomalies[0].metric_value is None


def test_list_anomalies_respects_max_results():
    service = MagicMock()
    service.anomalies.return_value.list.return_value.execute.return_value = {
        "anomalies": [{"name": str(i)} for i in range(5)],
        "nextPageToken": "more",
    }
    assert len(_client(service).list_anomalies(PKG, max_results=2)) == 2


# ---------------------------------------------------------------------------
# Client: errors
# ---------------------------------------------------------------------------


def test_search_error_issues_parses_nested_versions():
    service = MagicMock()
    issues_api = _vitals(service, "vitals", "errors", "issues")
    issues_api.search.return_value.execute.return_value = {
        "errorIssues": [
            {
                "name": "apps/x/errorIssues/1",
                "type": "CRASH",
                "cause": "NullPointerException",
                "location": "MainActivity.onCreate",
                "errorReportCount": 12,
                "distinctUsers": 9,
                "firstOsVersion": {"apiLevel": 31},
                "lastOsVersion": {"apiLevel": 34},
                "firstAppVersion": {"versionCode": 40},
                "lastAppVersion": {"versionCode": 42},
                "sampleErrorReports": ["apps/x/errorReports/r1"],
            }
        ]
    }
    issues = _client(service).search_error_issues(
        PKG, filter_expression="errorIssueType = CRASH", order_by="errorReportCount desc",
        sample_report_limit=3,
    )

    issue = issues[0]
    assert issue.issue_type == "CRASH"
    assert issue.error_report_count == 12
    assert issue.first_os_version == "31"
    assert issue.last_app_version == "42"
    assert issue.sample_error_reports == ["apps/x/errorReports/r1"]

    kwargs = issues_api.search.call_args.kwargs
    assert kwargs["parent"] == f"apps/{PKG}"
    assert kwargs["orderBy"] == "errorReportCount desc"
    assert kwargs["sampleErrorReportLimit"] == 3
    assert "interval_startTime_year" in kwargs


def test_search_error_issues_paginates():
    service = MagicMock()
    issues_api = _vitals(service, "vitals", "errors", "issues")
    issues_api.search.return_value.execute.side_effect = [
        {"errorIssues": [{"name": "1"}], "nextPageToken": "n"},
        {"errorIssues": [{"name": "2"}]},
    ]
    issues = _client(service).search_error_issues(PKG)
    assert [i.name for i in issues] == ["1", "2"]
    assert issues_api.search.call_args_list[1].kwargs["pageToken"] == "n"


def test_search_error_issues_respects_max_results():
    service = MagicMock()
    issues_api = _vitals(service, "vitals", "errors", "issues")
    issues_api.search.return_value.execute.return_value = {
        "errorIssues": [{"name": str(i)} for i in range(4)],
        "nextPageToken": "more",
    }
    assert len(_client(service).search_error_issues(PKG, max_results=2)) == 2


def test_search_error_reports_parses_nested_device_fields():
    service = MagicMock()
    reports_api = _vitals(service, "vitals", "errors", "reports")
    reports_api.search.return_value.execute.return_value = {
        "errorReports": [
            {
                "name": "apps/x/errorReports/r1",
                "type": "CRASH",
                "issue": "apps/x/errorIssues/1",
                "eventTime": "2026-08-20T10:00:00Z",
                "deviceModel": {"marketingName": "Pixel 8"},
                "osVersion": {"apiLevel": 34},
                "appVersion": {"versionCode": 42},
                "reportText": "java.lang.NullPointerException",
            }
        ]
    }
    reports = _client(service).search_error_reports(PKG, filter_expression="errorIssueId = 1")

    report = reports[0]
    assert report.report_type == "CRASH"
    assert report.device_model == "Pixel 8"
    assert report.os_version == "34"
    assert report.app_version == "42"
    assert report.event_time == datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    assert reports_api.search.call_args.kwargs["filter"] == "errorIssueId = 1"


def test_search_error_reports_paginates_and_caps():
    service = MagicMock()
    reports_api = _vitals(service, "vitals", "errors", "reports")
    reports_api.search.return_value.execute.side_effect = [
        {"errorReports": [{"name": "1"}], "nextPageToken": "n"},
        {"errorReports": [{"name": "2"}]},
    ]
    assert len(_client(service).search_error_reports(PKG)) == 2

    reports_api.search.return_value.execute.side_effect = None
    reports_api.search.return_value.execute.return_value = {
        "errorReports": [{"name": str(i)} for i in range(4)],
        "nextPageToken": "more",
    }
    assert len(_client(service).search_error_reports(PKG, max_results=2)) == 2


# ---------------------------------------------------------------------------
# Server tools
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock(spec=PlayStoreClient)
    monkeypatch.setattr(server, "get_client_from_context", lambda: client)
    return client


def test_tool_list_reporting_apps(mock_client: MagicMock):
    mock_client.list_reporting_apps.return_value = [
        ReportingApp(name="apps/a", package_name="a", display_name="A")
    ]
    result = server.list_reporting_apps(max_results=5)
    assert result[0]["package_name"] == "a"
    assert mock_client.list_reporting_apps.call_args.kwargs["max_results"] == 5


def test_tool_list_metric_sets_describes_every_set():
    result = server.list_metric_sets()
    assert len(result) == len(METRIC_SETS)
    crash = next(r for r in result if r["metric_set"] == "crash_rate")
    assert crash["resource_suffix"] == "crashRateMetricSet"
    assert crash["label"]


def test_tool_get_release_filter_options(mock_client: MagicMock):
    mock_client.get_release_filter_options.return_value = ReleaseFilterOptions(
        package_name=PKG, tracks=[{"displayName": "production"}]
    )
    result = server.get_release_filter_options(package_name=PKG)
    assert result["tracks"] == [{"displayName": "production"}]


def test_tool_get_vitals_freshness(mock_client: MagicMock):
    mock_client.get_metric_set_freshness.return_value = [
        MetricSetInfo(name="n", metric_set="crash_rate", package_name=PKG)
    ]
    result = server.get_vitals_freshness(package_name=PKG)
    assert result[0]["metric_set"] == "crash_rate"


def test_tool_get_vitals_freshness_reports_unknown_key(mock_client: MagicMock):
    mock_client.get_metric_set_freshness.side_effect = ReportingClientError("Unknown metric set")
    result = server.get_vitals_freshness(package_name=PKG, metric_set="nope")
    assert "error" in result[0]


def test_tool_get_metric_set_metadata(mock_client: MagicMock):
    mock_client.get_metric_set.return_value = MetricSetInfo(
        name="n", metric_set="anr_rate", package_name=PKG
    )
    result = server.get_metric_set_metadata(package_name=PKG, metric_set="anr_rate")
    assert result["metric_set"] == "anr_rate"


def test_tool_get_metric_set_metadata_reports_unknown_key(mock_client: MagicMock):
    mock_client.get_metric_set.side_effect = ReportingClientError("Unknown metric set 'nope'")
    result = server.get_metric_set_metadata(package_name=PKG, metric_set="nope")
    assert "Unknown metric set" in result["error"]


def test_tool_query_vitals(mock_client: MagicMock):
    mock_client.query_metric_set.return_value = MetricQueryResult(
        package_name=PKG,
        metric_set="crash_rate",
        rows=[MetricRow(metrics={"crashRate": 0.01})],
    )
    result = server.query_vitals(package_name=PKG, metric_set="crash_rate", days_back=30)
    assert result["rows"][0]["metrics"]["crashRate"] == pytest.approx(0.01)
    assert mock_client.query_metric_set.call_args.kwargs["days_back"] == 30


def test_tool_query_vitals_reports_validation_error(mock_client: MagicMock):
    mock_client.query_metric_set.side_effect = ReportingClientError("Unknown aggregation_period")
    result = server.query_vitals(
        package_name=PKG, metric_set="crash_rate", aggregation_period="WEEKLY"
    )
    assert "Unknown aggregation_period" in result["error"]


def test_tool_list_vitals_anomalies(mock_client: MagicMock):
    mock_client.list_anomalies.return_value = [Anomaly(name="a", metric="crashRate")]
    result = server.list_vitals_anomalies(package_name=PKG)
    assert result[0]["metric"] == "crashRate"


def test_tool_search_error_issues(mock_client: MagicMock):
    mock_client.search_error_issues.return_value = [ErrorIssue(name="i", error_report_count=3)]
    result = server.search_error_issues(package_name=PKG, days_back=14)
    assert result[0]["error_report_count"] == 3
    assert mock_client.search_error_issues.call_args.kwargs["days_back"] == 14


def test_tool_search_error_reports(mock_client: MagicMock):
    mock_client.search_error_reports.return_value = [ErrorReport(name="r", report_type="CRASH")]
    result = server.search_error_reports(package_name=PKG)
    assert result[0]["report_type"] == "CRASH"


def test_vitals_tools_are_readable_in_read_only_mode(mock_client: MagicMock):
    """Every Reporting tool is a read, so read-only mode must not block them."""
    mock_client.list_reporting_apps.return_value = []
    server.set_read_only(True)
    try:
        assert server.list_reporting_apps() == []
    finally:
        server.set_read_only(False)
