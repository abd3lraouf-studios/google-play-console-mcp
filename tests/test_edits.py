"""Tests for the edit lifecycle, the escape hatch, locales and resources."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import google_play_console_mcp.server as server
from google_play_console_mcp.client import PlayStoreClient
from google_play_console_mcp.edits import INVOKABLE_ROOTS, READ_ONLY_HTTP_METHODS
from google_play_console_mcp.errors import ValidationError
from google_play_console_mcp.models import (
    EditDescription,
    EditSession,
    ListingUpdateResult,
    LocaleCoverage,
)
from tests.api_double import verified
from tests.conftest import confirm_via_gate

PKG = "com.example.app"


def _http_error(reason: str = "boom", status: int = 400) -> HttpError:
    resp = MagicMock()
    resp.status, resp.reason = status, reason
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


def _stage(service: MagicMock, **sections: object) -> None:
    """Set what each describe_edit read returns; anything absent 404s."""
    api = _edits(service)
    api.tracks.return_value.list.return_value.execute.return_value = sections.get("tracks", {})
    api.listings.return_value.list.return_value.execute.return_value = sections.get("listings", {})
    api.details.return_value.get.return_value.execute.return_value = sections.get("details", {})
    api.apks.return_value.list.return_value.execute.return_value = sections.get("apks", {})
    api.bundles.return_value.list.return_value.execute.return_value = sections.get("bundles", {})


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_open_edit_returns_an_id_and_says_nothing_is_live_yet():
    session = _client(MagicMock()).open_edit(PKG)
    assert session.edit_id == "edit-1"
    assert "Nothing reaches users" in session.message


def test_describe_edit_reads_every_section_back():
    service = MagicMock()
    _stage(
        service,
        tracks={
            "tracks": [
                {
                    "track": "production",
                    "releases": [
                        {"status": "inProgress", "versionCodes": ["42"], "userFraction": 0.1}
                    ],
                }
            ]
        },
        listings={"listings": [{"language": "en-US"}, {"language": "de-DE"}]},
        details={"defaultLanguage": "en-US", "contactEmail": "a@b.com"},
        apks={"apks": [{"versionCode": 41}]},
        bundles={"bundles": [{"versionCode": 42}]},
    )

    described = _client(service).describe_edit(PKG, "edit-1")

    assert described.track_releases[0]["track"] == "production"
    assert described.listing_languages == ["de-DE", "en-US"]
    assert described.details["defaultLanguage"] == "en-US"
    assert described.apk_version_codes == [41]
    assert described.bundle_version_codes == [42]
    assert described.is_empty is False


def test_describe_edit_records_unreadable_sections_rather_than_failing():
    """An edit with no listings staged 404s for listings. That is information."""
    service = MagicMock()
    _stage(service)
    _edits(service).listings.return_value.list.return_value.execute.side_effect = _http_error(
        "no listings", status=404
    )

    described = _client(service).describe_edit(PKG, "edit-1")

    assert described.unreadable == ["listings: no listings"]
    assert described.is_empty is True


def test_describe_edit_reports_an_empty_edit():
    service = MagicMock()
    _stage(service)
    assert _client(service).describe_edit(PKG, "edit-1").is_empty is True


def test_validate_open_edit_passes_and_fails():
    service = MagicMock()
    client = _client(service)
    assert client.validate_open_edit(PKG, "edit-1")["valid"] is True

    _edits(service).validate.return_value.execute.side_effect = _http_error("bad")
    result = client.validate_open_edit(PKG, "edit-1")
    assert result["valid"] is False
    assert result["error"] == "bad"


def test_commit_open_edit_passes_the_review_flag():
    service = MagicMock()
    _edits(service).commit.return_value.execute.return_value = {"id": "edit-1"}
    result = _client(service).commit_open_edit(PKG, "edit-1", changes_not_sent_for_review=True)
    assert result["success"] is True
    assert _edits(service).commit.call_args.kwargs["changesNotSentForReview"] is True


def test_commit_open_edit_reports_failure():
    service = MagicMock()
    _edits(service).commit.return_value.execute.side_effect = _http_error("rejected")
    result = _client(service).commit_open_edit(PKG, "edit-1")
    assert result["success"] is False
    assert result["error"] == "rejected"


def test_discard_edit_deletes_it():
    service = MagicMock()
    result = _client(service).discard_edit(PKG, "edit-1")
    assert result["success"] is True
    _edits(service).delete.assert_called_once()


# ---------------------------------------------------------------------------
# Generic invocation
# ---------------------------------------------------------------------------


def test_resolve_returns_a_callable_and_the_http_verb():
    service = MagicMock()
    request = _edits(service).tracks.return_value.list.return_value
    request.method = "GET"
    request.execute.return_value = {"tracks": []}

    call, verb = _client(service).resolve_publisher_request(
        "edits.tracks.list", {"packageName": PKG, "editId": "edit-1"}
    )
    assert verb == "GET"
    assert call() == {"tracks": []}


def test_resolve_rejects_a_path_that_is_not_a_method():
    with pytest.raises(ValidationError, match="not a method path"):
        _client(MagicMock()).resolve_publisher_request("edits")


def test_resolve_rejects_a_root_outside_the_allowlist():
    """Free getattr traversal is how 'call any method' becomes 'reach anything'."""
    with pytest.raises(ValidationError, match="not an invokable") as exc:
        _client(MagicMock()).resolve_publisher_request("__class__.mro")
    assert "edits" in exc.value.remedy


def test_resolve_rejects_an_unknown_resource():
    client = _client(MagicMock())
    client._service = MagicMock(spec=["edits"])
    client._service.edits.return_value = MagicMock(spec=["tracks"])
    with pytest.raises(ValidationError, match="No API resource"):
        client.resolve_publisher_request("edits.nope.list")


def test_resolve_rejects_an_unknown_method():
    client = _client(MagicMock())
    client._service = MagicMock(spec=["edits"])
    client._service.edits.return_value = MagicMock(spec=["tracks"])
    with pytest.raises(ValidationError, match="No API method"):
        client.resolve_publisher_request("edits.notamethod")


def test_resolve_rejects_a_path_naming_a_resource_rather_than_a_method():
    """ "edits.tracks" resolves to a resource object, not a request."""
    service = MagicMock()
    resource = MagicMock(spec=[])
    service.edits.return_value.tracks.return_value = resource
    client = _client(MagicMock())
    client._service = verified(service)
    with pytest.raises(ValidationError, match="names a resource, not a method") as exc:
        client.resolve_publisher_request("edits.tracks")
    assert "edits.tracks.list" in exc.value.remedy


def test_read_only_http_methods_are_the_safe_verbs():
    assert {"GET", "HEAD", "OPTIONS"} == READ_ONLY_HTTP_METHODS
    assert "edits" in INVOKABLE_ROOTS
    assert "purchases" in INVOKABLE_ROOTS


# ---------------------------------------------------------------------------
# Locales
# ---------------------------------------------------------------------------


def test_list_locale_coverage_cleans_up_its_edit():
    """The source version leaks one edit per call; this must not."""
    service = MagicMock()
    _edits(service).listings.return_value.list.return_value.execute.return_value = {
        "listings": [{"language": "en-US"}, {"language": "fr-FR"}]
    }
    coverage = _client(service).list_locale_coverage(PKG)

    assert coverage.present_locales == ["en-US", "fr-FR"]
    assert coverage.count == 2
    _edits(service).delete.assert_called_once()


def test_list_locale_coverage_cleans_up_even_when_the_read_fails():
    service = MagicMock()
    _edits(service).listings.return_value.list.return_value.execute.side_effect = _http_error("x")
    with pytest.raises(HttpError):
        _client(service).list_locale_coverage(PKG)
    _edits(service).delete.assert_called_once()


def test_list_locale_coverage_computes_missing_and_extra():
    service = MagicMock()
    _edits(service).listings.return_value.list.return_value.execute.return_value = {
        "listings": [{"language": "en-US"}, {"language": "fr-FR"}]
    }
    coverage = _client(service).list_locale_coverage(PKG, ["en-US", "de-DE"])
    assert coverage.missing_locales == ["de-DE"]
    assert coverage.extra_locales == ["fr-FR"]


def test_clone_listing_copies_text_and_commits():
    service = MagicMock()
    listings = _edits(service).listings.return_value
    listings.get.return_value.execute.side_effect = [
        {"title": "T", "shortDescription": "S", "fullDescription": "F", "video": "v"},
        _http_error("no target listing", status=404),
    ]
    listings.get.return_value.execute.side_effect = [
        {"title": "T", "shortDescription": "S", "fullDescription": "F", "video": "v"},
        HttpError(MagicMock(status=404), b"{}"),
    ]

    result = _client(service).clone_listing_to_locale(PKG, "en-US", "en-GB")

    assert result.success is True
    body = listings.update.call_args.kwargs["body"]
    assert body["title"] == "T"
    assert body["fullDescription"] == "F"
    _edits(service).commit.assert_called_once()


def test_clone_listing_refuses_to_overwrite_a_translated_locale():
    """Silently replacing human translation with the source language is the risk."""
    service = MagicMock()
    listings = _edits(service).listings.return_value
    listings.get.return_value.execute.return_value = {"title": "existing"}

    result = _client(service).clone_listing_to_locale(PKG, "en-US", "de-DE")

    assert result.success is False
    assert result.error == "listing_exists"
    assert "overwrite=True" in result.message
    listings.update.assert_not_called()
    _edits(service).delete.assert_called_once()


def test_clone_listing_overwrites_when_told_to():
    service = MagicMock()
    listings = _edits(service).listings.return_value
    listings.get.return_value.execute.return_value = {"title": "T"}
    result = _client(service).clone_listing_to_locale(PKG, "en-US", "de-DE", overwrite=True)
    assert result.success is True
    listings.update.assert_called_once()


def test_clone_listing_reports_failure_and_cleans_up():
    service = MagicMock()
    _edits(service).listings.return_value.get.return_value.execute.side_effect = _http_error(
        "denied"
    )
    result = _client(service).clone_listing_to_locale(PKG, "en-US", "de-DE")
    assert result.success is False
    assert result.error == "denied"
    _edits(service).delete.assert_called_once()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_edit_models_have_sane_defaults():
    assert EditDescription(package_name=PKG, edit_id="e").unreadable == []
    assert EditSession(package_name=PKG, edit_id="e", message="m").edit_id == "e"
    assert LocaleCoverage(package_name=PKG).missing_locales == []


# ---------------------------------------------------------------------------
# Server tools
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    client = MagicMock(spec=PlayStoreClient)
    monkeypatch.setattr(server, "get_client_from_context", lambda: client)
    return client


def test_tool_create_and_describe_edit(mock_client: MagicMock):
    mock_client.open_edit.return_value = EditSession(package_name=PKG, edit_id="e1", message="ok")
    mock_client.describe_edit.return_value = EditDescription(
        package_name=PKG, edit_id="e1", listing_languages=["en-US"]
    )
    assert server.create_edit(package_name=PKG)["edit_id"] == "e1"
    assert server.describe_edit(package_name=PKG, edit_id="e1")["listing_languages"] == ["en-US"]


def test_tool_validate_open_edit(mock_client: MagicMock):
    mock_client.validate_open_edit.return_value = {"valid": True}
    assert server.validate_open_edit(package_name=PKG, edit_id="e1")["valid"] is True


def test_commit_edit_previews_what_is_actually_staged(mock_client: MagicMock):
    """The preview must come from the edit, not from this call's arguments."""
    mock_client.describe_edit.return_value = EditDescription(
        package_name=PKG,
        edit_id="e1",
        track_releases=[{"track": "production", "status": "inProgress", "versionCodes": ["42"]}],
        listing_languages=["en-US"],
        bundle_version_codes=[42],
    )
    preview = server.commit_edit(package_name=PKG, edit_id="e1")

    assert preview["confirmation_required"] is True
    text = preview["preview"]
    assert "production" in text
    assert "PRODUCTION release is staged" in text
    assert "en-US" in text
    assert "read back from the open edit" in text
    mock_client.commit_open_edit.assert_not_called()


def test_commit_edit_warns_when_the_edit_stages_nothing(mock_client: MagicMock):
    mock_client.describe_edit.return_value = EditDescription(
        package_name=PKG, edit_id="e1", is_empty=True, unreadable=["listings: 404"]
    )
    text = server.commit_edit(package_name=PKG, edit_id="e1")["preview"]
    assert "stages nothing readable" in text
    assert "Could not read listings: 404" in text


def test_commit_edit_executes_once_confirmed(mock_client: MagicMock):
    mock_client.describe_edit.return_value = EditDescription(package_name=PKG, edit_id="e1")
    mock_client.commit_open_edit.return_value = {"success": True, "edit_id": "e1"}
    result = confirm_via_gate(server.commit_edit, package_name=PKG, edit_id="e1")
    assert result["success"] is True


def test_commit_edit_builds_the_preview_only_on_the_preview_leg(mock_client: MagicMock):
    """Reading the edit back is an API call; a confirm must not pay for it again."""
    mock_client.describe_edit.return_value = EditDescription(package_name=PKG, edit_id="e1")
    mock_client.commit_open_edit.return_value = {"success": True}
    confirm_via_gate(server.commit_edit, package_name=PKG, edit_id="e1")
    assert mock_client.describe_edit.call_count == 1


def test_tool_delete_edit_is_not_gated(mock_client: MagicMock):
    """Discarding a draft is the safe direction; asking permission leaks edits."""
    mock_client.discard_edit.return_value = {"success": True, "edit_id": "e1"}
    assert server.delete_edit(package_name=PKG, edit_id="e1")["success"] is True


def test_invoke_runs_a_read_immediately(mock_client: MagicMock):
    mock_client.resolve_publisher_request.return_value = (lambda: {"tracks": []}, "GET")
    result = server.android_publisher_invoke(method_path="edits.tracks.list")
    assert result["result"] == {"tracks": []}
    assert result["http_method"] == "GET"


def test_invoke_gates_a_write(mock_client: MagicMock):
    mock_client.resolve_publisher_request.return_value = (lambda: {"ok": True}, "POST")
    preview = server.android_publisher_invoke(
        method_path="edits.commit", params={"packageName": PKG}
    )
    assert preview["confirmation_required"] is True
    assert "raw POST call" in preview["preview"]


def test_invoke_executes_a_write_once_confirmed(mock_client: MagicMock):
    mock_client.resolve_publisher_request.return_value = (lambda: {"ok": True}, "POST")
    result = confirm_via_gate(
        server.android_publisher_invoke,
        method_path="edits.commit",
        params={"packageName": PKG},
    )
    assert result["result"] == {"ok": True}


def test_invoke_returns_the_error_object_for_a_bad_path(mock_client: MagicMock):
    mock_client.resolve_publisher_request.side_effect = ValidationError("nope", remedy="fix it")
    assert server.android_publisher_invoke(method_path="bogus.thing")["remedy"] == "fix it"


def test_tool_list_locale_coverage(mock_client: MagicMock):
    mock_client.list_locale_coverage.return_value = LocaleCoverage(
        package_name=PKG, present_locales=["en-US"], count=1
    )
    assert server.list_locale_coverage(package_name=PKG)["count"] == 1


@pytest.mark.usefixtures("mock_client")
def test_tool_clone_listing_warns_about_overwriting_translation():
    preview = server.clone_listing_to_locale(
        package_name=PKG, source_language="en-US", target_language="de-DE", overwrite=True
    )
    assert "REPLACE any existing de-DE listing" in preview["preview"]


def test_tool_clone_listing_executes_once_confirmed(mock_client: MagicMock):
    mock_client.clone_listing_to_locale.return_value = ListingUpdateResult(
        success=True, package_name=PKG, language="en-GB", message="ok"
    )
    result = confirm_via_gate(
        server.clone_listing_to_locale,
        package_name=PKG,
        source_language="en-US",
        target_language="en-GB",
    )
    assert result["success"] is True


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        (lambda: server.create_edit, {"package_name": PKG}),
        (lambda: server.commit_edit, {"package_name": PKG, "edit_id": "e1"}),
        (lambda: server.delete_edit, {"package_name": PKG, "edit_id": "e1"}),
        (
            lambda: server.clone_listing_to_locale,
            {"package_name": PKG, "source_language": "a", "target_language": "b"},
        ),
    ],
)
def test_edit_write_tools_are_blocked_in_read_only_mode(tool, kwargs, mock_client):
    assert mock_client is not None  # the tools resolve a client before the guard
    server.set_read_only(True)
    try:
        assert "read-only mode" in tool()(**kwargs)["error"]
    finally:
        server.set_read_only(False)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


def test_resources_are_valid_json_and_need_no_credentials():
    for fn in (
        server.store_image_specs_resource,
        server.report_families_resource,
        server.vitals_metric_sets_resource,
        server.config_resource,
    ):
        assert json.loads(fn())


def test_the_config_resource_reports_readiness_without_leaking_values(monkeypatch):
    """A resource is read into the model's context; a bucket id does not belong there."""
    monkeypatch.setenv("GOOGLE_PLAY_MCP_REPORTS_BUCKET", "pubsite_prod_rev_SECRET")
    config = json.loads(server.config_resource())
    assert config["planes"]["bulk_reports"]["configured"] is True
    assert "SECRET" not in json.dumps(config)


def test_the_config_resource_names_what_is_missing(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLAY_MCP_REPORTS_BUCKET", raising=False)
    config = json.loads(server.config_resource())
    assert config["planes"]["bulk_reports"]["configured"] is False
    assert "ACCOUNT-level" in config["planes"]["bulk_reports"]["needs"]
    assert config["planes"]["image_preflight"]["configured"] is True


def test_the_store_image_resource_carries_the_alpha_rule():
    specs = json.loads(server.store_image_specs_resource())
    icon = next(s for s in specs if s["image_type"] == "icon")
    assert icon["alpha"] == "required"


def test_invoke_surfaces_an_api_failure_on_a_read(mock_client: MagicMock):
    """A read runs immediately, so its failure has to be reported here."""

    def boom() -> None:
        raise ValidationError("quota exceeded", remedy="wait")

    mock_client.resolve_publisher_request.return_value = (boom, "GET")
    assert server.android_publisher_invoke(method_path="edits.tracks.list")["remedy"] == "wait"


def test_invoke_write_is_blocked_in_read_only_mode(mock_client: MagicMock):
    mock_client.resolve_publisher_request.return_value = (dict, "POST")
    server.set_read_only(True)
    try:
        result = server.android_publisher_invoke(method_path="edits.commit")
    finally:
        server.set_read_only(False)
    assert "read-only mode" in result["error"]
