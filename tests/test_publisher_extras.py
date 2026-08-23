"""Tests for the extended Publisher v3 endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import google_play_console_mcp.server as server
from google_play_console_mcp.client import PlayStoreClient
from google_play_console_mcp.models import (
    AppDetailsUpdateResult,
    CountryAvailability,
    EditValidationResult,
    ExpansionFile,
    ExternallyHostedApk,
    ListingDeleteResult,
    RegionPriceConversion,
    ReviewReplyDeleteResult,
    SubscriptionActionResult,
    TrackActionResult,
)
from google_play_console_mcp.publisher_extras import (
    EXPANSION_FILE_TYPES,
    RELEASE_STATUSES,
    _parse_release,
)
from tests.api_double import verified
from tests.conftest import confirm_via_gate

PKG = "com.example.app"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_http_error(reason: str = "boom", status: int = 400) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    err = HttpError(resp, b"{}")
    err.reason = reason
    return err


def _client(service: MagicMock) -> PlayStoreClient:
    client = PlayStoreClient(credentials_json={"type": "service_account"})
    client._service = verified(service)
    service.edits.return_value.insert.return_value.execute.return_value = {"id": "edit-1"}
    return client


def _edits(service: MagicMock) -> MagicMock:
    return service.edits.return_value


# ---------------------------------------------------------------------------
# Models and pure helpers
# ---------------------------------------------------------------------------


def test_expansion_file_types_and_release_statuses():
    assert {"main", "patch"} == EXPANSION_FILE_TYPES
    assert {"draft", "inProgress", "halted", "completed"} == RELEASE_STATUSES


def test_parse_release_converts_user_fraction_to_percentage():
    release = _parse_release(
        {
            "track": "beta",
            "status": "inProgress",
            "versionCodes": ["42", "43"],
            "name": "1.2.3",
            "userFraction": 0.25,
            "releaseNotes": [{"language": "en-US", "text": "Fixes"}],
        }
    )
    assert release.version_codes == [42, 43]
    assert release.rollout_percentage == pytest.approx(25.0)
    assert release.release_notes == {"en-US": "Fixes"}


def test_parse_release_treats_absent_fraction_as_full_rollout():
    """A completed release omits userFraction; that means 100%, not 0%."""
    assert _parse_release({"status": "completed"}).rollout_percentage == pytest.approx(100.0)


def test_result_model_defaults():
    assert EditValidationResult(success=True, package_name=PKG, message="m").error is None
    assert CountryAvailability(package_name=PKG, track="production").countries == []
    assert ListingDeleteResult(success=True, package_name=PKG, message="m").deleted_all is False
    assert (
        ReviewReplyDeleteResult(success=True, package_name=PKG, review_id="r", message="m").method
        == "empty_reply"
    )
    assert RegionPriceConversion(package_name=PKG).converted_regions == {}
    assert AppDetailsUpdateResult(success=True, package_name=PKG, message="m").details is None
    assert (
        TrackActionResult(
            success=True, package_name=PKG, track="t", action="create", message="m"
        ).releases
        == []
    )
    assert ExternallyHostedApk(package_name=PKG).version_code is None


# ---------------------------------------------------------------------------
# validate_edit
# ---------------------------------------------------------------------------


def test_validate_edit_discards_the_edit_on_success():
    """Validation is a dry run: it must never commit."""
    service = MagicMock()
    client = _client(service)
    _edits(service).validate.return_value.execute.return_value = {"id": "edit-1"}

    result = client.validate_edit(PKG)

    assert result.success is True
    assert result.edit_id == "edit-1"
    _edits(service).delete.assert_called_once()
    _edits(service).commit.assert_not_called()


def test_validate_edit_reports_failure_and_cleans_up():
    service = MagicMock()
    client = _client(service)
    _edits(service).validate.return_value.execute.side_effect = _make_http_error("invalid")

    result = client.validate_edit(PKG)

    assert result.success is False
    assert result.error == "invalid"
    _edits(service).delete.assert_called_once()
    _edits(service).commit.assert_not_called()


# ---------------------------------------------------------------------------
# get_country_availability
# ---------------------------------------------------------------------------


def test_get_country_availability_parses_countries():
    service = MagicMock()
    client = _client(service)
    _edits(service).countryavailability.return_value.get.return_value.execute.return_value = {
        "syncWithProduction": False,
        "restOfWorld": True,
        "countries": [{"countryCode": "US"}, {"countryCode": "DE"}],
    }

    result = client.get_country_availability(PKG, "beta")

    assert result.countries == ["US", "DE"]
    assert result.sync_with_production is False
    assert result.rest_of_world is True
    _edits(service).delete.assert_called_once()


def test_get_country_availability_cleans_up_the_edit_on_error():
    """A read must not leak a dangling edit when the API fails."""
    service = MagicMock()
    client = _client(service)
    api = _edits(service).countryavailability.return_value
    api.get.return_value.execute.side_effect = _make_http_error("nope")

    with pytest.raises(HttpError):
        client.get_country_availability(PKG, "beta")

    _edits(service).delete.assert_called_once()


# ---------------------------------------------------------------------------
# update_app_details
# ---------------------------------------------------------------------------


def test_update_app_details_preserves_unsupplied_fields():
    """The API replaces the whole resource, so untouched fields must round-trip."""
    service = MagicMock()
    client = _client(service)
    details = _edits(service).details.return_value
    details.get.return_value.execute.return_value = {
        "defaultLanguage": "en-US",
        "contactEmail": "old@example.com",
        "contactPhone": "+1000",
        "contactWebsite": "https://old.example.com",
    }
    details.update.return_value.execute.return_value = {
        "defaultLanguage": "en-US",
        "contactEmail": "new@example.com",
        "contactWebsite": "https://old.example.com",
    }

    result = client.update_app_details(PKG, contact_email="new@example.com")

    body = details.update.call_args.kwargs["body"]
    assert body["contactEmail"] == "new@example.com"
    assert body["defaultLanguage"] == "en-US"
    assert body["contactPhone"] == "+1000"
    assert body["contactWebsite"] == "https://old.example.com"
    assert result.success is True
    assert result.details is not None
    assert result.details.developer_email == "new@example.com"
    _edits(service).commit.assert_called_once()


def test_update_app_details_reports_failure():
    service = MagicMock()
    client = _client(service)
    _edits(service).details.return_value.get.return_value.execute.side_effect = _make_http_error(
        "denied"
    )

    result = client.update_app_details(PKG, default_language="fr-FR")

    assert result.success is False
    assert result.error == "denied"
    _edits(service).delete.assert_called_once()


# ---------------------------------------------------------------------------
# tracks
# ---------------------------------------------------------------------------


def test_create_track_commits_and_parses_releases():
    service = MagicMock()
    client = _client(service)
    _edits(service).tracks.return_value.create.return_value.execute.return_value = {
        "track": "qa-team",
        "releases": [{"status": "draft", "versionCodes": ["7"]}],
    }

    result = client.create_track(PKG, "qa-team")

    assert result.success is True
    assert result.action == "create"
    assert result.releases[0].version_codes == [7]
    body = _edits(service).tracks.return_value.create.call_args.kwargs["body"]
    assert body["track"] == "qa-team"
    assert body["releases"] == []
    _edits(service).commit.assert_called_once()


def test_update_track_uses_patch_and_passes_releases():
    service = MagicMock()
    client = _client(service)
    _edits(service).tracks.return_value.patch.return_value.execute.return_value = {"releases": []}
    releases = [{"status": "completed", "versionCodes": ["9"]}]

    result = client.update_track(PKG, "beta", releases=releases)

    assert result.action == "update"
    kwargs = _edits(service).tracks.return_value.patch.call_args.kwargs
    assert kwargs["track"] == "beta"
    assert kwargs["body"]["releases"] == releases


def test_create_track_reports_failure():
    service = MagicMock()
    client = _client(service)
    _edits(service).tracks.return_value.create.return_value.execute.side_effect = _make_http_error(
        "exists"
    )

    result = client.create_track(PKG, "qa-team")

    assert result.success is False
    assert result.error == "exists"
    _edits(service).delete.assert_called_once()


def test_update_track_reports_failure():
    service = MagicMock()
    client = _client(service)
    _edits(service).tracks.return_value.patch.return_value.execute.side_effect = _make_http_error(
        "gone"
    )

    result = client.update_track(PKG, "beta")

    assert result.success is False
    assert result.error == "gone"


# ---------------------------------------------------------------------------
# listings
# ---------------------------------------------------------------------------


def test_delete_listing_commits():
    service = MagicMock()
    client = _client(service)
    result = client.delete_listing(PKG, "de-DE")

    assert result.success is True
    assert result.language == "de-DE"
    assert result.deleted_all is False
    assert _edits(service).listings.return_value.delete.call_args.kwargs["language"] == "de-DE"
    _edits(service).commit.assert_called_once()


def test_delete_listing_reports_failure():
    service = MagicMock()
    client = _client(service)
    _edits(
        service
    ).listings.return_value.delete.return_value.execute.side_effect = _make_http_error("nope")

    result = client.delete_listing(PKG, "de-DE")

    assert result.success is False
    assert result.error == "nope"
    _edits(service).delete.assert_called_once()


def test_delete_all_listings_commits():
    service = MagicMock()
    client = _client(service)
    result = client.delete_all_listings(PKG)

    assert result.success is True
    assert result.deleted_all is True
    assert result.language is None
    _edits(service).listings.return_value.deleteall.assert_called_once()
    _edits(service).commit.assert_called_once()


def test_delete_all_listings_reports_failure():
    service = MagicMock()
    client = _client(service)
    _edits(
        service
    ).listings.return_value.deleteall.return_value.execute.side_effect = _make_http_error("no")

    result = client.delete_all_listings(PKG)

    assert result.success is False
    assert result.deleted_all is True
    assert result.error == "no"


# ---------------------------------------------------------------------------
# externally hosted APKs and expansion files
# ---------------------------------------------------------------------------


def test_add_externally_hosted_apk_parses_result():
    service = MagicMock()
    client = _client(service)
    _edits(service).apks.return_value.addexternallyhosted.return_value.execute.return_value = {
        "externallyHostedApk": {
            "versionCode": 42,
            "externallyHostedUrl": "https://cdn.example.com/app.apk",
            "fileSize": "1048576",
            "fileSha1Base64": "sha1",
            "fileSha256Base64": "sha256",
        }
    }

    result = client.add_externally_hosted_apk(PKG, {"versionCode": 42})

    assert result.version_code == 42
    assert result.file_size == 1048576
    assert result.file_sha256_base64 == "sha256"
    _edits(service).commit.assert_called_once()


def test_add_externally_hosted_apk_handles_absent_file_size():
    service = MagicMock()
    client = _client(service)
    _edits(service).apks.return_value.addexternallyhosted.return_value.execute.return_value = {
        "externallyHostedApk": {"versionCode": 1}
    }
    assert client.add_externally_hosted_apk(PKG, {}).file_size is None


def test_add_externally_hosted_apk_cleans_up_and_reraises():
    service = MagicMock()
    client = _client(service)
    _edits(
        service
    ).apks.return_value.addexternallyhosted.return_value.execute.side_effect = _make_http_error(
        "403", status=403
    )

    with pytest.raises(HttpError):
        client.add_externally_hosted_apk(PKG, {})

    _edits(service).delete.assert_called_once()
    _edits(service).commit.assert_not_called()


def test_set_expansion_file_reference_update_semantics():
    service = MagicMock()
    client = _client(service)
    api = _edits(service).expansionfiles.return_value
    api.update.return_value.execute.return_value = {"referencesVersion": 41, "fileSize": 999}

    result = client.set_expansion_file_reference(PKG, 42, "main", 41)

    assert result.references_version == 41
    assert result.file_size == 999
    api.update.assert_called_once()
    api.patch.assert_not_called()


def test_set_expansion_file_reference_patch_semantics():
    service = MagicMock()
    client = _client(service)
    api = _edits(service).expansionfiles.return_value
    api.patch.return_value.execute.return_value = {"referencesVersion": 41}

    client.set_expansion_file_reference(PKG, 42, "patch", 41, patch=True)

    api.patch.assert_called_once()
    api.update.assert_not_called()


def test_set_expansion_file_reference_rejects_bad_type():
    client = _client(MagicMock())
    with pytest.raises(ValueError, match="expansion_file_type must be one of"):
        client.set_expansion_file_reference(PKG, 42, "obb", 41)


def test_set_expansion_file_reference_cleans_up_and_reraises():
    service = MagicMock()
    client = _client(service)
    api = _edits(service).expansionfiles.return_value
    api.update.return_value.execute.side_effect = _make_http_error("bad")

    with pytest.raises(HttpError):
        client.set_expansion_file_reference(PKG, 42, "main", 41)

    _edits(service).delete.assert_called_once()


# ---------------------------------------------------------------------------
# purchases and monetization
# ---------------------------------------------------------------------------


def test_acknowledge_subscription_purchase_success():
    service = MagicMock()
    client = _client(service)

    result = client.acknowledge_subscription_purchase(PKG, "sub_monthly", "tok-1")

    assert result.success is True
    assert result.action == "acknowledge"
    kwargs = service.purchases.return_value.subscriptions.return_value.acknowledge.call_args.kwargs
    assert kwargs["subscriptionId"] == "sub_monthly"
    assert kwargs["token"] == "tok-1"
    assert kwargs["body"] == {}


def test_acknowledge_subscription_purchase_sends_developer_payload():
    service = MagicMock()
    client = _client(service)
    client.acknowledge_subscription_purchase(PKG, "sub", "tok", developer_payload="order-9")
    kwargs = service.purchases.return_value.subscriptions.return_value.acknowledge.call_args.kwargs
    assert kwargs["body"] == {"developerPayload": "order-9"}


def test_acknowledge_subscription_purchase_reports_failure():
    service = MagicMock()
    client = _client(service)
    service.purchases.return_value.subscriptions.return_value.acknowledge.return_value.execute.side_effect = _make_http_error(
        "already acknowledged"
    )

    result = client.acknowledge_subscription_purchase(PKG, "sub", "tok")

    assert result.success is False
    assert result.error == "already acknowledged"


def test_archive_subscription_success():
    service = MagicMock()
    client = _client(service)

    result = client.archive_subscription(PKG, "sub_monthly")

    assert result.success is True
    assert result.action == "archive"
    assert result.details == {"product_id": "sub_monthly"}


def test_archive_subscription_reports_failure():
    service = MagicMock()
    client = _client(service)
    service.monetization.return_value.subscriptions.return_value.archive.return_value.execute.side_effect = _make_http_error(
        "has purchases"
    )

    result = client.archive_subscription(PKG, "sub_monthly")

    assert result.success is False
    assert result.error == "has purchases"


def test_convert_region_prices_splits_micros_into_units_and_nanos():
    service = MagicMock()
    client = _client(service)
    service.monetization.return_value.convertRegionPrices.return_value.execute.return_value = {
        "convertedRegionPrices": {"US": {"price": {"units": "4"}}},
        "convertedOtherRegionsPrice": {"usdPrice": {"units": "4"}},
    }

    result = client.convert_region_prices(PKG, "4990000", "USD")

    body = service.monetization.return_value.convertRegionPrices.call_args.kwargs["body"]
    assert body["price"]["units"] == "4"
    assert body["price"]["nanos"] == 990_000_000
    assert body["price"]["currencyCode"] == "USD"
    assert result.converted_regions == {"US": {"price": {"units": "4"}}}
    assert result.converted_other_regions == {"usdPrice": {"units": "4"}}


# ---------------------------------------------------------------------------
# reviews and managed publishing
# ---------------------------------------------------------------------------


def test_delete_review_reply_uses_empty_reply_and_says_so():
    """There is no delete-reply endpoint; the result must not pretend otherwise."""
    service = MagicMock()
    client = _client(service)

    result = client.delete_review_reply(PKG, "review-1")

    assert result.success is True
    assert result.method == "empty_reply"
    assert service.reviews.return_value.reply.call_args.kwargs["body"] == {"replyText": ""}


def test_delete_review_reply_reports_failure():
    service = MagicMock()
    client = _client(service)
    service.reviews.return_value.reply.return_value.execute.side_effect = _make_http_error("gone")

    result = client.delete_review_reply(PKG, "review-1")

    assert result.success is False
    assert result.error == "gone"


# ---------------------------------------------------------------------------
# Server tools
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock(spec=PlayStoreClient)
    monkeypatch.setattr(server, "get_client_from_context", lambda: client)
    return client


@pytest.fixture
def _writable() -> object:
    server.set_read_only(False)
    yield
    server.set_read_only(False)


@pytest.mark.usefixtures("_writable")
def test_tool_validate_edit(mock_client: MagicMock):
    mock_client.validate_edit.return_value = EditValidationResult(
        success=True, package_name=PKG, message="ok"
    )
    assert server.validate_edit(package_name=PKG)["success"] is True


@pytest.mark.usefixtures("_writable")
def test_tool_get_country_availability(mock_client: MagicMock):
    mock_client.get_country_availability.return_value = CountryAvailability(
        package_name=PKG, track="production", countries=["US"]
    )
    assert server.get_country_availability(package_name=PKG, track="production")["countries"] == [
        "US"
    ]


@pytest.mark.usefixtures("_writable")
def test_tool_update_app_details(mock_client: MagicMock):
    mock_client.update_app_details.return_value = AppDetailsUpdateResult(
        success=True, package_name=PKG, message="ok"
    )
    result = confirm_via_gate(server.update_app_details, package_name=PKG, contact_email="a@b.com")
    assert result["success"] is True
    assert mock_client.update_app_details.call_args.kwargs["contact_email"] == "a@b.com"


@pytest.mark.usefixtures("_writable")
def test_tool_create_and_update_track(mock_client: MagicMock):
    mock_client.create_track.return_value = TrackActionResult(
        success=True, package_name=PKG, track="qa", action="create", message="ok"
    )
    mock_client.update_track.return_value = TrackActionResult(
        success=True, package_name=PKG, track="qa", action="update", message="ok"
    )
    assert server.create_track(package_name=PKG, track="qa")["action"] == "create"
    assert server.update_track(package_name=PKG, track="qa")["action"] == "update"


@pytest.mark.usefixtures("_writable")
def test_tool_delete_listing_and_all(mock_client: MagicMock):
    mock_client.delete_listing.return_value = ListingDeleteResult(
        success=True, package_name=PKG, language="de-DE", message="ok"
    )
    mock_client.delete_all_listings.return_value = ListingDeleteResult(
        success=True, package_name=PKG, deleted_all=True, message="ok"
    )
    assert (
        confirm_via_gate(server.delete_listing, package_name=PKG, language="de-DE")["language"]
        == "de-DE"
    )
    assert confirm_via_gate(server.delete_all_listings, package_name=PKG)["deleted_all"] is True


@pytest.mark.usefixtures("_writable")
def test_tool_add_externally_hosted_apk(mock_client: MagicMock):
    mock_client.add_externally_hosted_apk.return_value = ExternallyHostedApk(
        package_name=PKG, version_code=42
    )
    result = server.add_externally_hosted_apk(
        package_name=PKG, externally_hosted_apk={"versionCode": 42}
    )
    assert result["version_code"] == 42


@pytest.mark.usefixtures("_writable")
def test_tool_expansion_file_patch_and_update(mock_client: MagicMock):
    mock_client.set_expansion_file_reference.return_value = ExpansionFile(
        version_code=42, expansion_file_type="main", references_version=41
    )
    assert (
        server.patch_expansion_file(
            package_name=PKG, version_code=42, expansion_file_type="main", references_version=41
        )["references_version"]
        == 41
    )
    assert mock_client.set_expansion_file_reference.call_args.kwargs["patch"] is True

    server.update_expansion_file(
        package_name=PKG, version_code=42, expansion_file_type="main", references_version=41
    )
    assert mock_client.set_expansion_file_reference.call_args.kwargs["patch"] is False


@pytest.mark.usefixtures("_writable")
def test_tool_expansion_file_reports_bad_type(mock_client: MagicMock):
    mock_client.set_expansion_file_reference.side_effect = ValueError(
        "expansion_file_type must be one of ['main', 'patch'], got 'obb'"
    )
    assert (
        "must be one of"
        in server.patch_expansion_file(
            package_name=PKG, version_code=1, expansion_file_type="obb", references_version=1
        )["error"]
    )
    assert (
        "must be one of"
        in server.update_expansion_file(
            package_name=PKG, version_code=1, expansion_file_type="obb", references_version=1
        )["error"]
    )


@pytest.mark.usefixtures("_writable")
def test_tool_acknowledge_subscription_purchase(mock_client: MagicMock):
    mock_client.acknowledge_subscription_purchase.return_value = SubscriptionActionResult(
        success=True, package_name=PKG, purchase_token="t", action="acknowledge", message="ok"
    )
    assert (
        server.acknowledge_subscription_purchase(
            package_name=PKG, subscription_id="sub", purchase_token="t"
        )["action"]
        == "acknowledge"
    )


@pytest.mark.usefixtures("_writable")
def test_tool_archive_subscription(mock_client: MagicMock):
    mock_client.archive_subscription.return_value = SubscriptionActionResult(
        success=True, package_name=PKG, purchase_token="", action="archive", message="ok"
    )
    assert server.archive_subscription(package_name=PKG, product_id="sub")["action"] == "archive"


def test_tool_convert_region_prices_is_a_read(mock_client: MagicMock):
    """Price conversion mutates nothing, so read-only mode must not block it."""
    mock_client.convert_region_prices.return_value = RegionPriceConversion(
        package_name=PKG, converted_regions={"US": {}}
    )
    server.set_read_only(True)
    try:
        result = server.convert_region_prices(
            package_name=PKG, price_micros="4990000", currency_code="USD"
        )
    finally:
        server.set_read_only(False)
    assert result["converted_regions"] == {"US": {}}


@pytest.mark.usefixtures("_writable")
def test_tool_delete_review_reply(mock_client: MagicMock):
    mock_client.delete_review_reply.return_value = ReviewReplyDeleteResult(
        success=True, package_name=PKG, review_id="r", message="ok"
    )
    assert (
        confirm_via_gate(server.delete_review_reply, package_name=PKG, review_id="r")["method"]
        == "empty_reply"
    )


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (lambda: server.validate_edit, {"package_name": PKG}),
        (lambda: server.update_app_details, {"package_name": PKG}),
        (lambda: server.create_track, {"package_name": PKG, "track": "qa"}),
        (lambda: server.update_track, {"package_name": PKG, "track": "qa"}),
        (lambda: server.delete_listing, {"package_name": PKG, "language": "de-DE"}),
        (lambda: server.delete_all_listings, {"package_name": PKG}),
        (
            lambda: server.add_externally_hosted_apk,
            {"package_name": PKG, "externally_hosted_apk": {}},
        ),
        (
            lambda: server.patch_expansion_file,
            {
                "package_name": PKG,
                "version_code": 1,
                "expansion_file_type": "main",
                "references_version": 1,
            },
        ),
        (
            lambda: server.update_expansion_file,
            {
                "package_name": PKG,
                "version_code": 1,
                "expansion_file_type": "main",
                "references_version": 1,
            },
        ),
        (
            lambda: server.acknowledge_subscription_purchase,
            {"package_name": PKG, "subscription_id": "s", "purchase_token": "t"},
        ),
        (lambda: server.archive_subscription, {"package_name": PKG, "product_id": "s"}),
        (lambda: server.delete_review_reply, {"package_name": PKG, "review_id": "r"}),
    ],
)
def test_write_tools_are_blocked_in_read_only_mode(tool, kwargs, mock_client: MagicMock):  # noqa: ARG001
    server.set_read_only(True)
    try:
        result = tool()(**kwargs)
    finally:
        server.set_read_only(False)
    assert "read-only mode" in result["error"]
