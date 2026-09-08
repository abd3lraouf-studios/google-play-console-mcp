"""Tests for the failing-tool-call audit log.

The point of this module is that a tool which *returns* an error looks like a
success to every log outside the server, so these tests care most about the
returned-error shapes rather than the raised ones.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from google_play_console_mcp.observability import (
    AUDIT_DISABLE_ENV,
    AUDIT_LOG_ENV,
    MAX_LOG_BYTES,
    ToolCallAuditor,
    audit_log_path,
    redact_arguments,
    result_failure,
)


class _Result:
    """Stand-in for fastmcp's ToolResult."""

    def __init__(self, structured_content: Any = None, is_error: bool = False) -> None:
        self.structured_content = structured_content
        self.is_error = is_error


class _Message:
    def __init__(self, name: str, arguments: Any) -> None:
        self.name = name
        self.arguments = arguments


class _Context:
    def __init__(self, name: str = "get_listing", arguments: Any = None) -> None:
        self.message = _Message(name, arguments or {})


async def _call(auditor: ToolCallAuditor, result: Any, context: _Context | None = None) -> Any:
    async def call_next(_ctx: Any) -> Any:
        if isinstance(result, Exception):
            raise result
        return result

    return await auditor.on_call_tool(context or _Context(), call_next)


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Recognising a failure
# ---------------------------------------------------------------------------


def test_result_failure_sees_an_error_payload():
    # The invisible class: a successful call carrying an error.
    failure = result_failure(_Result({"error": "validation_error", "message": "bad csv"}))
    assert failure is not None
    # The code alone ("validation_error") says nothing; keep the message too.
    assert "validation_error" in failure
    assert "bad csv" in failure


def test_result_failure_does_not_repeat_an_error_that_is_its_own_message():
    assert result_failure(_Result({"error": "boom", "message": "boom"})) == "error: boom"


def test_result_failure_sees_an_unsuccessful_report():
    failure = result_failure(_Result({"success": False, "message": "track upgraded"}))
    assert failure is not None
    assert "track upgraded" in failure


def test_result_failure_sees_a_false_ok_flag():
    failure = result_failure(_Result({"ok": False, "message": "no reports bucket"}))
    assert failure is not None
    assert "no reports bucket" in failure


def test_result_failure_sees_the_mcp_error_flag():
    assert result_failure(_Result({"anything": 1}, is_error=True)) == "tool reported is_error"


def test_result_failure_unwraps_a_list_return():
    # fastmcp wraps this server's list-returning tools as {"result": [...]}.
    wrapped = _Result({"result": [{"error": "boom"}]})
    assert result_failure(wrapped) == "error: boom"


def test_result_failure_skips_non_dict_items_in_a_list_result():
    # Some tools answer a list of plain values; there is no error key to find.
    assert result_failure(_Result({"result": ["premium", "free"]})) is None


def test_result_failure_unwraps_a_single_wrapped_object():
    assert result_failure(_Result({"result": {"error": "boom"}})) == "error: boom"


def test_result_failure_ignores_a_healthy_result():
    assert result_failure(_Result({"success": True, "error": None})) is None
    assert result_failure(_Result({"result": [{"product_id": "premium"}]})) is None
    assert result_failure(_Result(None)) is None


def test_result_failure_ignores_an_error_key_that_is_empty():
    # Several tools answer {"success": true, ..., "error": null}; that is a
    # success and must not be recorded as a failure.
    assert result_failure(_Result({"success": True, "error": None})) is None
    assert result_failure(_Result({"error": ""})) is None


# ---------------------------------------------------------------------------
# Writing the record
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_records_a_returned_error(tmp_path: Path):
    path = tmp_path / "failures.jsonl"
    auditor = ToolCallAuditor(path)

    await _call(
        auditor,
        _Result({"error": "validation_error", "message": "No such file"}),
        _Context("preflight_screenshot_set", {"path": "missing.png"}),
    )

    (record,) = _records(path)
    assert record["tool"] == "preflight_screenshot_set"
    assert record["outcome"] == "returned_error"
    assert "validation_error" in record["detail"]
    assert record["arguments"] == {"path": "missing.png"}
    assert isinstance(record["duration_ms"], int)
    assert record["timestamp"]


@pytest.mark.anyio
async def test_records_a_raised_error_and_re_raises_it(tmp_path: Path):
    path = tmp_path / "failures.jsonl"
    auditor = ToolCallAuditor(path)

    with pytest.raises(BrokenPipeError):
        await _call(auditor, BrokenPipeError(32, "Broken pipe"), _Context("create_edit"))

    (record,) = _records(path)
    assert record["outcome"] == "raised"
    assert "BrokenPipeError" in record["detail"]


@pytest.mark.anyio
async def test_records_nothing_for_a_successful_call(tmp_path: Path):
    path = tmp_path / "failures.jsonl"
    result = _Result({"result": [{"product_id": "premium"}]})

    assert await _call(ToolCallAuditor(path), result) is result
    assert not path.exists()


@pytest.mark.anyio
async def test_appends_rather_than_overwriting(tmp_path: Path):
    path = tmp_path / "failures.jsonl"
    auditor = ToolCallAuditor(path)

    for i in range(3):
        await _call(auditor, _Result({"error": f"boom {i}"}))

    assert len(_records(path)) == 3


@pytest.mark.anyio
async def test_redacts_sensitive_arguments(tmp_path: Path):
    path = tmp_path / "failures.jsonl"
    auditor = ToolCallAuditor(path)

    await _call(
        auditor,
        _Result({"error": "nope"}),
        _Context(
            "get_subscription_status",
            {
                "package_name": "app.tamatem.android",
                "purchase_token": "super-secret",
                "confirmation_token": "also-secret",
            },
        ),
    )

    (record,) = _records(path)
    assert record["arguments"]["package_name"] == "app.tamatem.android"
    assert record["arguments"]["purchase_token"] == "<redacted>"
    assert record["arguments"]["confirmation_token"] == "<redacted>"
    assert "super-secret" not in path.read_text()


@pytest.mark.anyio
async def test_a_failing_log_write_never_fails_the_call(tmp_path: Path):
    # The audit log is a diagnostic, so an unwritable path must cost nothing.
    unwritable = tmp_path / "file.txt"
    unwritable.write_text("not a directory")
    auditor = ToolCallAuditor(unwritable / "nested" / "failures.jsonl")

    result = _Result({"error": "boom"})
    assert await _call(auditor, result) is result


@pytest.mark.anyio
async def test_rotates_once_the_log_grows_large(tmp_path: Path):
    path = tmp_path / "failures.jsonl"
    path.write_text("x" * (MAX_LOG_BYTES + 1))
    auditor = ToolCallAuditor(path)

    await _call(auditor, _Result({"error": "boom"}))

    assert path.with_suffix(".jsonl.1").exists()
    assert len(_records(path)) == 1


@pytest.mark.anyio
async def test_logging_can_be_switched_off():
    result = _Result({"error": "boom"})
    assert await _call(ToolCallAuditor(None), result) is result


# ---------------------------------------------------------------------------
# Redaction and truncation
# ---------------------------------------------------------------------------


def test_redaction_reaches_into_nested_objects():
    redacted = redact_arguments({"body": {"apiKey": "secret", "track": "production"}})
    assert redacted["body"]["apiKey"] == "<redacted>"
    assert redacted["body"]["track"] == "production"


def test_redaction_truncates_a_long_value():
    redacted = redact_arguments({"full_description": "x" * 5000})
    assert len(redacted["full_description"]) < 300
    assert "+4800 chars" in redacted["full_description"]


def test_redaction_truncates_a_long_list():
    redacted = redact_arguments({"screenshots": [f"s{i}.png" for i in range(50)]})
    assert len(redacted["screenshots"]) == 11
    assert "+40 more" in redacted["screenshots"][-1]


def test_redaction_survives_non_dict_arguments():
    assert redact_arguments(None) is None
    assert redact_arguments("plain") == "plain"


# ---------------------------------------------------------------------------
# Where the log lives
# ---------------------------------------------------------------------------


def test_audit_log_path_honours_an_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv(AUDIT_DISABLE_ENV, raising=False)
    monkeypatch.setenv(AUDIT_LOG_ENV, str(tmp_path / "custom.jsonl"))
    assert audit_log_path() == tmp_path / "custom.jsonl"


def test_audit_log_path_can_be_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(AUDIT_DISABLE_ENV, "1")
    assert audit_log_path() is None


def test_audit_log_path_defaults_under_the_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv(AUDIT_DISABLE_ENV, raising=False)
    monkeypatch.delenv(AUDIT_LOG_ENV, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert audit_log_path() == tmp_path / "google-play-console-mcp" / "tool-failures.jsonl"


def test_redaction_matches_camel_case_and_hyphenated_names():
    # Play request bodies are camelCase and tool arguments are snake_case; both
    # reach this middleware, so neither spelling may leak.
    redacted = redact_arguments(
        {"apiKey": "a", "api_key": "b", "API-KEY": "c", "purchaseToken": "d"}
    )
    assert set(redacted.values()) == {"<redacted>"}


# ---------------------------------------------------------------------------
# Wiring: the middleware has to be on the real server, not just importable
# ---------------------------------------------------------------------------


def test_the_server_registers_the_auditor():
    import google_play_console_mcp.server as server

    assert any(isinstance(m, ToolCallAuditor) for m in server.mcp.middleware), (
        "the audit middleware must be registered on the server, or nothing is recorded"
    )
