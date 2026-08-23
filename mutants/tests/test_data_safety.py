"""Tests for the Data Safety CSV path (applications.dataSafety).

The declaration has no read API, so nothing here can be checked against Play.
What can be checked is that a bad CSV never leaves the machine, that a good one
is transmitted byte-for-byte, and that the confirmation gate shows a human the
declaration rather than 190 KB of CSV.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import google_play_console_mcp.server as server
from google_play_console_mcp import data_safety
from google_play_console_mcp.client import PlayStoreClient, PlayStoreClientError
from google_play_console_mcp.data_safety import (
    build_request,
    declared,
    load_csv,
    parse_csv,
    preflight_csv,
    validate,
)
from google_play_console_mcp.errors import ValidationError
from google_play_console_mcp.models import DataSafetyResult
from tests.api_double import verified
from tests.conftest import confirm_via_gate

PKG = "com.example.app"
HEADER = (
    "Question ID (machine readable),Response ID (machine readable),"
    "Response value,Answer requirement,Human-friendly question label"
)


def csv_text(*rows: str, header: str = HEADER) -> str:
    return "\n".join([header, *rows])


def write_csv(tmp_path, text: str, *, name="ds.csv", raw: bytes | None = None) -> str:
    """Write a fixture to disk.

    Built here rather than checked in: the BOM, CRLF and UTF-16 cases are
    exactly what git normalisation and editors rewrite, so a committed file
    would test whatever the last checkout did to it.
    """
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8") if raw is None else raw)
    return str(path)


VALID_ROWS = (
    "PSL_DATA_TYPES_PERSONAL,PSL_NAME,TRUE,MULTIPLE_CHOICE,Personal info / Name",
    "PSL_DATA_TYPES_PERSONAL,PSL_EMAIL,FALSE,MULTIPLE_CHOICE,Personal info / Email",
    "PSL_SUPPORT_DATA_DELETION_BY_USER,DATA_DELETION_YES,TRUE,SINGLE_CHOICE,Deletion?",
    "PSL_SUPPORT_DATA_DELETION_BY_USER,DATA_DELETION_NO,FALSE,SINGLE_CHOICE,Deletion?",
)


@pytest.fixture
def good_csv(tmp_path) -> str:
    return write_csv(tmp_path, csv_text(*VALID_ROWS))


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def test_a_plain_utf8_export_parses(good_csv):
    report = preflight_csv(good_csv)
    assert report["valid"] is True
    assert report["errors"] == []
    assert report["detected"]["encoding"] == "utf-8"
    assert report["detected"]["line_ending"] == "LF"
    assert report["detected"]["rows"] == 4
    assert report["detected"]["questions"] == 2
    assert report["detected"]["answered_true"] == 2


def test_a_bom_is_stripped_from_the_uploaded_text(tmp_path):
    text = csv_text(*VALID_ROWS)
    with_bom = write_csv(tmp_path, "", raw=b"\xef\xbb\xbf" + text.encode())
    without = write_csv(tmp_path, text, name="plain.csv")

    parsed = load_csv(with_bom)

    assert parsed.encoding == "utf-8-sig"
    assert not parsed.text.startswith("﻿")
    # Same answers, so the same digest as the BOM-less file.
    assert parsed.sha256 == load_csv(without).sha256
    assert any("BOM" in w for w in preflight_csv(with_bom)["warnings"])


def test_crlf_is_reported_but_never_rewritten(tmp_path):
    path = write_csv(tmp_path, csv_text(*VALID_ROWS).replace("\n", "\r\n"))

    parsed = load_csv(path)

    assert parsed.line_ending == "CRLF"
    # Re-serialising would change bytes the human approved by digest.
    assert "\r\n" in parsed.text
    assert preflight_csv(path)["valid"] is True


def test_mixed_line_endings_are_flagged(tmp_path):
    path = write_csv(tmp_path, HEADER + "\r\n" + VALID_ROWS[0] + "\n" + VALID_ROWS[1])
    assert any("mixed" in w for w in preflight_csv(path)["warnings"])


def test_a_utf16_file_is_refused_rather_than_transcoded(tmp_path):
    path = write_csv(tmp_path, "", raw=b"\xff\xfe" + HEADER.encode("utf-16-le"))
    with pytest.raises(ValidationError, match="UTF-16, not UTF-8"):
        preflight_csv(path)


def test_a_big_endian_utf16_file_is_refused(tmp_path):
    path = write_csv(tmp_path, "", raw=b"\xfe\xff" + HEADER.encode("utf-16-be"))
    with pytest.raises(ValidationError, match="UTF-16, not UTF-8"):
        preflight_csv(path)


def test_undecodable_bytes_are_refused(tmp_path):
    path = write_csv(tmp_path, "", raw=b"\x41\xc3\x28")
    with pytest.raises(ValidationError, match="not valid UTF-8"):
        preflight_csv(path)


def test_a_missing_file_names_the_export_step():
    with pytest.raises(ValidationError, match="No Data Safety CSV") as excinfo:
        load_csv("/nonexistent/ds.csv")
    assert "Export to CSV" in excinfo.value.remedy


def test_an_oversized_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(data_safety, "MAX_CSV_BYTES", 10)
    path = write_csv(tmp_path, csv_text(*VALID_ROWS))
    with pytest.raises(ValidationError, match="MB"):
        load_csv(path)


def test_an_unreadable_file_is_refused(tmp_path, monkeypatch):
    path = write_csv(tmp_path, csv_text(*VALID_ROWS))

    def _boom(_self):
        raise OSError("permission denied")

    monkeypatch.setattr("pathlib.Path.read_bytes", _boom)
    with pytest.raises(ValidationError, match="Could not read"):
        load_csv(path)


def test_a_field_past_the_csv_limit_is_an_error_not_a_crash(tmp_path):
    # An unclosed quote swallows the rest of the file into one cell and trips
    # csv's own 128 KB field limit. Unhandled, that escapes as a bare _csv.Error
    # with no remedy — the one thing errors.py exists to prevent.
    path = write_csv(tmp_path, csv_text('PSL_NAME,R1,true,REQUIRED,"' + "x" * 200_000))
    with pytest.raises(ValidationError, match="could not be read as CSV"):
        preflight_csv(path)


def test_oversized_inline_text_is_refused(monkeypatch):
    # load_csv checks the file on disk; without this the inline safety_labels
    # path had no ceiling at all.
    monkeypatch.setattr(data_safety, "MAX_CSV_BYTES", 100)
    with pytest.raises(ValidationError, match="MB"):
        parse_csv(csv_text(*VALID_ROWS) + "\n" + "PSL_X,R,true,OPTIONAL,y\n" * 50)


def test_text_input_is_accepted_directly():
    parsed = parse_csv("﻿" + csv_text(*VALID_ROWS))
    assert not parsed.text.startswith("﻿")
    assert len(parsed.rows) == 4


# ---------------------------------------------------------------------------
# Structure — blocking
# ---------------------------------------------------------------------------


def test_an_empty_file_is_an_error(tmp_path):
    report = preflight_csv(write_csv(tmp_path, ""))
    assert report["valid"] is False
    assert "CSV is empty." in report["errors"]


def test_a_header_with_no_rows_is_an_error(tmp_path):
    report = preflight_csv(write_csv(tmp_path, HEADER))
    assert report["errors"] == ["CSV has a header but no response rows."]


def test_wrong_headers_name_the_column(tmp_path):
    report = preflight_csv(write_csv(tmp_path, csv_text(VALID_ROWS[0], header="A,B,C,D,E")))
    assert "Column 1 header is 'A'" in report["errors"][0]


def test_a_short_header_names_what_is_missing(tmp_path):
    short = ",".join(HEADER.split(",")[:4])
    report = preflight_csv(write_csv(tmp_path, csv_text("a,b,c,d", header=short)))
    assert "Missing: 'Human-friendly question label'" in report["errors"][0]


def test_the_help_center_spelling_of_column_two_is_accepted(tmp_path):
    header = HEADER.replace("Response ID (machine readable)", "Response (machine readable)")
    report = preflight_csv(write_csv(tmp_path, csv_text(*VALID_ROWS, header=header)))
    assert report["valid"] is True


@pytest.mark.parametrize("value", ["true", "TRUE", "false", "FALSE", "True"])
def test_booleans_are_accepted_in_any_case(tmp_path, value):
    # Google's Help Center says "enter TRUE or FALSE", but real Play Console
    # exports disagree with it: of six sampled from published apps between 2022
    # and 2026, five write lowercase and one writes upper. Refusing either
    # spelling would reject most genuine files.
    row = f"PSL_NAME,R1,{value},SINGLE_CHOICE,Name"
    assert preflight_csv(write_csv(tmp_path, csv_text(row)))["valid"] is True


@pytest.mark.parametrize("value", ["Yes", "1", "y", "maybe"])
def test_a_response_value_that_is_not_a_boolean_is_an_error(tmp_path, value):
    row = f"PSL_NAME,R1,{value},SINGLE_CHOICE,Name"
    report = preflight_csv(write_csv(tmp_path, csv_text(row)))
    assert report["valid"] is False
    assert f"Response value is {value!r}" in report["errors"][0]


def test_a_blank_response_value_is_allowed(tmp_path):
    report = preflight_csv(write_csv(tmp_path, csv_text("PSL_NAME,R1,,SINGLE_CHOICE,N")))
    assert report["valid"] is True


def test_a_row_with_no_question_id_is_an_error(tmp_path):
    report = preflight_csv(write_csv(tmp_path, csv_text(",R1,TRUE,SINGLE_CHOICE,N")))
    assert report["errors"] == ["Line 2 has no Question ID."]


def test_a_repeated_response_names_both_lines(tmp_path):
    rows = ("PSL_NAME,R1,TRUE,SINGLE_CHOICE,N", "PSL_NAME,R1,FALSE,SINGLE_CHOICE,N")
    report = preflight_csv(write_csv(tmp_path, csv_text(*rows)))
    assert "repeats PSL_NAME / R1, already on line 2" in report["errors"][0]


def test_two_answers_to_a_single_choice_question_is_an_error(tmp_path):
    rows = ("PSL_NAME,R1,TRUE,SINGLE_CHOICE,N", "PSL_NAME,R2,TRUE,SINGLE_CHOICE,N")
    report = preflight_csv(write_csv(tmp_path, csv_text(*rows)))
    assert report["valid"] is False
    assert "SINGLE_CHOICE but 2 responses are TRUE" in report["errors"][0]


def test_a_row_with_no_response_id_is_not_deduplicated(tmp_path):
    # Free-text questions have no response option, so two of them are not a
    # repeat of each other.
    rows = (
        "PSL_DATA_DELETION_URL,,TRUE,OPTIONAL,URL",
        "PSL_ACCOUNT_DELETION_URL,,TRUE,OPTIONAL,URL",
    )
    report = preflight_csv(write_csv(tmp_path, csv_text(*rows)))
    assert report["valid"] is True
    assert report["detected"]["rows"] == 2


def test_a_short_row_is_padded_rather_than_dropped(tmp_path):
    report = preflight_csv(write_csv(tmp_path, csv_text("PSL_NAME,R1,TRUE")))
    assert report["valid"] is True
    assert report["detected"]["rows"] == 1


def test_blank_rows_are_skipped(tmp_path):
    report = preflight_csv(write_csv(tmp_path, csv_text(VALID_ROWS[0], "", ",,,,")))
    assert report["detected"]["rows"] == 1


def test_a_quoted_label_containing_a_comma_survives(tmp_path):
    row = 'PSL_NAME,R1,TRUE,SINGLE_CHOICE,"Name, address, and more"'
    report = preflight_csv(write_csv(tmp_path, csv_text(row)))
    assert report["valid"] is True
    assert report["declared"][0]["label"] == "Name, address, and more"


# ---------------------------------------------------------------------------
# Conditional rules — warnings only
# ---------------------------------------------------------------------------


def test_an_entirely_blank_question_is_not_flagged(tmp_path):
    # Most of a real export is conditional on answers elsewhere in it, so an
    # unanswered group is normal, not a fault.
    rows = ("PSL_NAME,R1,,MULTIPLE_CHOICE,N", "PSL_NAME,R2,,MULTIPLE_CHOICE,N")
    report = preflight_csv(write_csv(tmp_path, csv_text(*rows)))
    assert report["valid"] is True
    assert not [w for w in report["warnings"] if "no response is TRUE" in w]


@pytest.mark.parametrize("requirement", ["MULTIPLE_CHOICE", "SINGLE_CHOICE", "REQUIRED"])
def test_an_answered_question_with_no_yes_is_only_a_warning(tmp_path, requirement):
    rows = (f"PSL_NAME,R1,FALSE,{requirement},N", f"PSL_NAME,R2,FALSE,{requirement},N")
    report = preflight_csv(write_csv(tmp_path, csv_text(*rows)))
    assert report["valid"] is True
    assert any("no response is TRUE" in w for w in report["warnings"])


def test_an_unknown_answer_requirement_is_a_warning_and_disables_its_rules(tmp_path):
    rows = ("PSL_NAME,R1,TRUE,BRAND_NEW_KIND,N", "PSL_NAME,R2,TRUE,BRAND_NEW_KIND,N")
    report = preflight_csv(write_csv(tmp_path, csv_text(*rows)))
    assert report["valid"] is True
    assert any("does not know" in w for w in report["warnings"])


def test_an_unknown_question_id_is_a_warning_not_an_error(tmp_path):
    report = preflight_csv(write_csv(tmp_path, csv_text("PSL_BRAND_NEW,R1,TRUE,SINGLE_CHOICE,N")))
    assert report["valid"] is True
    assert report["unknown_question_ids"] == ["PSL_BRAND_NEW"]
    assert "not in the partial snapshot" in report["warnings"][0]


def test_composite_ids_are_checked_segment_by_segment(tmp_path):
    known = "PSL_DATA_USAGE_RESPONSES:PSL_NAME:DATA_USAGE_COLLECTION_PURPOSE"
    novel = "PSL_DATA_USAGE_RESPONSES:PSL_BRAND_NEW_TYPE:DATA_USAGE_COLLECTION_PURPOSE"
    rows = (
        f"{known},R1,TRUE,MULTIPLE_CHOICE,Known",
        f"{novel},R1,TRUE,MULTIPLE_CHOICE,Novel",
    )
    report = preflight_csv(write_csv(tmp_path, csv_text(*rows)))
    # A new data type under a familiar family must not be hidden by the prefix.
    assert report["unknown_question_ids"] == [novel]


def test_many_unknown_ids_collapse_into_one_warning(tmp_path):
    rows = tuple(f"PSL_NOVEL_{i},R1,TRUE,SINGLE_CHOICE,N" for i in range(10))
    report = preflight_csv(write_csv(tmp_path, csv_text(*rows)))
    assert len(report["unknown_question_ids"]) == 10
    id_warnings = [w for w in report["warnings"] if "snapshot" in w]
    assert len(id_warnings) == 1
    assert "per-question warnings are suppressed" in id_warnings[0]


def test_an_empty_snapshot_produces_silence(tmp_path, monkeypatch):
    monkeypatch.setattr(data_safety, "KNOWN_ID_TOKENS", frozenset())
    report = preflight_csv(write_csv(tmp_path, csv_text("PSL_ANYTHING,R1,TRUE,SINGLE_CHOICE,N")))
    assert not [w for w in report["warnings"] if "snapshot" in w]


def test_a_complete_snapshot_drops_the_partial_qualifier(tmp_path, monkeypatch):
    monkeypatch.setattr(data_safety, "SNAPSHOT_PARTIAL", False)
    report = preflight_csv(write_csv(tmp_path, csv_text("PSL_BRAND_NEW,R1,TRUE,SINGLE_CHOICE,N")))
    assert "not in the snapshot" in report["warnings"][0]


# ---------------------------------------------------------------------------
# The declaration itself
# ---------------------------------------------------------------------------


def test_declared_lists_only_the_yes_answers(good_csv):
    parsed = load_csv(good_csv)
    entries = declared(parsed)
    assert [e["question_id"] for e in entries] == [
        "PSL_DATA_TYPES_PERSONAL",
        "PSL_SUPPORT_DATA_DELETION_BY_USER",
    ]
    assert entries[0]["selected"] == ["PSL_NAME"]
    assert preflight_csv(good_csv)["questions_not_declared"] == 0


def test_declared_is_empty_when_nothing_is_collected(tmp_path):
    path = write_csv(tmp_path, csv_text("PSL_NAME,R1,FALSE,MULTIPLE_CHOICE,N"))
    assert declared(load_csv(path)) == []


def test_build_request_sends_the_text_unchanged(good_csv):
    parsed = load_csv(good_csv)
    assert build_request(parsed) == {"safetyLabels": parsed.text}


def test_stats_count_what_the_preview_reports(good_csv):
    report = validate(load_csv(good_csv))
    assert report["stats"]["questions_answered"] == 2
    assert report["stats"]["by_requirement"] == {"MULTIPLE_CHOICE": 1, "SINGLE_CHOICE": 1}


def test_maybe_required_blanks_are_counted_not_flagged(tmp_path):
    path = write_csv(tmp_path, csv_text("PSL_NAME,R1,,MAYBE_REQUIRED,N"))
    report = validate(load_csv(path))
    assert report["stats"]["maybe_required_blank"] == 1
    assert report["valid"] is True


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _make_http_error(reason: str = "boom") -> HttpError:
    resp = MagicMock()
    resp.status = 400
    resp.reason = reason
    err = HttpError(resp, b"{}")
    err.reason = reason
    return err


def _client(service: MagicMock) -> PlayStoreClient:
    client = PlayStoreClient(credentials_json={"type": "service_account"})
    client._service = verified(service)
    return client


def test_client_calls_the_real_api_method():
    service = MagicMock()
    service.applications.return_value.dataSafety.return_value.execute.return_value = {}
    labels = {"safetyLabels": "col1,col2\nval1,val2"}

    result = _client(service).set_data_safety(PKG, labels)

    assert isinstance(result, DataSafetyResult)
    assert result.success is True
    assert result.package_name == PKG
    service.applications.return_value.dataSafety.assert_called_once_with(
        packageName=PKG, body=labels
    )


def test_client_wraps_an_http_error():
    service = MagicMock()
    service.applications.return_value.dataSafety.return_value.execute.side_effect = (
        _make_http_error("bad")
    )
    with pytest.raises(PlayStoreClientError, match="Failed to update data safety labels"):
        _client(service).set_data_safety(PKG, {"safetyLabels": "csv"})


def test_data_safety_result_defaults():
    result = DataSafetyResult(success=True, package_name=PKG, message="Data safety labels updated")
    assert result.error is None


# ---------------------------------------------------------------------------
# preflight_data_safety_csv
# ---------------------------------------------------------------------------


def test_preflight_tool_returns_the_report(good_csv):
    assert server.preflight_data_safety_csv(csv_path=good_csv)["valid"] is True


def test_preflight_tool_renders_a_refusal_as_an_error_object():
    result = server.preflight_data_safety_csv(csv_path="/nonexistent/ds.csv")
    assert result["error"] == "validation_error"
    assert "Export to CSV" in result["remedy"]


# ---------------------------------------------------------------------------
# set_data_safety
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client(monkeypatch):
    monkeypatch.setattr(server, "READ_ONLY", False)
    client = MagicMock()
    client.set_data_safety.return_value = DataSafetyResult(
        success=True, package_name=PKG, message="Data safety labels updated"
    )
    monkeypatch.setattr(server, "get_client_from_context", lambda: client)
    return client


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"csv_path": "x.csv", "safety_labels": {"safetyLabels": "csv"}}],
    ids=["neither", "both"],
)
def test_exactly_one_input_is_required(mock_client, kwargs):
    result = server.set_data_safety(package_name=PKG, **kwargs)
    assert result["error"] == "validation_error"
    assert "exactly one" in result["message"]
    mock_client.set_data_safety.assert_not_called()


def test_a_missing_csv_is_reported_before_the_gate(mock_client):
    result = server.set_data_safety(package_name=PKG, csv_path="/nonexistent/ds.csv")
    assert result["error"] == "validation_error"
    mock_client.set_data_safety.assert_not_called()


def test_an_invalid_csv_is_never_sent(mock_client, tmp_path):
    path = write_csv(tmp_path, csv_text("PSL_NAME,R1,Yes,SINGLE_CHOICE,N"))

    result = server.set_data_safety(package_name=PKG, csv_path=path)

    assert result["error"] == "validation_error"
    assert result["report"]["errors"]
    assert "preflight_data_safety_csv" in result["remedy"]
    mock_client.set_data_safety.assert_not_called()


def test_a_valid_csv_is_transmitted_byte_for_byte(mock_client, good_csv):
    result = confirm_via_gate(server.set_data_safety, package_name=PKG, csv_path=good_csv)

    assert result["success"] is True
    assert result["validated"] is True
    assert result["rows_submitted"] == 4
    assert result["questions_submitted"] == 2
    sent = mock_client.set_data_safety.call_args.kwargs["safety_labels"]
    assert sent == {"safetyLabels": Path(good_csv).read_text()}
    assert result["csv_sha256"] == load_csv(good_csv).sha256


@pytest.mark.usefixtures("mock_client")
def test_the_preview_shows_the_declaration_not_the_csv(good_csv):
    preview = server.set_data_safety(package_name=PKG, csv_path=good_csv, confirm=False)

    block = preview["preview"]
    assert preview["confirmation_required"] is True
    # The whole point: a human can read this.
    assert "Personal info / Name" in block
    assert "2 questions, 2 answered YES" in block
    assert "no API to read the current declaration" in block
    assert "PSL_DATA_TYPES_PERSONAL,PSL_NAME,TRUE" not in block


@pytest.mark.usefixtures("mock_client")
def test_a_large_csv_does_not_drown_the_preview(tmp_path):
    rows = tuple(
        f"PSL_DATA_TYPES_Q{i},PSL_R{i},{'TRUE' if i % 3 == 0 else 'FALSE'},"
        f"MULTIPLE_CHOICE,Question number {i}"
        for i in range(800)
    )
    path = write_csv(tmp_path, csv_text(*rows))
    assert len(Path(path).read_text()) > 40_000

    block = server.set_data_safety(package_name=PKG, csv_path=path)["preview"]

    assert len(block) < 6_000
    assert "and 12 more" in block or "more (see preflight_data_safety_csv)" in block


def test_the_token_is_bound_to_the_answers_not_the_path(mock_client, tmp_path):
    path = write_csv(tmp_path, csv_text(*VALID_ROWS))
    preview = server.set_data_safety(package_name=PKG, csv_path=path)
    token = _token(preview)

    # Same path, different answers -> the approval no longer applies.
    write_csv(tmp_path, csv_text("PSL_NAME,R1,FALSE,SINGLE_CHOICE,N"))
    refused = server.set_data_safety(
        package_name=PKG, csv_path=path, confirm=True, confirmation_token=token
    )

    assert refused.get("error") == "guard_error"
    mock_client.set_data_safety.assert_not_called()


@pytest.mark.usefixtures("mock_client")
def test_an_unchanged_file_still_confirms(tmp_path):
    path = write_csv(tmp_path, csv_text(*VALID_ROWS))
    token = _token(server.set_data_safety(package_name=PKG, csv_path=path))

    result = server.set_data_safety(
        package_name=PKG, csv_path=path, confirm=True, confirmation_token=token
    )

    assert result["success"] is True


def _token(preview: dict) -> str:
    import re

    match = re.search(r'confirmation_token="([^"]+)"', preview["preview"])
    assert match
    return match.group(1)


# --- the inline escape hatch ---


def test_inline_labels_still_work(mock_client):
    labels = {"safetyLabels": csv_text(*VALID_ROWS)}

    result = confirm_via_gate(server.set_data_safety, package_name=PKG, safety_labels=labels)

    assert result["success"] is True
    assert result["validated"] is False
    mock_client.set_data_safety.assert_called_once_with(
        package_name=PKG, safety_labels={"safetyLabels": labels["safetyLabels"]}
    )


@pytest.mark.usefixtures("mock_client")
def test_inline_labels_report_structure_without_enforcing_it():
    # The validator encodes an unpublished format Google changes without notice,
    # so there has to be a way past it that costs the caller a deliberate act.
    labels = {"safetyLabels": csv_text("PSL_NAME,R1,Yes,SINGLE_CHOICE,N")}

    preview = server.set_data_safety(package_name=PKG, safety_labels=labels)

    assert preview["confirmation_required"] is True
    assert "NOT enforced" in preview["preview"]
    assert "Structure check (not enforced)" in preview["preview"]


@pytest.mark.parametrize("labels", [{}, {"safetyLabels": 42}, {"safetyLabels": "  "}])
@pytest.mark.usefixtures("mock_client")
def test_inline_labels_must_carry_the_csv(labels):
    result = server.set_data_safety(package_name=PKG, safety_labels=labels)
    assert result["error"] == "validation_error"
    assert "safetyLabels" in result["message"]


@pytest.mark.usefixtures("mock_client")
def test_a_declaration_of_nothing_says_so(tmp_path):
    path = write_csv(tmp_path, csv_text("PSL_NAME,R1,FALSE,MULTIPLE_CHOICE,N"))
    block = server.set_data_safety(package_name=PKG, csv_path=path)["preview"]
    assert "collects and shares no user data" in block


def test_read_only_blocks_the_csv_path(monkeypatch, good_csv):
    monkeypatch.setattr(server, "READ_ONLY", True)
    result = server.set_data_safety(package_name=PKG, csv_path=good_csv)
    assert "read-only" in str(result).lower()


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------


def test_the_prompt_leads_with_the_export():
    text = server.declare_data_safety(package_name=PKG)
    assert "Export to CSV" in text
    assert "no API to read the current one back" in text
    # The export is step 1 and the write is step 5: the rollback has to be
    # taken before anything is written, because nothing else can produce one.
    assert text.index("Export to CSV") < text.index("set_data_safety(package_name=")
