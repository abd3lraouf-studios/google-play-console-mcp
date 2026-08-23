"""Errors that carry a remedy.

Every error a tool can return names what went wrong *and* what to do about it.
The caller is a language model: an error without a remedy gets guessed at, and
a guess against a publishing API is how a wrong release ships.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar


def redact_path(path: str | Path | None) -> str:
    """Render a filesystem path without exposing the full user directory tree.

    Credential paths usually sit under a home directory that identifies the
    user; only the last two segments carry diagnostic value.
    """
    if path is None:
        return "<not set>"
    p = Path(path)
    parts = p.parts
    if len(parts) <= 2:
        return str(p)
    return str(Path(".../") / parts[-2] / parts[-1])


class PlayConsoleError(Exception):
    """Base error. Carries a concrete remedy so the caller is never stranded."""

    kind: ClassVar[str] = "error"

    def __init__(
        self,
        message: str,
        *,
        remedy: str,
        doc_url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy
        self.doc_url = doc_url
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Render as the error object a tool returns."""
        data: dict[str, Any] = {
            "error": self.kind,
            "message": self.message,
            "remedy": self.remedy,
        }
        if self.doc_url:
            data["doc_url"] = self.doc_url
        if self.details:
            data["details"] = self.details
        return data


class ValidationError(PlayConsoleError):
    """Input, or an upstream payload's shape, failed validation."""

    kind = "validation_error"


class GuardError(PlayConsoleError):
    """A write was refused by the confirmation gate."""

    kind = "guard_error"


def render_error(exc: BaseException) -> dict[str, Any]:
    """Render any exception into an object a caller can act on.

    Always safe to call: an exception that is not a PlayConsoleError degrades to
    type plus message, so a tool can return this instead of crashing the server.
    """
    if isinstance(exc, PlayConsoleError):
        return exc.to_dict()
    return {
        "error": "unexpected_error",
        "message": f"{type(exc).__name__}: {exc}",
        "remedy": "This was not an anticipated failure. Check the server logs for a traceback.",
    }
