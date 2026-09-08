"""Durable record of every tool call that failed.

The MCP client's own logs record that a tool *completed*, never what it
returned. A tool that answers ``{"error": ...}`` — which this server does for
validation failures, missing configuration and partial successes — is written
down as a success, so the largest class of failure is invisible to the only
log a user would think to read. Reconstructing it means archaeology through
session transcripts.

This middleware closes that gap from the server side: it watches every tool
call and appends the failures to a JSONL file, whether the tool raised or
merely returned an error-shaped payload. The file is the artefact you grep
three weeks later when someone says "it's been failing a lot".

Two rules the implementation must never break:

1. **Auditing cannot break a call.** Every write is best-effort; a full disk,
   a read-only home directory or an unserializable argument must cost the
   caller nothing. Failures here are swallowed deliberately.
2. **Arguments are not automatically safe to keep.** Purchase tokens,
   credentials and confirmation tokens all arrive as ordinary tool arguments,
   so anything whose name looks sensitive is redacted before it is written.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from fastmcp.server.middleware import Middleware

logger = structlog.get_logger(__name__)

#: Env var pointing the audit log somewhere other than the default.
AUDIT_LOG_ENV = "GOOGLE_PLAY_MCP_AUDIT_LOG"

#: Env var that switches the audit log off entirely.
AUDIT_DISABLE_ENV = "GOOGLE_PLAY_MCP_NO_AUDIT_LOG"

#: Rotate once the file passes this size, keeping one previous generation. The
#: log is a triage aid, not an archive — unbounded growth in a user's home
#: directory would be a worse bug than the one it exists to catch.
MAX_LOG_BYTES = 5 * 1024 * 1024

#: Argument names whose values must never be written down. Matched against the
#: name with case and separators stripped, so purchase_token and
#: confirmation_token are both caught by "token", and apiKey, api_key and
#: API-KEY all match "apikey" — Play request bodies are camelCase, tool
#: arguments are snake_case, and both reach this middleware.
SENSITIVE_ARGUMENT_MARKERS = ("token", "credential", "secret", "password", "apikey")

#: Longer argument values are truncated: the audit log records *which* call
#: failed, not a copy of its payload.
MAX_VALUE_CHARS = 200


def audit_log_path() -> Path | None:
    """Where failures are recorded, or None when auditing is switched off."""
    if os.environ.get(AUDIT_DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return None
    override = os.environ.get(AUDIT_LOG_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "google-play-console-mcp" / "tool-failures.jsonl"


def redact_arguments(arguments: Any) -> Any:
    """Copy the arguments with sensitive values replaced and long ones cut."""
    if not isinstance(arguments, dict):
        return _shorten(arguments)
    redacted: dict[str, Any] = {}
    for key, value in arguments.items():
        normalized = _normalize_key(key)
        if any(marker in normalized for marker in SENSITIVE_ARGUMENT_MARKERS):
            redacted[key] = "<redacted>"
        elif isinstance(value, dict):
            redacted[key] = redact_arguments(value)
        else:
            redacted[key] = _shorten(value)
    return redacted


def _normalize_key(key: Any) -> str:
    """Reduce an argument name to letters and digits, lowercased."""
    return "".join(c for c in str(key).lower() if c.isalnum())


def _shorten(value: Any) -> Any:
    """Truncate a value that is too long to be worth keeping in full."""
    if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
        return value[:MAX_VALUE_CHARS] + f"... (+{len(value) - MAX_VALUE_CHARS} chars)"
    if isinstance(value, list) and len(value) > 10:
        return [_shorten(v) for v in value[:10]] + [f"... (+{len(value) - 10} more)"]
    return value


def result_failure(result: Any) -> str | None:
    """Describe how a tool result represents a failure, or None if it does not.

    Covers the three shapes this server answers with — an explicit MCP error
    flag, an ``{"error": ...}`` payload, and a ``{"success": false}`` /
    ``{"ok": false}`` report — because all three reach the caller as a
    successful call and would otherwise go unrecorded.
    """
    if getattr(result, "is_error", False):
        return "tool reported is_error"

    payload = getattr(result, "structured_content", None)
    if not isinstance(payload, dict):
        return None

    # fastmcp wraps a non-dict return (this server's list-returning tools) as
    # {"result": [...]}, so unwrap before looking for error keys.
    candidates: list[Any] = [payload]
    if set(payload) == {"result"}:
        inner = payload["result"]
        candidates = inner if isinstance(inner, list) else [inner]

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("error"):
            # The error key is often a bare code ("validation_error"); the
            # message beside it is what actually says what went wrong.
            detail = _stringify(candidate["error"])
            message = candidate.get("message")
            if message and _stringify(message) != detail:
                detail = f"{detail} — {_stringify(message)}"
            return f"error: {detail}"
        if candidate.get("success") is False or candidate.get("ok") is False:
            message = candidate.get("message") or candidate.get("error") or "no detail"
            return f"unsuccessful: {_stringify(message)}"
    return None


def _stringify(value: Any) -> str:
    """Render a failure detail as a short single-line string."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text[:MAX_VALUE_CHARS]


class ToolCallAuditor(Middleware):
    """Record every failing tool call, raised or returned.

    Args:
        path: Where to append records. Defaults to :func:`audit_log_path`;
            pass None explicitly to log to stderr only.
    """

    def __init__(self, path: Path | str | None = "default") -> None:
        self._path = audit_log_path() if path == "default" else (Path(path) if path else None)

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        name = getattr(context.message, "name", "unknown")
        arguments = getattr(context.message, "arguments", None)
        started = time.monotonic()

        try:
            result = await call_next(context)
        except Exception as e:
            self._record(name, arguments, "raised", f"{type(e).__name__}: {e}", started)
            raise

        failure = result_failure(result)
        if failure is not None:
            self._record(name, arguments, "returned_error", failure, started)
        return result

    def _record(self, tool: str, arguments: Any, outcome: str, detail: str, started: float) -> None:
        """Write one failure to the log and to stderr. Never raises."""
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": tool,
            "outcome": outcome,
            "detail": _stringify(detail),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "arguments": redact_arguments(arguments),
        }
        logger.warning(
            "Tool call failed",
            tool=tool,
            outcome=outcome,
            detail=record["detail"],
            duration_ms=record["duration_ms"],
        )
        self._append(record)

    def _append(self, record: dict[str, Any]) -> None:
        """Append one JSON line, rotating first if the file has grown large."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists() and self._path.stat().st_size > MAX_LOG_BYTES:
                self._path.replace(self._path.with_suffix(self._path.suffix + ".1"))
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except OSError as e:
            # Auditing must never be the reason a tool call fails, so a full
            # disk or an unwritable home directory costs the caller nothing.
            logger.debug("Could not write the audit log", path=str(self._path), error=str(e))
