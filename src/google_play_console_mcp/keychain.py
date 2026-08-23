"""Credential storage in the OS keychain.

A service-account key is a bearer credential for a Play Console account: it can
ship a release and read what the account earned. The usual ways of pointing a
server at one leave it in the clear — a JSON file on disk that the whole
process tree can read, or a path baked into an agent's config file that syncs
to a dotfiles repo. This module keeps the key material in the platform's own
secret store instead: macOS Keychain, Windows Credential Locker, or a
Freedesktop Secret Service (GNOME Keyring, KWallet) on Linux.

``keyring`` does the platform work. It is already a runtime dependency of the
server by way of ``fastmcp``, so nothing new is pulled in to get this, but it is
declared directly in ``pyproject.toml`` because this module depends on it in its
own right and a transitive pin is not a promise.

Storage is never assumed to work. A headless Linux box with no Secret Service
running, a locked keychain, a CI container — all of them raise, and all of them
are a normal thing for this server to run inside. Every entry point here reports
failure rather than raising into a tool call, so the resolution chain in
:mod:`google_play_console_mcp.credentials` can simply move on to the next
source.

On macOS the first read from a newly installed binary raises a Keychain prompt.
That is the OS asking whether *this* executable may read the item, and it is
worth clicking "Always Allow": a GUI agent such as Claude Desktop launches the
server without a terminal, so a prompt nobody sees looks exactly like a hang.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

SERVICE_NAME = "google-play-console-mcp"
"""Keychain service the entries live under."""

DEFAULT_ACCOUNT = "service-account"
"""Entry name within the service. Named rather than fixed so more than one Play
account can be kept side by side."""


class KeychainUnavailableError(RuntimeError):
    """The platform secret store could not be reached."""


def _keyring() -> Any:
    """Import ``keyring`` lazily.

    Kept out of module import so that a broken or absent backend cannot stop the
    server from starting — the import itself touches platform libraries.
    """
    try:
        import keyring  # noqa: PLC0415 - deliberately lazy; see docstring
    except ImportError as exc:  # pragma: no cover - keyring ships with fastmcp
        raise KeychainUnavailableError(f"keyring is not installed: {exc}") from exc
    return keyring


def is_available() -> bool:
    """True when a usable secret-store backend is present."""
    try:
        keyring = _keyring()
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - any probe failure means "no usable store"
        return False
    # keyring falls back to a "fail" backend when no real store is available;
    # its priority is below 1 by convention.
    return getattr(backend, "priority", 0) >= 1


def backend_name() -> str:
    """Human-readable name of the active backend, for status output."""
    try:
        keyring = _keyring()
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - any probe failure means "no usable store"
        return "unavailable"
    # Qualify it: every backend class is named "Keyring", so the bare class name
    # says nothing about which store is actually in use.
    module = type(backend).__module__.rsplit(".", 1)[-1]
    return f"{module}.{type(backend).__name__}"


def store(secret: str, *, account: str = DEFAULT_ACCOUNT) -> None:
    """Put ``secret`` in the keychain under ``account``.

    Raises:
        KeychainUnavailableError: the store could not be written.
    """
    try:
        _keyring().set_password(SERVICE_NAME, account, secret)
    except Exception as exc:
        raise KeychainUnavailableError(f"Could not write to the keychain: {exc}") from exc


def load(*, account: str = DEFAULT_ACCOUNT) -> str | None:
    """Read the secret stored under ``account``, or None if there is none.

    Never raises: an unreachable keychain is indistinguishable from an empty one
    as far as the resolution chain is concerned, and a credential lookup is not
    the place to take the server down.
    """
    try:
        return _keyring().get_password(SERVICE_NAME, account)  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.debug("Keychain lookup failed", account=account, error=str(exc))
        return None


def delete(*, account: str = DEFAULT_ACCOUNT) -> bool:
    """Remove the entry. True if one was removed, False if there was nothing."""
    try:
        keyring = _keyring()
        if keyring.get_password(SERVICE_NAME, account) is None:
            return False
        keyring.delete_password(SERVICE_NAME, account)
    except Exception as exc:  # noqa: BLE001 - absent entry, locked store, no backend
        logger.debug("Keychain delete failed", account=account, error=str(exc))
        return False
    return True


def describe_stored(*, account: str = DEFAULT_ACCOUNT) -> dict[str, Any] | None:
    """Identify the stored key without revealing it.

    Returns the service-account e-mail and project, which are the two fields
    that tell you *which* credential is in there. The private key is never
    returned, logged, or rendered.
    """
    raw = load(account=account)
    if raw is None:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return {"account": account, "type": "opaque"}
    return {
        "account": account,
        "type": info.get("type", "unknown"),
        "client_email": info.get("client_email"),
        "project_id": info.get("project_id"),
    }
