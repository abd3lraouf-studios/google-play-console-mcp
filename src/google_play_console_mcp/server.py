"""Google Play Console MCP Server - Main server implementation."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from google_play_console_mcp.client import PlayStoreClient, PlayStoreClientError
from google_play_console_mcp.edits import READ_ONLY_HTTP_METHODS
from google_play_console_mcp.errors import PlayConsoleError
from google_play_console_mcp.guards import (
    PRODUCTION_POLICY,
    UNTRUSTED_CONTENT_NOTE,
    Change,
    Operation,
    Preview,
    audit_execution,
    audit_log_path,
    is_production_track,
    require_confirmation,
    target_for,
    unguarded,
)
from google_play_console_mcp.install import run_cli as run_install_cli
from google_play_console_mcp.reporting import METRIC_SETS, ReportingClientError
from google_play_console_mcp.reports import (
    earnings_by_app,
    earnings_by_currency,
    group_rows,
    list_families,
)
from google_play_console_mcp.scraping import (
    PRICE_FILTERS,
    list_age_filters,
    list_categories,
    list_collections,
    list_sort_orders,
)
from google_play_console_mcp.scraping import (
    check_aso_metadata as _check_aso_metadata,
)
from google_play_console_mcp.store_images import (
    list_image_specs,
    preflight_image,
)
from google_play_console_mcp.store_images import (
    preflight_screenshot_set as _preflight_screenshot_set,
)

# Configure structured logging to stderr (stdout is reserved for MCP JSON-RPC)
log_level = os.environ.get("GOOGLE_PLAY_MCP_LOG_LEVEL", "INFO")
numeric_level = getattr(logging, log_level.upper(), logging.INFO)
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)
if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger(__name__)


# =============================================================================
# Write-safety gate
# =============================================================================
#
# GOOGLE_PLAY_MCP_READ_ONLY refuses every write outright. The gate is the finer
# instrument: a human reads exactly what one write will do, approves it, and the
# token binds that approval to those exact arguments. See google_play_console_mcp/guards.py
# for the threat model, and docs/write-safety.md for the operator's view.


def _gate(
    op: Operation,
    preview: Preview | Callable[[], Preview],
    *,
    confirm: bool,
    confirmation_token: str | None,
    token_required: bool = True,
) -> dict[str, Any] | None:
    """Run the confirmation gate for ``op``.

    Returns a dict the tool must return immediately — either the preview block
    or a guard error — or None when the call is authorised and the tool may
    proceed to mutate.

    ``preview`` may be a callable so an expensive one — commit_edit reads the
    staged edit back from Play — is only paid for on the preview leg.
    """
    try:
        pending = require_confirmation(
            op,
            preview,
            confirm=confirm,
            confirmation_token=confirmation_token,
            token_required=token_required,
        )
    except PlayConsoleError as exc:
        return exc.to_dict()

    if pending is not None:
        return {"confirmation_required": True, "preview": pending}
    return None


def _rollout_note(rollout_percentage: float, track: str) -> list[str]:
    """Warnings a human should see before a release changes who receives it."""
    notes: list[str] = []
    if is_production_track(track):
        notes.append(f"Track '{track}' is PRODUCTION — this reaches real users of the live app.")
    if rollout_percentage >= 100.0:
        notes.append("Rollout is 100%: every eligible user on this track receives it at once.")
    return notes


def _report_tool(kind: str, package_name: str, month: str, dimension: str) -> dict[str, Any]:
    """Shared body for the per-app bulk-report tools.

    Rows are collapsed to per-dimension totals: a month of country-sliced
    installs is a few thousand rows, and an agent needs the shape, not the file.
    The freshness block always travels with the numbers.
    """
    client = get_client_from_context()
    try:
        report = client.get_stats_report(kind, package_name, month, dimension=dimension)
    except PlayConsoleError as exc:
        return exc.to_dict()

    return {
        "package_name": package_name,
        "month": month,
        "dimension": dimension,
        "totals": report.totals,
        "breakdown": group_rows(report),
        "row_count": len(report.rows),
        "freshness": report.freshness.model_dump(),
        "source_object": report.source_object,
    }


def get_client_from_context() -> PlayStoreClient:
    """Resolve a PlayStoreClient for the current request.

    Per-request credentials in the X-Google-Credentials /
    X-Google-Credentials-Base64 headers take precedence; otherwise the shared
    client from the lifespan is used.

    Raises:
        PlayStoreClientError: if credentials are invalid or unavailable.
    """
    headers = get_http_headers() or {}

    if "x-google-credentials" in headers:
        try:
            creds_json = json.loads(headers["x-google-credentials"])
        except json.JSONDecodeError as e:
            raise PlayStoreClientError(f"Invalid JSON in X-Google-Credentials header: {e}") from e
        return PlayStoreClient(credentials_json=creds_json)

    if "x-google-credentials-base64" in headers:
        try:
            creds_bytes = base64.b64decode(headers["x-google-credentials-base64"])
            creds_json = json.loads(creds_bytes.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise PlayStoreClientError(
                f"Invalid base64 or JSON in X-Google-Credentials-Base64 header: {e}"
            ) from e
        return PlayStoreClient(credentials_json=creds_json)

    client: PlayStoreClient | None = _shared_state.get("client")
    if client is not None:
        return client

    raise PlayStoreClientError(
        "No credentials provided. Set X-Google-Credentials or X-Google-Credentials-Base64 header, "
        "or configure server with GOOGLE_PLAY_STORE_CREDENTIALS environment variable."
    )


# Shared fallback client, used when a request carries no per-request
# credential header. Populated by the lifespan on startup and swapped by the
# /credentials route. Module-level so custom routes and get_client_from_context
# can reach it without depending on framework-internal context plumbing.
_shared_state: dict[str, Any] = {"client": None, "credentials_updated": False}


@asynccontextmanager
async def lifespan(_server: FastMCP):  # type: ignore[no-untyped-def]
    """Initialize the shared PlayStoreClient on startup."""
    logger.info("Initializing Google Play Console MCP Server")
    try:
        client = PlayStoreClient()
        # Validate credentials off the event loop — _get_service() does blocking
        # discovery/auth, matching the offload used by the /credentials route.
        _ = await asyncio.to_thread(client._get_service)
        logger.info("Play Store client initialized successfully")
        _shared_state["client"] = client
    except PlayStoreClientError as e:
        logger.warning("Play Store client initialization failed", error=str(e))
        _shared_state["client"] = None

    yield _shared_state

    logger.info("Shutting down Google Play Console MCP Server")


def _validate_deploy_file(file_path: str) -> str | None:
    """Return error message if file_path is invalid, None if valid."""
    resolved = os.path.realpath(file_path)
    if not resolved.lower().endswith((".apk", ".aab")):
        return "file_path must be a .apk or .aab file"
    if not Path(resolved).is_file():
        return f"File not found: {resolved}"
    return None


def _validate_rollout(pct: float) -> str | None:
    """Return error message if rollout percentage is invalid, None if valid."""
    if not (0.0 <= pct <= 100.0):
        return "rollout_percentage must be between 0.0 and 100.0"
    return None


def _env_read_only() -> bool:
    """Return True if GOOGLE_PLAY_MCP_READ_ONLY is set to a truthy value."""
    return os.environ.get("GOOGLE_PLAY_MCP_READ_ONLY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# When True, all write/mutating tools are disabled. Initialized from the
# environment at import time; may be overridden by the --read-only CLI flag.
READ_ONLY: bool = _env_read_only()

READ_ONLY_ERROR = (
    "Server is running in read-only mode; write operations are disabled. "
    "Unset GOOGLE_PLAY_MCP_READ_ONLY (or omit --read-only) to enable writes."
)


def set_read_only(value: bool) -> None:
    """Set the process-wide read-only flag."""
    global READ_ONLY
    READ_ONLY = value


def _read_only_block(operation: str) -> dict[str, Any] | None:
    """Return an error object if read-only mode blocks a write, else None."""
    if READ_ONLY:
        logger.warning("Blocked write operation in read-only mode", operation=operation)
        return {"error": f"{READ_ONLY_ERROR} (attempted: {operation})"}
    return None


def _code_mode_enabled() -> bool:
    """Return True if CODE_MODE enables the experimental code-mode transform."""
    return os.environ.get("CODE_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def _build_transforms() -> list[Any]:
    """Return the FastMCP transforms for this process.

    When CODE_MODE is enabled, wrap the tool surface in the experimental CodeMode
    transform (search/get_schema/execute meta-tools + sandboxed execution), which
    cuts per-request tool-list overhead. Default: no transforms — the classic
    117-tool surface, unchanged.
    """
    if not _code_mode_enabled():
        return []
    # Imported lazily so the base install (without the code-mode extra) never
    # pays for it when the flag is off.
    from fastmcp.experimental.transforms.code_mode import (  # noqa: PLC0415 - lazy import: code-mode extra is optional
        CodeMode,
    )

    logger.warning(
        "CODE_MODE enabled: exposing tools via the experimental code-mode transform "
        "(search/get_schema/execute). The 'execute' sandbox requires the code-mode "
        "extra — install google-play-console-mcp[code-mode]."
    )
    return [CodeMode()]


# Initialize the MCP server
mcp = FastMCP(
    "Google Play Console MCP Server",
    lifespan=lifespan,
    transforms=_build_transforms(),
)


# =============================================================================
# Publishing Tools
# =============================================================================


@mcp.tool()
def deploy_app(
    package_name: str,
    track: str,
    file_path: str,
    release_notes: str | None = None,
    release_notes_language: str = "en-US",
    rollout_percentage: float = 100.0,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Deploy an APK or AAB file to a Play Store track.

    Two-step: call with confirm=False first, show the returned preview to the
    user, and only call again with confirm=True and the preview's
    confirmation_token once they have approved it.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        track: Release track - one of: internal, alpha, beta, production
        file_path: Absolute path to APK or AAB file
        release_notes: Optional release notes for this version (string for single language,
                      or use release_notes_multilang for multiple languages)
        release_notes_language: Language code for release notes (default: en-US)
        rollout_percentage: Rollout percentage (0-100). Default 100 for full rollout.
        confirm: Set True only after the user approved the preview
        confirmation_token: The token from that preview; never invent one

    Returns:
        The preview block when unconfirmed, otherwise the deployment result
    """
    if blocked := _read_only_block("deploy_app"):
        return blocked
    if err := _validate_deploy_file(file_path):
        return {"error": err}
    if err := _validate_rollout(rollout_percentage):
        return {"error": err}

    op = Operation(
        tool="deploy_app",
        target=target_for(package_name),
        params={
            "track": track,
            "file": Path(file_path),
            "rollout_percentage": rollout_percentage,
            "release_notes_language": release_notes_language,
            "release_notes": release_notes,
        },
        call_args={
            "package_name": package_name,
            "track": track,
            "file_path": file_path,
            "release_notes": release_notes,
            "release_notes_language": release_notes_language,
            "rollout_percentage": rollout_percentage,
        },
    )
    preview = Preview(
        summary=f"Upload {Path(file_path).name} and release it on track '{track}'.",
        changes=[
            Change("track", None, track),
            Change("rollout", None, f"{rollout_percentage:g}% of eligible users"),
        ],
        warnings=_rollout_note(rollout_percentage, track),
        reversal="halt_release stops the rollout; it cannot un-deliver installs already served.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()

    with audit_execution(op) as recorder:
        result = client.deploy_app(
            package_name=package_name,
            track=track,
            file_path=file_path,
            release_notes=release_notes,
            release_notes_language=release_notes_language,
            rollout_percentage=rollout_percentage,
        )
        recorder.set("version_code", result.version_code)

    return result.model_dump()


@mcp.tool()
def deploy_app_multilang(
    package_name: str,
    track: str,
    file_path: str,
    release_notes: dict[str, str],
    rollout_percentage: float = 100.0,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Deploy an APK or AAB file with multi-language release notes.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        track: Release track - one of: internal, alpha, beta, production
        file_path: Absolute path to APK or AAB file
        release_notes: Dictionary mapping language codes to release notes
                      (e.g., {"en-US": "Bug fixes", "es-ES": "Corrección de errores"})
        rollout_percentage: Rollout percentage (0-100). Default 100 for full rollout.

    Returns:
        Deployment result with success status and details
    """
    if blocked := _read_only_block("deploy_app_multilang"):
        return blocked
    if err := _validate_deploy_file(file_path):
        return {"error": err}
    if err := _validate_rollout(rollout_percentage):
        return {"error": err}

    op = Operation(
        tool="deploy_app_multilang",
        target=target_for(package_name),
        params={
            "track": track,
            "file": Path(file_path),
            "rollout_percentage": rollout_percentage,
            "locales": sorted(release_notes),
        },
        call_args={
            "package_name": package_name,
            "track": track,
            "file_path": file_path,
            "release_notes": release_notes,
            "rollout_percentage": rollout_percentage,
        },
    )
    preview = Preview(
        summary=f"Upload {Path(file_path).name} to track '{track}' with notes in "
        f"{len(release_notes)} locale(s).",
        changes=[
            Change("track", None, track),
            Change("rollout", None, f"{rollout_percentage:g}% of eligible users"),
        ],
        warnings=_rollout_note(rollout_percentage, track),
        reversal="halt_release stops the rollout; it cannot un-deliver installs already served.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()

    with audit_execution(op):
        result = client.deploy_app(
            package_name=package_name,
            track=track,
            file_path=file_path,
            release_notes=release_notes,
            rollout_percentage=rollout_percentage,
        )

    return result.model_dump()


@mcp.tool()
def promote_release(
    package_name: str,
    from_track: str,
    to_track: str,
    version_code: int,
    rollout_percentage: float = 100.0,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Promote a release from one track to another.

    Two-step: preview first, then confirm with the preview's token.

    Args:
        package_name: App package name
        from_track: Source track (internal, alpha, beta)
        to_track: Destination track (alpha, beta, production)
        version_code: Version code to promote
        rollout_percentage: Rollout percentage for target track (0-100)
        confirm: Set True only after the user approved the preview
        confirmation_token: The token from that preview; never invent one

    Returns:
        The preview block when unconfirmed, otherwise the promotion result
    """
    if blocked := _read_only_block("promote_release"):
        return blocked
    if err := _validate_rollout(rollout_percentage):
        return {"error": err}

    op = Operation(
        tool="promote_release",
        target=target_for(package_name),
        params={
            "from_track": from_track,
            "to_track": to_track,
            "version_code": version_code,
            "rollout_percentage": rollout_percentage,
        },
        call_args={
            "package_name": package_name,
            "from_track": from_track,
            "to_track": to_track,
            "version_code": version_code,
            "rollout_percentage": rollout_percentage,
        },
    )
    preview = Preview(
        summary=f"Promote version {version_code} from '{from_track}' to '{to_track}'.",
        changes=[
            Change("track", from_track, to_track),
            Change("rollout on target", None, f"{rollout_percentage:g}% of eligible users"),
        ],
        warnings=_rollout_note(rollout_percentage, to_track),
        reversal="halt_release stops the rollout on the destination track.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()

    with audit_execution(op):
        result = client.promote_release(
            package_name=package_name,
            from_track=from_track,
            to_track=to_track,
            version_code=version_code,
            rollout_percentage=rollout_percentage,
        )

    return result.model_dump()


@mcp.tool()
def get_releases(package_name: str) -> list[dict[str, Any]]:
    """Get release status for all tracks of an app.

    Args:
        package_name: App package name

    Returns:
        List of tracks with their releases and version information
    """
    client = get_client_from_context()

    tracks = client.get_releases(package_name)
    return [track.model_dump() for track in tracks]


@mcp.tool()
def halt_release(
    package_name: str,
    track: str,
    version_code: int,
) -> dict[str, Any]:
    """Halt a staged rollout. Takes effect immediately, no confirmation needed.

    Deliberately NOT behind the confirmation gate. Halting is the one operation
    that makes things safer, and demanding a second round-trip during an
    incident is itself the failure mode. It is still written to the audit log.

    Args:
        package_name: App package name
        track: Track containing the release (internal, alpha, beta, production)
        version_code: Version code of the release to halt

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("halt_release"):
        return blocked

    op = Operation(
        tool="halt_release",
        target=target_for(package_name),
        params={"track": track, "version_code": version_code},
    )
    client = get_client_from_context()

    with unguarded(op, reason="halting a rollout reduces blast radius; never gated"):
        result = client.halt_release(
            package_name=package_name,
            track=track,
            version_code=version_code,
        )

    return result.model_dump()


@mcp.tool()
def update_rollout(
    package_name: str,
    track: str,
    version_code: int,
    rollout_percentage: float,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Update the rollout percentage for a staged release.

    Use this to increase or decrease the percentage of users receiving an update.
    Set to 100 to complete the rollout. Two-step: preview, then confirm.

    Args:
        package_name: App package name
        track: Track containing the release
        version_code: Version code of the staged release
        rollout_percentage: New rollout percentage (0-100)
        confirm: Set True only after the user approved the preview
        confirmation_token: The token from that preview; never invent one

    Returns:
        The preview block when unconfirmed, otherwise the result
    """
    if blocked := _read_only_block("update_rollout"):
        return blocked
    if err := _validate_rollout(rollout_percentage):
        return {"error": err}

    op = Operation(
        tool="update_rollout",
        target=target_for(package_name),
        params={
            "track": track,
            "version_code": version_code,
            "rollout_percentage": rollout_percentage,
        },
        call_args={
            "package_name": package_name,
            "track": track,
            "version_code": version_code,
            "rollout_percentage": rollout_percentage,
        },
    )
    preview = Preview(
        summary=(
            f"Change the rollout of version {version_code} on '{track}' to {rollout_percentage:g}%."
        ),
        changes=[Change("rollout", None, f"{rollout_percentage:g}% of eligible users")],
        warnings=_rollout_note(rollout_percentage, track),
        reversal=(
            "Widening cannot be undone — users who received the update keep it. "
            "halt_release stops any further delivery."
        ),
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()

    with audit_execution(op):
        result = client.update_rollout(
            package_name=package_name,
            track=track,
            version_code=version_code,
            rollout_percentage=rollout_percentage,
        )

    return result.model_dump()


@mcp.tool()
def get_app_details(
    package_name: str,
    language: str = "en-US",
) -> dict[str, Any]:
    """Get app details including title, description, and developer info.

    Args:
        package_name: App package name
        language: Language code for localized content (default: en-US)

    Returns:
        App details including title, descriptions, and developer information
    """
    client = get_client_from_context()

    details = client.get_app_details(package_name, language)
    return details.model_dump()


# =============================================================================
# Reviews Tools
# =============================================================================


@mcp.tool()
def get_reviews(
    package_name: str,
    max_results: int = 50,
    translation_language: str | None = None,
) -> list[dict[str, Any]]:
    """Get recent reviews for an app.

    Args:
        package_name: App package name
        max_results: Maximum number of reviews to return (default: 50, max: 100)
        translation_language: Optional language code to translate reviews to

    Returns:
        List of reviews with ratings, comments, and author info
    """
    client = get_client_from_context()

    reviews = client.get_reviews(
        package_name=package_name,
        max_results=min(max_results, 100),
        translation_language=translation_language,
    )

    return [review.model_dump() for review in reviews]


@mcp.tool()
def reply_to_review(
    package_name: str,
    review_id: str,
    reply_text: str,
) -> dict[str, Any]:
    """Reply to a user review.

    Args:
        package_name: App package name
        review_id: ID of the review to reply to (from get_reviews)
        reply_text: Text of the reply (will be visible to the reviewer)

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("reply_to_review"):
        return blocked
    client = get_client_from_context()

    result = client.reply_to_review(
        package_name=package_name,
        review_id=review_id,
        reply_text=reply_text,
    )

    return result.model_dump()


@mcp.tool()
def get_review(
    package_name: str,
    review_id: str,
    translation_language: str | None = None,
) -> dict[str, Any]:
    """Get a single user review by its ID.

    Args:
        package_name: App package name
        review_id: Review ID (from get_reviews)
        translation_language: Optional language to translate the review to

    Returns:
        The review with rating, text, and any developer reply
    """
    client = get_client_from_context()

    review = client.get_review(
        package_name=package_name,
        review_id=review_id,
        translation_language=translation_language,
    )

    return review.model_dump()


# =============================================================================
# Subscription Tools
# =============================================================================


@mcp.tool()
def list_subscriptions(package_name: str) -> list[dict[str, Any]]:
    """List all subscription products for an app.

    Args:
        package_name: App package name

    Returns:
        List of subscription products with their base plans
    """
    client = get_client_from_context()

    subscriptions = client.list_subscriptions(package_name)
    return [sub.model_dump() for sub in subscriptions]


@mcp.tool()
def get_subscription_status(
    package_name: str,
    subscription_id: str,
    purchase_token: str,
) -> dict[str, Any]:
    """Get the status of a subscription purchase.

    Args:
        package_name: App package name
        subscription_id: Subscription product ID
        purchase_token: The purchase token from the client app

    Returns:
        Subscription purchase status including expiry and renewal info
    """
    client = get_client_from_context()

    status = client.get_subscription_purchase(
        package_name=package_name,
        subscription_id=subscription_id,
        token=purchase_token,
    )

    return status.model_dump()


@mcp.tool()
def list_voided_purchases(
    package_name: str,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    """List voided purchases (refunds, chargebacks).

    Args:
        package_name: App package name
        max_results: Maximum number of results (default: 100)

    Returns:
        List of voided purchases with reason and timing
    """
    client = get_client_from_context()

    voided = client.list_voided_purchases(
        package_name=package_name,
        max_results=max_results,
    )

    return [v.model_dump() for v in voided]


@mcp.tool()
def get_product_purchase(
    package_name: str,
    product_id: str,
    purchase_token: str,
) -> dict[str, Any]:
    """Get the status of an in-app product purchase.

    Args:
        package_name: App package name
        product_id: In-app product SKU
        purchase_token: The purchase token from the client app

    Returns:
        Product purchase status (purchase/consumption/acknowledgement state, order, region)
    """
    client = get_client_from_context()

    purchase = client.get_product_purchase(
        package_name=package_name,
        product_id=product_id,
        token=purchase_token,
    )

    return purchase.model_dump()


@mcp.tool()
def acknowledge_product_purchase(
    package_name: str,
    product_id: str,
    purchase_token: str,
    developer_payload: str | None = None,
) -> dict[str, Any]:
    """Acknowledge an in-app product purchase.

    Purchases not acknowledged within 3 days are automatically refunded.

    Args:
        package_name: App package name
        product_id: In-app product SKU
        purchase_token: The purchase token from the client app
        developer_payload: Optional payload to associate with the purchase

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("acknowledge_product_purchase"):
        return blocked
    client = get_client_from_context()

    result = client.acknowledge_product_purchase(
        package_name=package_name,
        product_id=product_id,
        token=purchase_token,
        developer_payload=developer_payload,
    )

    return result.model_dump()


@mcp.tool()
def consume_product_purchase(
    package_name: str,
    product_id: str,
    purchase_token: str,
) -> dict[str, Any]:
    """Consume an in-app product purchase (for consumable products).

    Marks the product as consumed so the user can purchase it again.

    Args:
        package_name: App package name
        product_id: In-app product SKU
        purchase_token: The purchase token from the client app

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("consume_product_purchase"):
        return blocked
    client = get_client_from_context()

    result = client.consume_product_purchase(
        package_name=package_name,
        product_id=product_id,
        token=purchase_token,
    )

    return result.model_dump()


@mcp.tool()
def refund_order(
    package_name: str,
    order_id: str,
    revoke: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Refund an order, optionally revoking the user's entitlement.

    Args:
        package_name: App package name
        order_id: Order ID to refund
        revoke: If True, also revoke the user's entitlement (default: False)

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("refund_order"):
        return blocked

    op = Operation(
        tool="refund_order",
        target=target_for(package_name),
        params={"order_id": order_id, "revoke": revoke},
        call_args={"package_name": package_name, "order_id": order_id, "revoke": revoke},
    )
    preview = Preview(
        summary=f"Refund order {order_id}"
        + (" and revoke the user's entitlement." if revoke else "."),
        changes=[Change("entitlement", "active", "revoked" if revoke else "unchanged")],
        warnings=["This moves real money. A refund cannot be reversed through the API."],
        reversal="None — a refund is final.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early
    client = get_client_from_context()

    with audit_execution(op):
        result = client.refund_order(package_name=package_name, order_id=order_id, revoke=revoke)

    return result.model_dump()


@mcp.tool()
def cancel_subscription_purchase(
    package_name: str,
    purchase_token: str,
    cancellation_type: str = "USER_REQUESTED_STOP_RENEWALS",
) -> dict[str, Any]:
    """Cancel a subscription purchase.

    Args:
        package_name: App package name
        purchase_token: The purchase token from the client app
        cancellation_type: USER_REQUESTED_STOP_RENEWALS (default),
            DEVELOPER_REQUESTED_STOP_PAYMENTS, or CANCELLATION_TYPE_UNSPECIFIED

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("cancel_subscription_purchase"):
        return blocked
    client = get_client_from_context()

    result = client.cancel_subscription_purchase(
        package_name=package_name,
        token=purchase_token,
        cancellation_type=cancellation_type,
    )

    return result.model_dump()


@mcp.tool()
def defer_subscription_purchase(
    package_name: str,
    purchase_token: str,
    defer_duration: str,
    etag: str,
) -> dict[str, Any]:
    """Defer a subscription purchase's next renewal.

    Args:
        package_name: App package name
        purchase_token: The purchase token from the client app
        defer_duration: Duration to defer, e.g. "604800s" for 7 days
        etag: Current etag of the subscription purchase

    Returns:
        Result with success status and new expiry details
    """
    if blocked := _read_only_block("defer_subscription_purchase"):
        return blocked
    client = get_client_from_context()

    result = client.defer_subscription_purchase(
        package_name=package_name,
        token=purchase_token,
        defer_duration=defer_duration,
        etag=etag,
    )

    return result.model_dump()


@mcp.tool()
def revoke_subscription_purchase(
    package_name: str,
    purchase_token: str,
    refund_type: str = "full",
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Revoke (refund) a subscription purchase.

    Args:
        package_name: App package name
        purchase_token: The purchase token from the client app
        refund_type: "full" or "prorated" (default: full)

    Returns:
        Result with success status and details
    """
    if blocked := _read_only_block("revoke_subscription_purchase"):
        return blocked
    if refund_type not in ("full", "prorated"):
        return {"error": "refund_type must be 'full' or 'prorated'"}

    op = Operation(
        tool="revoke_subscription_purchase",
        target=target_for(package_name),
        params={"purchase_token": purchase_token, "refund_type": refund_type},
        call_args={
            "package_name": package_name,
            "purchase_token": purchase_token,
            "refund_type": refund_type,
        },
    )
    preview = Preview(
        summary=f"Revoke this subscription purchase with a {refund_type} refund.",
        changes=[Change("subscription", "active", "revoked")],
        warnings=[
            "This moves real money and ends the subscriber's access immediately.",
            "It cannot be reversed through the API.",
        ],
        reversal="None — the subscriber must purchase again.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early
    client = get_client_from_context()

    with audit_execution(op):
        result = client.revoke_subscription_purchase(
            package_name=package_name,
            token=purchase_token,
            refund_type=refund_type,
        )

    return result.model_dump()


@mcp.tool()
def get_product_purchase_v2(
    package_name: str,
    purchase_token: str,
) -> dict[str, Any]:
    """Get the status of an in-app product purchase using the v2 API.

    Unlike get_product_purchase, this identifies the purchase by token alone
    (no product ID) and returns line items and acknowledgement state.

    Args:
        package_name: App package name
        purchase_token: The purchase token from the client app

    Returns:
        Product purchase (v2) status
    """
    client = get_client_from_context()

    purchase = client.get_product_purchase_v2(package_name=package_name, token=purchase_token)

    return purchase.model_dump()


# =============================================================================
# In-App Products Tools
# =============================================================================


@mcp.tool()
def list_in_app_products(package_name: str) -> list[dict[str, Any]]:
    """List all in-app products for an app.

    Args:
        package_name: App package name

    Returns:
        List of in-app products with SKU, title, description, and pricing
    """
    client = get_client_from_context()

    products = client.list_in_app_products(package_name)
    return [product.model_dump() for product in products]


@mcp.tool()
def get_in_app_product(
    package_name: str,
    sku: str,
) -> dict[str, Any]:
    """Get details of a specific in-app product.

    Args:
        package_name: App package name
        sku: Product SKU identifier

    Returns:
        In-app product details including title, description, and pricing
    """
    client = get_client_from_context()

    product = client.get_in_app_product(package_name, sku)
    return product.model_dump()


@mcp.tool()
def create_in_app_product(
    package_name: str,
    product: dict[str, Any],
) -> dict[str, Any]:
    """Create a new in-app product in the catalog.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product: In-app product resource body (e.g. sku, purchaseType, defaultPrice,
            listings, status, defaultLanguage)

    Returns:
        The created in-app product
    """
    if blocked := _read_only_block("create_in_app_product"):
        return blocked
    client = get_client_from_context()

    result = client.create_in_app_product(package_name=package_name, product=product)
    return result.model_dump()


@mcp.tool()
def update_in_app_product(
    package_name: str,
    sku: str,
    product: dict[str, Any],
    auto_convert_missing_prices: bool = False,
) -> dict[str, Any]:
    """Update (replace) an existing in-app product.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        sku: Product SKU identifier
        product: In-app product resource body
        auto_convert_missing_prices: Auto-convert prices for regions without a
            specified price based on the default price (default: False)

    Returns:
        The updated in-app product
    """
    if blocked := _read_only_block("update_in_app_product"):
        return blocked
    client = get_client_from_context()

    result = client.update_in_app_product(
        package_name=package_name,
        sku=sku,
        product=product,
        auto_convert_missing_prices=auto_convert_missing_prices,
    )
    return result.model_dump()


@mcp.tool()
def patch_in_app_product(
    package_name: str,
    sku: str,
    product: dict[str, Any],
) -> dict[str, Any]:
    """Partially update an existing in-app product.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        sku: Product SKU identifier
        product: Partial in-app product resource body with fields to change

    Returns:
        The patched in-app product
    """
    if blocked := _read_only_block("patch_in_app_product"):
        return blocked
    client = get_client_from_context()

    result = client.patch_in_app_product(package_name=package_name, sku=sku, product=product)
    return result.model_dump()


@mcp.tool()
def delete_in_app_product(
    package_name: str,
    sku: str,
) -> dict[str, Any]:
    """Delete an in-app product from the catalog.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        sku: Product SKU identifier

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("delete_in_app_product"):
        return blocked
    client = get_client_from_context()

    result = client.delete_in_app_product(package_name=package_name, sku=sku)
    return result.model_dump()


@mcp.tool()
def batch_get_in_app_products(
    package_name: str,
    skus: list[str],
) -> list[dict[str, Any]]:
    """Get details for multiple in-app products at once.

    Args:
        package_name: App package name
        skus: List of product SKUs to retrieve

    Returns:
        List of in-app products, in the same order as requested
    """
    client = get_client_from_context()

    products = client.batch_get_in_app_products(package_name=package_name, skus=skus)
    return [product.model_dump() for product in products]


@mcp.tool()
def batch_delete_in_app_products(
    package_name: str,
    skus: list[str],
) -> dict[str, Any]:
    """Delete multiple in-app products in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        skus: List of product SKUs to delete

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("batch_delete_in_app_products"):
        return blocked
    client = get_client_from_context()

    result = client.batch_delete_in_app_products(package_name=package_name, skus=skus)
    return result.model_dump()


# =============================================================================
# One-Time Product Catalog Tools
# =============================================================================


@mcp.tool()
def get_one_time_product(
    package_name: str,
    product_id: str,
) -> dict[str, Any]:
    """Get details of a specific one-time product.

    Args:
        package_name: App package name
        product_id: One-time product ID

    Returns:
        One-time product details including listings and purchase options
    """
    client = get_client_from_context()

    product = client.get_one_time_product(package_name, product_id)
    return product.model_dump()


@mcp.tool()
def list_one_time_products(
    package_name: str,
) -> list[dict[str, Any]]:
    """List all one-time products for an app.

    Args:
        package_name: App package name

    Returns:
        List of one-time products
    """
    client = get_client_from_context()

    products = client.list_one_time_products(package_name)
    return [product.model_dump() for product in products]


@mcp.tool()
def batch_get_one_time_products(
    package_name: str,
    product_ids: list[str],
) -> list[dict[str, Any]]:
    """Get details for multiple one-time products at once.

    Args:
        package_name: App package name
        product_ids: List of one-time product IDs to retrieve

    Returns:
        List of one-time products
    """
    client = get_client_from_context()

    products = client.batch_get_one_time_products(
        package_name=package_name, product_ids=product_ids
    )
    return [product.model_dump() for product in products]


@mcp.tool()
def patch_one_time_product(
    package_name: str,
    product_id: str,
    product: dict[str, Any],
    update_mask: str,
    regions_version: str = "2022/02",
) -> dict[str, Any]:
    """Create or update a one-time product (patch is create-or-update).

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: One-time product ID
        product: Partial OneTimeProduct resource body with fields to change
        update_mask: Comma-separated list of fields to update
        regions_version: Version of available regions for regional prices (default: "2022/02")

    Returns:
        The patched one-time product
    """
    if blocked := _read_only_block("patch_one_time_product"):
        return blocked
    client = get_client_from_context()

    result = client.patch_one_time_product(
        package_name=package_name,
        product_id=product_id,
        product=product,
        update_mask=update_mask,
        regions_version=regions_version,
    )
    return result.model_dump()


@mcp.tool()
def delete_one_time_product(
    package_name: str,
    product_id: str,
) -> dict[str, Any]:
    """Delete a one-time product from the catalog.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: One-time product ID

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("delete_one_time_product"):
        return blocked
    client = get_client_from_context()

    result = client.delete_one_time_product(package_name=package_name, product_id=product_id)
    return result.model_dump()


@mcp.tool()
def batch_update_one_time_products(
    package_name: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Update multiple one-time products in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        requests: List of UpdateOneTimeProductRequest bodies (each with oneTimeProduct,
            updateMask, and optional regionsVersion / allowMissing)

    Returns:
        List of updated one-time products, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_one_time_products"):
        return blocked
    client = get_client_from_context()

    products = client.batch_update_one_time_products(package_name=package_name, requests=requests)
    return [product.model_dump() for product in products]


@mcp.tool()
def batch_delete_one_time_products(
    package_name: str,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delete multiple one-time products in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        requests: List of DeleteOneTimeProductRequest bodies (each with productId
            and optional packageName / latencyTolerance)

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("batch_delete_one_time_products"):
        return blocked
    client = get_client_from_context()

    result = client.batch_delete_one_time_products(package_name=package_name, requests=requests)
    return result.model_dump()


# =============================================================================
# One-Time Product Purchase Option Tools
# =============================================================================


@mcp.tool()
def batch_delete_purchase_options(
    package_name: str,
    product_id: str,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delete multiple purchase options from a one-time product in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID
        requests: List of DeletePurchaseOptionRequest bodies (each with purchaseOptionId
            and optional latencyTolerance)

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("batch_delete_purchase_options"):
        return blocked
    client = get_client_from_context()

    result = client.batch_delete_purchase_options(
        package_name=package_name, product_id=product_id, requests=requests
    )
    return result.model_dump()


@mcp.tool()
def batch_update_purchase_option_states(
    package_name: str,
    product_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Activate or deactivate multiple purchase options in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID
        requests: List of UpdatePurchaseOptionStateRequest bodies (each with a nested
            activatePurchaseOptionRequest or deactivatePurchaseOptionRequest)

    Returns:
        List of updated one-time products, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_purchase_option_states"):
        return blocked
    client = get_client_from_context()

    products = client.batch_update_purchase_option_states(
        package_name=package_name, product_id=product_id, requests=requests
    )
    return [product.model_dump() for product in products]


# =============================================================================
# One-Time Product Purchase Option Offer Tools
# =============================================================================


@mcp.tool()
def list_purchase_option_offers(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
) -> list[dict[str, Any]]:
    """List all offers for a one-time product purchase option.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID ('-' wildcard lists across products)
        purchase_option_id: Parent purchase option ID ('-' wildcard lists across options)

    Returns:
        List of one-time product offers
    """
    client = get_client_from_context()

    offers = client.list_purchase_option_offers(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool()
def batch_get_purchase_option_offers(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Get details for multiple one-time product offers at once.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID ('-' wildcard allowed)
        purchase_option_id: Parent purchase option ID ('-' wildcard allowed)
        requests: List of GetOneTimeProductOfferRequest bodies

    Returns:
        List of one-time product offers
    """
    client = get_client_from_context()

    offers = client.batch_get_purchase_option_offers(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool()
def activate_purchase_option_offer(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Activate a one-time product offer, making it available to eligible buyers.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID
        purchase_option_id: Parent purchase option ID
        offer_id: One-time product offer ID to activate

    Returns:
        The updated one-time product offer
    """
    if blocked := _read_only_block("activate_purchase_option_offer"):
        return blocked
    client = get_client_from_context()

    result = client.activate_purchase_option_offer(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool()
def deactivate_purchase_option_offer(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Deactivate a one-time product offer, making it unavailable to new buyers.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID
        purchase_option_id: Parent purchase option ID
        offer_id: One-time product offer ID to deactivate

    Returns:
        The updated one-time product offer
    """
    if blocked := _read_only_block("deactivate_purchase_option_offer"):
        return blocked
    client = get_client_from_context()

    result = client.deactivate_purchase_option_offer(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool()
def cancel_purchase_option_offer(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Cancel a one-time product offer (e.g. a pre-order offer).

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID
        purchase_option_id: Parent purchase option ID
        offer_id: One-time product offer ID to cancel

    Returns:
        The updated one-time product offer
    """
    if blocked := _read_only_block("cancel_purchase_option_offer"):
        return blocked
    client = get_client_from_context()

    result = client.cancel_purchase_option_offer(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool()
def batch_update_purchase_option_offers(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Create or update multiple one-time product offers in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID ('-' wildcard allowed)
        purchase_option_id: Parent purchase option ID ('-' wildcard allowed)
        requests: List of UpdateOneTimeProductOfferRequest bodies (each with
            oneTimeProductOffer, updateMask, and optional allowMissing / latencyTolerance)

    Returns:
        List of updated one-time product offers, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_purchase_option_offers"):
        return blocked
    client = get_client_from_context()

    offers = client.batch_update_purchase_option_offers(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool()
def batch_update_purchase_option_offer_states(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Activate, deactivate or cancel multiple one-time product offers in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID ('-' wildcard allowed)
        purchase_option_id: Parent purchase option ID ('-' wildcard allowed)
        requests: List of UpdateOneTimeProductOfferStateRequest bodies (each with a nested
            activate/deactivate/cancel one-time product offer request)

    Returns:
        List of updated one-time product offers, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_purchase_option_offer_states"):
        return blocked
    client = get_client_from_context()

    offers = client.batch_update_purchase_option_offer_states(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool()
def batch_delete_purchase_option_offers(
    package_name: str,
    product_id: str,
    purchase_option_id: str,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Delete multiple one-time product offers in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent one-time product ID ('-' wildcard allowed)
        purchase_option_id: Parent purchase option ID ('-' wildcard allowed)
        requests: List of DeleteOneTimeProductOfferRequest bodies (each with offerId
            and optional latencyTolerance)

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("batch_delete_purchase_option_offers"):
        return blocked
    client = get_client_from_context()

    result = client.batch_delete_purchase_option_offers(
        package_name=package_name,
        product_id=product_id,
        purchase_option_id=purchase_option_id,
        requests=requests,
    )
    return result.model_dump()


# =============================================================================
# Subscription Catalog Tools
# =============================================================================


@mcp.tool()
def get_subscription(
    package_name: str,
    product_id: str,
) -> dict[str, Any]:
    """Get details of a specific subscription product.

    Args:
        package_name: App package name
        product_id: Subscription product ID

    Returns:
        Subscription product details including base plans
    """
    client = get_client_from_context()

    subscription = client.get_subscription(package_name, product_id)
    return subscription.model_dump()


@mcp.tool()
def create_subscription(
    package_name: str,
    product_id: str,
    subscription: dict[str, Any],
    regions_version: str = "2022/02",
) -> dict[str, Any]:
    """Create a new subscription product in the catalog.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Subscription product ID
        subscription: Subscription resource body (e.g. basePlans, listings)
        regions_version: Version of available regions for regional prices (default: "2022/02")

    Returns:
        The created subscription product
    """
    if blocked := _read_only_block("create_subscription"):
        return blocked
    client = get_client_from_context()

    result = client.create_subscription(
        package_name=package_name,
        product_id=product_id,
        subscription=subscription,
        regions_version=regions_version,
    )
    return result.model_dump()


@mcp.tool()
def patch_subscription(
    package_name: str,
    product_id: str,
    subscription: dict[str, Any],
    update_mask: str,
    regions_version: str = "2022/02",
) -> dict[str, Any]:
    """Partially update an existing subscription product.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Subscription product ID
        subscription: Partial Subscription resource body with fields to change
        update_mask: Comma-separated list of fields to update
        regions_version: Version of available regions for regional prices (default: "2022/02")

    Returns:
        The patched subscription product
    """
    if blocked := _read_only_block("patch_subscription"):
        return blocked
    client = get_client_from_context()

    result = client.patch_subscription(
        package_name=package_name,
        product_id=product_id,
        subscription=subscription,
        update_mask=update_mask,
        regions_version=regions_version,
    )
    return result.model_dump()


@mcp.tool()
def delete_subscription(
    package_name: str,
    product_id: str,
) -> dict[str, Any]:
    """Delete a subscription product from the catalog.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Subscription product ID

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("delete_subscription"):
        return blocked
    client = get_client_from_context()

    result = client.delete_subscription(package_name=package_name, product_id=product_id)
    return result.model_dump()


@mcp.tool()
def batch_get_subscriptions(
    package_name: str,
    product_ids: list[str],
) -> list[dict[str, Any]]:
    """Get details for multiple subscription products at once.

    Args:
        package_name: App package name
        product_ids: List of subscription product IDs to retrieve

    Returns:
        List of subscription products
    """
    client = get_client_from_context()

    subscriptions = client.batch_get_subscriptions(
        package_name=package_name, product_ids=product_ids
    )
    return [sub.model_dump() for sub in subscriptions]


@mcp.tool()
def batch_update_subscriptions(
    package_name: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Update multiple subscription products in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        requests: List of UpdateSubscriptionRequest bodies (each with subscription,
            updateMask, and optional regionsVersion)

    Returns:
        List of updated subscription products, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_subscriptions"):
        return blocked
    client = get_client_from_context()

    subscriptions = client.batch_update_subscriptions(package_name=package_name, requests=requests)
    return [sub.model_dump() for sub in subscriptions]


# =============================================================================
# Subscription Base Plan Tools
# =============================================================================


@mcp.tool()
def activate_base_plan(
    package_name: str,
    product_id: str,
    base_plan_id: str,
) -> dict[str, Any]:
    """Activate a subscription base plan, making it available to new subscribers.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Base plan ID to activate

    Returns:
        The updated subscription product
    """
    if blocked := _read_only_block("activate_base_plan"):
        return blocked
    client = get_client_from_context()

    result = client.activate_base_plan(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
    )
    return result.model_dump()


@mcp.tool()
def deactivate_base_plan(
    package_name: str,
    product_id: str,
    base_plan_id: str,
) -> dict[str, Any]:
    """Deactivate a subscription base plan, making it unavailable to new subscribers.

    Existing subscribers keep their subscription. Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Base plan ID to deactivate

    Returns:
        The updated subscription product
    """
    if blocked := _read_only_block("deactivate_base_plan"):
        return blocked
    client = get_client_from_context()

    result = client.deactivate_base_plan(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
    )
    return result.model_dump()


@mcp.tool()
def delete_base_plan(
    package_name: str,
    product_id: str,
    base_plan_id: str,
) -> dict[str, Any]:
    """Delete a subscription base plan.

    Only inactive base plans with no active subscribers can be deleted.
    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Base plan ID to delete

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("delete_base_plan"):
        return blocked
    client = get_client_from_context()

    result = client.delete_base_plan(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
    )
    return result.model_dump()


@mcp.tool()
def migrate_base_plan_prices(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Migrate subscribers to the current base plan prices.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Base plan ID whose prices to migrate
        request: MigrateBasePlanPricesRequest body (e.g. regionalPriceMigrations,
            regionsVersion)

    Returns:
        The MigrateBasePlanPricesResponse (raw dict), or an error object in read-only mode
    """
    if blocked := _read_only_block("migrate_base_plan_prices"):
        return blocked
    client = get_client_from_context()

    return client.migrate_base_plan_prices(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        request=request,
    )


@mcp.tool()
def batch_migrate_base_plan_prices(
    package_name: str,
    product_id: str,
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Migrate prices for multiple base plans in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        requests: List of MigrateBasePlanPricesRequest bodies

    Returns:
        The BatchMigrateBasePlanPricesResponse (raw dict), or an error object in
        read-only mode
    """
    if blocked := _read_only_block("batch_migrate_base_plan_prices"):
        return blocked
    client = get_client_from_context()

    return client.batch_migrate_base_plan_prices(
        package_name=package_name,
        product_id=product_id,
        requests=requests,
    )


@mcp.tool()
def batch_update_base_plan_states(
    package_name: str,
    product_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Activate or deactivate multiple base plans in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        requests: List of UpdateBasePlanStateRequest bodies (each with a nested
            activateBasePlanRequest or deactivateBasePlanRequest)

    Returns:
        The updated subscriptions, one per request
    """
    if blocked := _read_only_block("batch_update_base_plan_states"):
        return blocked
    client = get_client_from_context()

    results = client.batch_update_base_plan_states(
        package_name=package_name,
        product_id=product_id,
        requests=requests,
    )
    return [result.model_dump() for result in results]


# =============================================================================
# Subscription Offer Tools
# =============================================================================


@mcp.tool()
def get_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Get details of a specific subscription offer.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID

    Returns:
        The subscription offer details
    """
    client = get_client_from_context()

    offer = client.get_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
    )
    return offer.model_dump()


@mcp.tool()
def list_subscription_offers(
    package_name: str,
    product_id: str,
    base_plan_id: str,
) -> list[dict[str, Any]]:
    """List all offers for a subscription base plan.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID ('-' wildcard lists offers across base plans)

    Returns:
        List of subscription offers
    """
    client = get_client_from_context()

    offers = client.list_subscription_offers(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool()
def create_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
    offer: dict[str, Any],
    regions_version: str = "2022/02",
) -> dict[str, Any]:
    """Create a new subscription offer.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID
        offer: SubscriptionOffer resource body (e.g. phases, regionalConfigs, targeting)
        regions_version: Version of available regions for regional prices (default: "2022/02")

    Returns:
        The created subscription offer
    """
    if blocked := _read_only_block("create_subscription_offer"):
        return blocked
    client = get_client_from_context()

    result = client.create_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
        offer=offer,
        regions_version=regions_version,
    )
    return result.model_dump()


@mcp.tool()
def patch_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
    offer: dict[str, Any],
    update_mask: str,
    regions_version: str = "2022/02",
) -> dict[str, Any]:
    """Partially update an existing subscription offer.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID
        offer: Partial SubscriptionOffer resource body with fields to change
        update_mask: Comma-separated list of fields to update
        regions_version: Version of available regions for regional prices (default: "2022/02")

    Returns:
        The patched subscription offer
    """
    if blocked := _read_only_block("patch_subscription_offer"):
        return blocked
    client = get_client_from_context()

    result = client.patch_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
        offer=offer,
        update_mask=update_mask,
        regions_version=regions_version,
    )
    return result.model_dump()


@mcp.tool()
def activate_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Activate a subscription offer, making it available to eligible subscribers.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID to activate

    Returns:
        The updated subscription offer
    """
    if blocked := _read_only_block("activate_subscription_offer"):
        return blocked
    client = get_client_from_context()

    result = client.activate_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool()
def deactivate_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Deactivate a subscription offer, making it unavailable to new subscribers.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID to deactivate

    Returns:
        The updated subscription offer
    """
    if blocked := _read_only_block("deactivate_subscription_offer"):
        return blocked
    client = get_client_from_context()

    result = client.deactivate_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool()
def delete_subscription_offer(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    offer_id: str,
) -> dict[str, Any]:
    """Delete a subscription offer.

    Only inactive offers with no active subscribers can be deleted.
    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID
        offer_id: Subscription offer ID to delete

    Returns:
        Result with success status
    """
    if blocked := _read_only_block("delete_subscription_offer"):
        return blocked
    client = get_client_from_context()

    result = client.delete_subscription_offer(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        offer_id=offer_id,
    )
    return result.model_dump()


@mcp.tool()
def batch_get_subscription_offers(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Get details for multiple subscription offers in a single operation.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID ('-' wildcard allowed)
        requests: List of GetSubscriptionOfferRequest bodies

    Returns:
        List of subscription offers
    """
    client = get_client_from_context()

    offers = client.batch_get_subscription_offers(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool()
def batch_update_subscription_offers(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Update multiple subscription offers in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID ('-' wildcard allowed)
        requests: List of UpdateSubscriptionOfferRequest bodies (each with
            subscriptionOffer, updateMask, and optional regionsVersion)

    Returns:
        List of updated subscription offers, or an error object in read-only mode
    """
    if blocked := _read_only_block("batch_update_subscription_offers"):
        return blocked
    client = get_client_from_context()

    offers = client.batch_update_subscription_offers(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


@mcp.tool()
def batch_update_subscription_offer_states(
    package_name: str,
    product_id: str,
    base_plan_id: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]] | dict[str, Any]:
    """Activate or deactivate multiple subscription offers in a single operation.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        product_id: Parent subscription product ID
        base_plan_id: Parent base plan ID ('-' wildcard allowed)
        requests: List of UpdateSubscriptionOfferStateRequest bodies (each with a
            nested activateSubscriptionOfferRequest or deactivateSubscriptionOfferRequest)

    Returns:
        The updated subscription offers, one per request
    """
    if blocked := _read_only_block("batch_update_subscription_offer_states"):
        return blocked
    client = get_client_from_context()

    offers = client.batch_update_subscription_offer_states(
        package_name=package_name,
        product_id=product_id,
        base_plan_id=base_plan_id,
        requests=requests,
    )
    return [offer.model_dump() for offer in offers]


# =============================================================================
# Store Listings Tools
# =============================================================================


@mcp.tool()
def get_listing(
    package_name: str,
    language: str = "en-US",
) -> dict[str, Any]:
    """Get store listing for a specific language.

    Args:
        package_name: App package name
        language: Language code (e.g., en-US, es-ES, fr-FR)

    Returns:
        Store listing with title, descriptions, and video
    """
    client = get_client_from_context()

    listing = client.get_listing(package_name, language)
    return listing.model_dump()


@mcp.tool()
def update_listing(
    package_name: str,
    language: str,
    title: str | None = None,
    full_description: str | None = None,
    short_description: str | None = None,
    video: str | None = None,
) -> dict[str, Any]:
    """Update store listing for a specific language.

    Args:
        package_name: App package name
        language: Language code (e.g., en-US, es-ES, fr-FR)
        title: App title (max 50 characters, optional)
        full_description: Full description (max 4000 characters, optional)
        short_description: Short description (max 80 characters, optional)
        video: YouTube video URL (optional)

    Returns:
        Update result with success status
    """
    if blocked := _read_only_block("update_listing"):
        return blocked
    client = get_client_from_context()

    result = client.update_listing(
        package_name=package_name,
        language=language,
        title=title,
        full_description=full_description,
        short_description=short_description,
        video=video,
    )
    return result.model_dump()


@mcp.tool()
def list_all_listings(package_name: str) -> list[dict[str, Any]]:
    """List all store listings for all languages.

    Args:
        package_name: App package name

    Returns:
        List of store listings for all configured languages
    """
    client = get_client_from_context()

    listings = client.list_all_listings(package_name)
    return [listing.model_dump() for listing in listings]


# =============================================================================
# Testers Management Tools
# =============================================================================


@mcp.tool()
def get_testers(
    package_name: str,
    track: str,
) -> dict[str, Any]:
    """Get testers for a specific testing track.

    Args:
        package_name: App package name
        track: Track name (internal, alpha, beta)

    Returns:
        Tester information with list of email addresses
    """
    client = get_client_from_context()

    testers = client.get_testers(package_name, track)
    return testers.model_dump()


@mcp.tool()
def update_testers(
    package_name: str,
    track: str,
    google_groups: list[str],
) -> dict[str, Any]:
    """Update testers for a specific testing track.

    Args:
        package_name: App package name
        track: Track name (internal, alpha, beta)
        google_groups: List of Google Group email addresses

    Returns:
        Update result with success status
    """
    if blocked := _read_only_block("update_testers"):
        return blocked
    client = get_client_from_context()

    result = client.update_testers(package_name, track, google_groups)
    return result


# =============================================================================
# Orders Tools
# =============================================================================


@mcp.tool()
def get_order(
    package_name: str,
    order_id: str,
) -> dict[str, Any]:
    """Get detailed order/transaction information.

    Args:
        package_name: App package name
        order_id: Order ID to retrieve

    Returns:
        Order details including product, purchase state, and token
    """
    client = get_client_from_context()

    order = client.get_order(package_name, order_id)
    return order.model_dump()


@mcp.tool()
def batch_get_orders(
    package_name: str,
    order_ids: list[str],
) -> list[dict[str, Any]]:
    """Get detailed information for multiple orders at once.

    Args:
        package_name: App package name
        order_ids: List of order IDs to retrieve (1-1000)

    Returns:
        List of order details
    """
    client = get_client_from_context()

    orders = client.batch_get_orders(package_name=package_name, order_ids=order_ids)
    return [order.model_dump() for order in orders]


# =============================================================================
# External Transactions Tools
# =============================================================================


@mcp.tool()
def get_external_transaction(
    package_name: str,
    external_transaction_id: str,
) -> dict[str, Any]:
    """Get an external (alternative billing) transaction.

    Args:
        package_name: App package name
        external_transaction_id: External transaction ID

    Returns:
        External transaction details including state, amounts, and create time
    """
    client = get_client_from_context()

    transaction = client.get_external_transaction(
        package_name=package_name,
        external_transaction_id=external_transaction_id,
    )
    return transaction.model_dump()


@mcp.tool()
def create_external_transaction(
    package_name: str,
    external_transaction_id: str,
    transaction: dict[str, Any],
) -> dict[str, Any]:
    """Create an external (alternative billing) transaction.

    Args:
        package_name: App package name
        external_transaction_id: External transaction ID to assign
        transaction: ExternalTransaction resource body

    Returns:
        The created external transaction
    """
    if blocked := _read_only_block("create_external_transaction"):
        return blocked
    client = get_client_from_context()

    result = client.create_external_transaction(
        package_name=package_name,
        external_transaction_id=external_transaction_id,
        transaction=transaction,
    )
    return result.model_dump()


@mcp.tool()
def refund_external_transaction(
    package_name: str,
    external_transaction_id: str,
    refund: dict[str, Any],
) -> dict[str, Any]:
    """Refund an external (alternative billing) transaction.

    Args:
        package_name: App package name
        external_transaction_id: External transaction ID to refund
        refund: RefundExternalTransactionRequest body (e.g. refundTime plus
            fullRefund or partialRefund)

    Returns:
        The refunded external transaction
    """
    if blocked := _read_only_block("refund_external_transaction"):
        return blocked
    client = get_client_from_context()

    result = client.refund_external_transaction(
        package_name=package_name,
        external_transaction_id=external_transaction_id,
        refund=refund,
    )
    return result.model_dump()


# =============================================================================
# Device Tier Config Tools
# =============================================================================


@mcp.tool()
def get_device_tier_config(
    package_name: str,
    device_tier_config_id: str,
) -> dict[str, Any]:
    """Get a device tier config.

    Args:
        package_name: App package name
        device_tier_config_id: Device tier config ID

    Returns:
        Device tier config details including device groups, tier set, and country sets
    """
    client = get_client_from_context()

    config = client.get_device_tier_config(
        package_name=package_name,
        device_tier_config_id=device_tier_config_id,
    )
    return config.model_dump()


@mcp.tool()
def list_device_tier_configs(package_name: str) -> list[dict[str, Any]]:
    """List all device tier configs for an app.

    Args:
        package_name: App package name

    Returns:
        List of device tier configs with device groups, tier set, and country sets
    """
    client = get_client_from_context()

    configs = client.list_device_tier_configs(package_name)
    return [config.model_dump() for config in configs]


@mcp.tool()
def create_device_tier_config(
    package_name: str,
    config: dict[str, Any],
    allow_unknown_devices: bool = False,
) -> dict[str, Any]:
    """Create a new device tier config.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        config: DeviceTierConfig resource body (deviceGroups, deviceTierSet,
            userCountrySets)
        allow_unknown_devices: Accept device IDs unknown to Play's catalog rather
            than rejecting them (default: False)

    Returns:
        The created device tier config
    """
    if blocked := _read_only_block("create_device_tier_config"):
        return blocked
    client = get_client_from_context()

    result = client.create_device_tier_config(
        package_name=package_name,
        config=config,
        allow_unknown_devices=allow_unknown_devices,
    )
    return result.model_dump()


# =============================================================================
# Account Access Tools (Users & Grants)
# =============================================================================


@mcp.tool()
def list_users(developer_id: str) -> list[dict[str, Any]]:
    """List users with access to a developer account.

    Args:
        developer_id: Developer account ID

    Returns:
        List of users with their access state and account-level permissions
    """
    client = get_client_from_context()

    users = client.list_users(developer_id)
    return [user.model_dump() for user in users]


@mcp.tool()
def create_user(developer_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """Grant a user access to a developer account.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        user: User resource body (email, developerAccountPermissions,
            expirationTime, grants)

    Returns:
        The created user
    """
    if blocked := _read_only_block("create_user"):
        return blocked
    client = get_client_from_context()

    result = client.create_user(developer_id=developer_id, user=user)
    return result.model_dump()


@mcp.tool()
def update_user(
    developer_id: str,
    email: str,
    user: dict[str, Any],
    update_mask: str,
) -> dict[str, Any]:
    """Update a user's account access.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        email: Email of the user to update
        user: User resource body with the fields to update
        update_mask: Comma-separated list of fields to update (e.g.
            "developerAccountPermissions,expirationTime")

    Returns:
        The updated user
    """
    if blocked := _read_only_block("update_user"):
        return blocked
    client = get_client_from_context()

    result = client.update_user(
        developer_id=developer_id,
        email=email,
        user=user,
        update_mask=update_mask,
    )
    return result.model_dump()


@mcp.tool()
def delete_user(
    developer_id: str,
    email: str,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Remove a user's access to a developer account.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        email: Email of the user to remove

    Returns:
        Access result with success status
    """
    if blocked := _read_only_block("delete_user"):
        return blocked

    op = Operation(
        tool="delete_user",
        target=f"google_play_account:{developer_id}",
        params={"developer_id": developer_id, "email": email},
        call_args={"developer_id": developer_id, "email": email},
    )
    preview = Preview(
        summary=f"Remove {email} from developer account {developer_id}.",
        changes=[Change("access", "granted", "removed")],
        warnings=["The person loses all Play Console access to this developer account."],
        reversal="Re-invite them with create_user; permissions must be set again.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early
    client = get_client_from_context()

    with audit_execution(op):
        result = client.delete_user(developer_id=developer_id, email=email)
    return result.model_dump()


@mcp.tool()
def create_grant(developer_id: str, email: str, grant: dict[str, Any]) -> dict[str, Any]:
    """Grant a user app-level access.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        email: Email of the user to grant access to
        grant: Grant resource body (packageName, appLevelPermissions)

    Returns:
        The created grant
    """
    if blocked := _read_only_block("create_grant"):
        return blocked
    client = get_client_from_context()

    result = client.create_grant(developer_id=developer_id, email=email, grant=grant)
    return result.model_dump()


@mcp.tool()
def update_grant(
    developer_id: str,
    email: str,
    package_name: str,
    grant: dict[str, Any],
    update_mask: str,
) -> dict[str, Any]:
    """Update a user's app-level access.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        email: Email of the user the grant belongs to
        package_name: App package name the grant applies to
        grant: Grant resource body with the fields to update
        update_mask: Comma-separated list of fields to update (e.g.
            "appLevelPermissions")

    Returns:
        The updated grant
    """
    if blocked := _read_only_block("update_grant"):
        return blocked
    client = get_client_from_context()

    result = client.update_grant(
        developer_id=developer_id,
        email=email,
        package_name=package_name,
        grant=grant,
        update_mask=update_mask,
    )
    return result.model_dump()


@mcp.tool()
def delete_grant(developer_id: str, email: str, package_name: str) -> dict[str, Any]:
    """Remove a user's app-level access.

    Disabled in read-only mode.

    Args:
        developer_id: Developer account ID
        email: Email of the user the grant belongs to
        package_name: App package name the grant applies to

    Returns:
        Access result with success status
    """
    if blocked := _read_only_block("delete_grant"):
        return blocked
    client = get_client_from_context()

    result = client.delete_grant(
        developer_id=developer_id,
        email=email,
        package_name=package_name,
    )
    return result.model_dump()


# =============================================================================
# Data Safety Tools
# =============================================================================


@mcp.tool()
def set_data_safety(
    package_name: str,
    safety_labels: dict[str, Any],
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Write the data safety labels declaration of an app.

    Disabled in read-only mode. Two-step: preview, then confirm.

    Args:
        package_name: App package name
        safety_labels: SafetyLabelsUpdateRequest resource body. Contains a
            `safetyLabels` string with the contents of the Data Safety CSV
        confirm: Set True only after the user approved the preview
        confirmation_token: The token from that preview; never invent one

    Returns:
        The preview block when unconfirmed, otherwise the result of the update
    """
    if blocked := _read_only_block("set_data_safety"):
        return blocked

    op = Operation(
        tool="set_data_safety",
        target=target_for(package_name),
        params={"safety_labels": safety_labels},
        call_args={"package_name": package_name, "safety_labels": safety_labels},
    )
    preview = Preview(
        summary="Replace this app's Data Safety declaration.",
        warnings=[
            "The Data Safety form is a declaration to users and to Google about what the app "
            "collects and shares. An inaccurate one is a policy violation, and Play can remove "
            "the app over it.",
            "This replaces the whole declaration, not just the fields you changed.",
        ],
        reversal="Submit the previous declaration again.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()

    with audit_execution(op):
        result = client.set_data_safety(
            package_name=package_name,
            safety_labels=safety_labels,
        )
    return result.model_dump()


# =============================================================================
# App Recovery Tools
# =============================================================================


@mcp.tool()
def list_app_recoveries(package_name: str, version_code: int) -> list[dict[str, Any]]:
    """List all app recovery actions for an app version.

    Args:
        package_name: App package name
        version_code: App version code the recovery actions target

    Returns:
        List of app recovery actions with ID, status, targeting, and create time
    """
    client = get_client_from_context()

    recoveries = client.list_app_recoveries(package_name, version_code)
    return [recovery.model_dump() for recovery in recoveries]


@mcp.tool()
def create_app_recovery(
    package_name: str,
    recovery: dict[str, Any],
) -> dict[str, Any]:
    """Create a draft app recovery action.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        recovery: CreateDraftAppRecoveryRequest resource body (e.g.
            `remoteInAppUpdate` plus `targeting`)

    Returns:
        The created app recovery action
    """
    if blocked := _read_only_block("create_app_recovery"):
        return blocked
    client = get_client_from_context()

    result = client.create_app_recovery(
        package_name=package_name,
        recovery=recovery,
    )
    return result.model_dump()


@mcp.tool()
def deploy_app_recovery(
    package_name: str,
    app_recovery_id: str,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Deploy an app recovery action to users.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        app_recovery_id: App recovery action ID

    Returns:
        The result of the deploy action
    """
    if blocked := _read_only_block("deploy_app_recovery"):
        return blocked

    op = Operation(
        tool="deploy_app_recovery",
        target=target_for(package_name),
        params={"app_recovery_id": app_recovery_id},
        call_args={"package_name": package_name, "app_recovery_id": app_recovery_id},
    )
    preview = Preview(
        summary=f"Deploy app recovery action {app_recovery_id} to users.",
        changes=[],
        warnings=["A recovery action is pushed to installed devices matching its targeting."],
        reversal="cancel_app_recovery stops further deployment.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early
    client = get_client_from_context()

    with audit_execution(op):
        result = client.deploy_app_recovery(
            package_name=package_name,
            app_recovery_id=app_recovery_id,
        )
    return result.model_dump()


@mcp.tool()
def cancel_app_recovery(
    package_name: str,
    app_recovery_id: str,
) -> dict[str, Any]:
    """Cancel an app recovery action.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        app_recovery_id: App recovery action ID

    Returns:
        The result of the cancel action
    """
    if blocked := _read_only_block("cancel_app_recovery"):
        return blocked
    client = get_client_from_context()

    result = client.cancel_app_recovery(
        package_name=package_name,
        app_recovery_id=app_recovery_id,
    )
    return result.model_dump()


@mcp.tool()
def add_app_recovery_targeting(
    package_name: str,
    app_recovery_id: str,
    targeting: dict[str, Any],
) -> dict[str, Any]:
    """Add targeting to an app recovery action.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        app_recovery_id: App recovery action ID
        targeting: AddTargetingRequest resource body (e.g. a `targetingUpdate`
            object)

    Returns:
        The result of the add-targeting action
    """
    if blocked := _read_only_block("add_app_recovery_targeting"):
        return blocked
    client = get_client_from_context()

    result = client.add_app_recovery_targeting(
        package_name=package_name,
        app_recovery_id=app_recovery_id,
        targeting=targeting,
    )
    return result.model_dump()


# =============================================================================
# Generated APKs Tools
# =============================================================================


@mcp.tool()
def list_generated_apks(
    package_name: str,
    version_code: int,
) -> list[dict[str, Any]]:
    """List the APKs Google Play generated from an app bundle version.

    Returns one entry per downloadable generated APK (split, standalone,
    universal, asset pack slice, or recovery), each with a download ID that can
    be passed to `download_generated_apk`.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle

    Returns:
        List of downloadable generated APKs with their download IDs and types
    """
    client = get_client_from_context()

    downloads = client.list_generated_apks(
        package_name=package_name,
        version_code=version_code,
    )
    return [download.model_dump() for download in downloads]


@mcp.tool()
def download_generated_apk(
    package_name: str,
    version_code: int,
    download_id: str,
    destination_path: str,
) -> dict[str, Any]:
    """Download a single generated APK to a local file.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle
        download_id: Download ID of the generated APK (from `list_generated_apks`)
        destination_path: Local path to write the APK bytes to

    Returns:
        Download result with success status and destination path
    """
    client = get_client_from_context()

    result = client.download_generated_apk(
        package_name=package_name,
        version_code=version_code,
        download_id=download_id,
        destination_path=destination_path,
    )
    return result.model_dump()


# =============================================================================
# System APK Variants Tools
# =============================================================================


@mcp.tool()
def get_system_apk_variant(
    package_name: str,
    version_code: int,
    variant_id: int,
) -> dict[str, Any]:
    """Get a previously created system APK variant.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle
        variant_id: ID of the system APK variant

    Returns:
        System APK variant details including device spec and options
    """
    client = get_client_from_context()

    variant = client.get_system_apk_variant(
        package_name=package_name,
        version_code=version_code,
        variant_id=variant_id,
    )
    return variant.model_dump()


@mcp.tool()
def list_system_apk_variants(
    package_name: str,
    version_code: int,
) -> list[dict[str, Any]]:
    """List previously created system APK variants for an app bundle version.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle

    Returns:
        List of system APK variants with their IDs, device specs, and options
    """
    client = get_client_from_context()

    variants = client.list_system_apk_variants(
        package_name=package_name,
        version_code=version_code,
    )
    return [variant.model_dump() for variant in variants]


@mcp.tool()
def create_system_apk_variant(
    package_name: str,
    version_code: int,
    variant: dict[str, Any],
) -> dict[str, Any]:
    """Create a system APK variant from an uploaded app bundle.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle
        variant: Variant resource body (e.g. `deviceSpec` and `options`)

    Returns:
        The created system APK variant
    """
    if blocked := _read_only_block("create_system_apk_variant"):
        return blocked
    client = get_client_from_context()

    result = client.create_system_apk_variant(
        package_name=package_name,
        version_code=version_code,
        variant=variant,
    )
    return result.model_dump()


@mcp.tool()
def download_system_apk_variant(
    package_name: str,
    version_code: int,
    variant_id: int,
    destination_path: str,
) -> dict[str, Any]:
    """Download a previously created system APK variant to a local file.

    Args:
        package_name: App package name
        version_code: Version code of the app bundle
        variant_id: ID of the system APK variant (from `list_system_apk_variants`)
        destination_path: Local path to write the APK bytes to

    Returns:
        Download result with success status and destination path
    """
    client = get_client_from_context()

    result = client.download_system_apk_variant(
        package_name=package_name,
        version_code=version_code,
        variant_id=variant_id,
        destination_path=destination_path,
    )
    return result.model_dump()


# =============================================================================
# Expansion Files Tools
# =============================================================================


@mcp.tool()
def get_expansion_file(
    package_name: str,
    version_code: int,
    expansion_file_type: str = "main",
) -> dict[str, Any]:
    """Get APK expansion file information.

    Expansion files are used for large apps (especially games) that exceed
    the 100MB APK size limit.

    Args:
        package_name: App package name
        version_code: APK version code
        expansion_file_type: Type of expansion file (main or patch)

    Returns:
        Expansion file information including size and references
    """
    client = get_client_from_context()

    expansion_file = client.get_expansion_file(package_name, version_code, expansion_file_type)
    return expansion_file.model_dump()


# =============================================================================
# Edit Upload Tools (APKs, bundles, deobfuscation & expansion files)
# =============================================================================


@mcp.tool()
def list_apks(package_name: str) -> list[dict[str, Any]]:
    """List the APKs currently uploaded for an app.

    Args:
        package_name: App package name

    Returns:
        List of APKs, each with its version code and binary sha1/sha256 hashes
    """
    client = get_client_from_context()

    apks = client.list_apks(package_name)
    return [apk.model_dump() for apk in apks]


@mcp.tool()
def list_bundles(package_name: str) -> list[dict[str, Any]]:
    """List the Android App Bundles currently uploaded for an app.

    Args:
        package_name: App package name

    Returns:
        List of app bundles, each with its version code and sha1/sha256 hashes
    """
    client = get_client_from_context()

    bundles = client.list_bundles(package_name)
    return [bundle.model_dump() for bundle in bundles]


@mcp.tool()
def upload_apk(package_name: str, apk_path: str) -> dict[str, Any]:
    """Upload an APK to a new edit and commit it.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        apk_path: Local path to the APK file

    Returns:
        The uploaded APK with its version code and binary sha1/sha256 hashes
    """
    if blocked := _read_only_block("upload_apk"):
        return blocked
    client = get_client_from_context()

    apk = client.upload_apk(package_name=package_name, apk_path=apk_path)
    return apk.model_dump()


@mcp.tool()
def upload_bundle(package_name: str, bundle_path: str) -> dict[str, Any]:
    """Upload an Android App Bundle (.aab) to a new edit and commit it.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        bundle_path: Local path to the app bundle (.aab) file

    Returns:
        The uploaded app bundle with its version code and sha1/sha256 hashes
    """
    if blocked := _read_only_block("upload_bundle"):
        return blocked
    client = get_client_from_context()

    bundle = client.upload_bundle(package_name=package_name, bundle_path=bundle_path)
    return bundle.model_dump()


@mcp.tool()
def upload_deobfuscation_file(
    package_name: str,
    version_code: int,
    file_path: str,
    deobfuscation_file_type: str = "proguard",
) -> dict[str, Any]:
    """Upload a deobfuscation (ProGuard mapping or native symbols) file.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        version_code: APK version code the file applies to
        file_path: Local path to the deobfuscation file
        deobfuscation_file_type: Type of file - one of: proguard, nativeCode

    Returns:
        The uploaded deobfuscation file configuration with its symbol type
    """
    if blocked := _read_only_block("upload_deobfuscation_file"):
        return blocked
    client = get_client_from_context()

    deobfuscation_file = client.upload_deobfuscation_file(
        package_name=package_name,
        version_code=version_code,
        file_path=file_path,
        deobfuscation_file_type=deobfuscation_file_type,
    )
    return deobfuscation_file.model_dump()


@mcp.tool()
def upload_expansion_file(
    package_name: str,
    version_code: int,
    file_path: str,
    expansion_file_type: str = "main",
) -> dict[str, Any]:
    """Upload an APK expansion file (OBB) to a new edit and commit it.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        version_code: APK version code the file applies to
        file_path: Local path to the expansion file
        expansion_file_type: Type of expansion file - one of: main, patch

    Returns:
        The uploaded expansion file information including size and references
    """
    if blocked := _read_only_block("upload_expansion_file"):
        return blocked
    client = get_client_from_context()

    expansion_file = client.upload_expansion_file(
        package_name=package_name,
        version_code=version_code,
        file_path=file_path,
        expansion_file_type=expansion_file_type,
    )
    return expansion_file.model_dump()


# =============================================================================
# Store Listing Image Tools
# =============================================================================


@mcp.tool()
def list_images(package_name: str, language: str, image_type: str) -> list[dict[str, Any]]:
    """List the store-listing images for a language and image type.

    Args:
        package_name: App package name
        language: Language localization code (BCP-47 tag, e.g. en-US)
        image_type: Image type - one of: phoneScreenshots, sevenInchScreenshots,
            tenInchScreenshots, tvScreenshots, wearScreenshots, icon, featureGraphic,
            tvBanner

    Returns:
        List of images, each with its ID, serving URL and sha1/sha256 hashes
    """
    client = get_client_from_context()

    images = client.list_images(package_name, language, image_type)
    return [image.model_dump() for image in images]


@mcp.tool()
def upload_image(
    package_name: str, language: str, image_type: str, image_path: str
) -> dict[str, Any]:
    """Upload a store-listing image (PNG or JPEG) to a new edit and commit it.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        language: Language localization code (BCP-47 tag, e.g. en-US)
        image_type: Image type - one of: phoneScreenshots, sevenInchScreenshots,
            tenInchScreenshots, tvScreenshots, wearScreenshots, icon, featureGraphic,
            tvBanner
        image_path: Local path to the image file (PNG or JPEG)

    Returns:
        The uploaded image with its ID, serving URL and sha1/sha256 hashes
    """
    if blocked := _read_only_block("upload_image"):
        return blocked
    client = get_client_from_context()

    image = client.upload_image(
        package_name=package_name,
        language=language,
        image_type=image_type,
        image_path=image_path,
    )
    return image.model_dump()


@mcp.tool()
def delete_image(
    package_name: str, language: str, image_type: str, image_id: str
) -> dict[str, Any]:
    """Delete a single store-listing image by ID and commit the edit.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        language: Language localization code (BCP-47 tag, e.g. en-US)
        image_type: Image type - one of: phoneScreenshots, sevenInchScreenshots,
            tenInchScreenshots, tvScreenshots, wearScreenshots, icon, featureGraphic,
            tvBanner
        image_id: Unique identifier of the image to delete

    Returns:
        Delete result with success status and deleted count
    """
    if blocked := _read_only_block("delete_image"):
        return blocked
    client = get_client_from_context()

    result = client.delete_image(
        package_name=package_name,
        language=language,
        image_type=image_type,
        image_id=image_id,
    )
    return result.model_dump()


@mcp.tool()
def delete_all_images(
    package_name: str,
    language: str,
    image_type: str,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Delete all store-listing images for a language and image type; commit the edit.

    Disabled in read-only mode. Two-step: preview, then confirm.

    Args:
        package_name: App package name
        language: Language localization code (BCP-47 tag, e.g. en-US)
        image_type: Image type to clear all images for - one of: phoneScreenshots,
            sevenInchScreenshots, tenInchScreenshots, tvScreenshots, wearScreenshots,
            icon, featureGraphic, tvBanner
        confirm: Set True only after the user approved the preview
        confirmation_token: The token from that preview; never invent one

    Returns:
        The preview block when unconfirmed, otherwise the delete result
    """
    if blocked := _read_only_block("delete_all_images"):
        return blocked

    op = Operation(
        tool="delete_all_images",
        target=target_for(package_name),
        params={"language": language, "image_type": image_type},
        call_args={
            "package_name": package_name,
            "language": language,
            "image_type": image_type,
        },
    )
    preview = Preview(
        summary=f"Delete every '{image_type}' image for locale '{language}'.",
        warnings=["The Play API has no undo; the images must be re-uploaded to restore them."],
        reversal="Re-upload the images with upload_image.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()

    with audit_execution(op):
        result = client.delete_all_images(
            package_name=package_name,
            language=language,
            image_type=image_type,
        )
    return result.model_dump()


# =============================================================================
# Validation Tools
# =============================================================================


@mcp.tool()
def validate_package_name(package_name: str) -> dict[str, Any]:
    """Validate package name format before using it in other operations.

    Args:
        package_name: Package name to validate (e.g., com.example.myapp)

    Returns:
        Validation result with any errors found
    """
    client = get_client_from_context()

    errors = client.validate_package_name(package_name)
    return {
        "valid": len(errors) == 0,
        "errors": [error.model_dump() for error in errors],
        "package_name": package_name,
    }


@mcp.tool()
def validate_track(track: str) -> dict[str, Any]:
    """Validate track name before using it in deployment operations.

    Args:
        track: Track name to validate (internal, alpha, beta, production)

    Returns:
        Validation result with any errors found
    """
    client = get_client_from_context()

    errors = client.validate_track(track)
    return {
        "valid": len(errors) == 0,
        "errors": [error.model_dump() for error in errors],
        "track": track,
    }


@mcp.tool()
def validate_listing_text(
    title: str | None = None,
    short_description: str | None = None,
    full_description: str | None = None,
) -> dict[str, Any]:
    """Validate store listing text lengths before updating.

    Args:
        title: App title (max 50 characters)
        short_description: Short description (max 80 characters)
        full_description: Full description (max 4000 characters)

    Returns:
        Validation result with any errors found
    """
    client = get_client_from_context()

    errors = client.validate_listing_text(title, short_description, full_description)
    return {
        "valid": len(errors) == 0,
        "errors": [error.model_dump() for error in errors],
    }


# =============================================================================
# Batch Operations Tools
# =============================================================================


@mcp.tool()
def batch_deploy(
    package_name: str,
    file_path: str,
    tracks: list[str],
    release_notes: str | None = None,
    rollout_percentages: dict[str, float] | None = None,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Deploy an app to multiple tracks in a single operation.

    This is useful for deploying to internal and alpha tracks simultaneously,
    or for promoting to multiple testing tracks at once.

    Args:
        package_name: App package name
        file_path: Absolute path to APK or AAB file
        tracks: List of tracks to deploy to (e.g., ["internal", "alpha"])
        release_notes: Optional release notes for all tracks
        rollout_percentages: Optional dict mapping track names to rollout percentages

    Returns:
        Batch deployment result with individual results for each track
    """
    if blocked := _read_only_block("batch_deploy"):
        return blocked
    if err := _validate_deploy_file(file_path):
        return {"error": err}

    if rollout_percentages:
        for track_name, pct in rollout_percentages.items():
            if not (0.0 <= pct <= 100.0):
                return {
                    "error": f"rollout_percentage for track '{track_name}' must be between 0.0 and 100.0"
                }

    op = Operation(
        tool="batch_deploy",
        target=target_for(package_name),
        params={
            "file": Path(file_path),
            "tracks": sorted(tracks),
            "rollout_percentages": rollout_percentages,
        },
        call_args={
            "package_name": package_name,
            "file_path": file_path,
            "tracks": tracks,
            "release_notes": release_notes,
            "rollout_percentages": rollout_percentages,
        },
    )
    preview = Preview(
        summary=f"Upload {Path(file_path).name} and release it on {len(tracks)} track(s): "
        f"{', '.join(tracks)}.",
        changes=[Change("tracks", None, ", ".join(tracks))],
        warnings=[
            w for t in tracks for w in _rollout_note((rollout_percentages or {}).get(t, 100.0), t)
        ],
        reversal="halt_release must be run per track.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()

    with audit_execution(op):
        result = client.batch_deploy(
            package_name=package_name,
            file_path=file_path,
            tracks=tracks,
            release_notes=release_notes,
            rollout_percentages=rollout_percentages,
        )
    return result.model_dump()


# =============================================================================
# Internal App Sharing Tools
# =============================================================================


@mcp.tool()
def upload_internal_app_sharing_apk(
    package_name: str,
    apk_path: str,
) -> dict[str, Any]:
    """Upload an APK to internal app sharing.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        apk_path: Local path to the APK file

    Returns:
        The uploaded artifact with download URL, certificate fingerprint, and sha256
    """
    if blocked := _read_only_block("upload_internal_app_sharing_apk"):
        return blocked
    client = get_client_from_context()

    artifact = client.upload_internal_app_sharing_apk(
        package_name=package_name,
        apk_path=apk_path,
    )
    return artifact.model_dump()


@mcp.tool()
def upload_internal_app_sharing_bundle(
    package_name: str,
    bundle_path: str,
) -> dict[str, Any]:
    """Upload an app bundle (.aab) to internal app sharing.

    Disabled in read-only mode.

    Args:
        package_name: App package name
        bundle_path: Local path to the app bundle (.aab) file

    Returns:
        The uploaded artifact with download URL, certificate fingerprint, and sha256
    """
    if blocked := _read_only_block("upload_internal_app_sharing_bundle"):
        return blocked
    client = get_client_from_context()

    artifact = client.upload_internal_app_sharing_bundle(
        package_name=package_name,
        bundle_path=bundle_path,
    )
    return artifact.model_dump()


# =============================================================================
# Play Developer Reporting API — app vitals, errors, anomalies
# =============================================================================
#
# A second Google API with its own OAuth scope, granted separately from
# publishing access in Play Console. All tools here are read-only.
#
# Callers pass a package name and a short metric-set key (crash_rate,
# anr_rate, ...) rather than a resource name like
# apps/com.example/crashRateMetricSet.


@mcp.tool()
def list_reporting_apps(max_results: int = 100) -> list[dict[str, Any]]:
    """List every app the service account can see through the Reporting API.

    The Publisher API has no "list my apps" endpoint, so this is the only way to
    enumerate a portfolio from a service account.

    Args:
        max_results: Maximum number of apps to return (default 100)

    Returns:
        List of apps with resource name, package name and display name
    """
    client = get_client_from_context()
    return [app.model_dump() for app in client.list_reporting_apps(max_results=max_results)]


@mcp.tool()
def list_metric_sets() -> list[dict[str, str]]:
    """List the vitals metric sets this server can query.

    Returns:
        Each metric set's short key (use it as the metric_set argument), the
        underlying Play resource suffix, and a human-readable label
    """
    return [
        {"metric_set": key, "resource_suffix": suffix, "label": label}
        for key, (suffix, _accessor, label) in sorted(METRIC_SETS.items())
    ]


@mcp.tool()
def get_release_filter_options(package_name: str) -> dict[str, Any]:
    """List the release dimensions usable as query filters for an app.

    Use this to discover which tracks and version codes can appear in a
    query_vitals filter expression.

    Args:
        package_name: App package name (e.g., com.example.myapp)

    Returns:
        Tracks and version codes available as filters
    """
    client = get_client_from_context()
    return client.get_release_filter_options(package_name=package_name).model_dump()


@mcp.tool()
def get_vitals_freshness(package_name: str, metric_set: str | None = None) -> list[dict[str, Any]]:
    """Report how current each vitals metric set's data is.

    Call this before acting on a vitals number. Play data lags reality by hours
    to days, so a query covering "the last 7 days" can silently return five —
    and the absent days look like an improvement rather than a gap.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        metric_set: Short metric set key (e.g., crash_rate), or omit for all sets

    Returns:
        Freshness per metric set and aggregation period. A metric set the
        service account cannot access is skipped rather than failing the batch.
    """
    client = get_client_from_context()
    try:
        results = client.get_metric_set_freshness(package_name=package_name, metric_set=metric_set)
    except ReportingClientError as e:
        return [{"error": str(e)}]
    return [info.model_dump() for info in results]


@mcp.tool()
def get_metric_set_metadata(package_name: str, metric_set: str) -> dict[str, Any]:
    """Get metadata for one vitals metric set, including its freshness.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        metric_set: Short metric set key (e.g., crash_rate, anr_rate)

    Returns:
        Metric set metadata, or an error object if the key is unknown
    """
    client = get_client_from_context()
    try:
        return client.get_metric_set(package_name=package_name, metric_set=metric_set).model_dump()
    except ReportingClientError as e:
        return {"error": str(e)}


@mcp.tool()
def query_vitals(
    package_name: str,
    metric_set: str,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
    days_back: int = 7,
    aggregation_period: str = "DAILY",
    filter_expression: str | None = None,
    user_cohort: str | None = None,
    max_results: int = 1000,
) -> dict[str, Any]:
    """Query an app vitals metric set timeline.

    Covers every vital Play exposes: crash rate, ANR rate, slow start, slow
    rendering, excessive wake-ups, stuck background wakelocks, low-memory-killer
    rate and error counts. Call list_metric_sets for the available keys and
    get_vitals_freshness to check the data is current before drawing conclusions.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        metric_set: Short metric set key (e.g., crash_rate, anr_rate)
        metrics: Metric names to return (e.g., ["crashRate", "distinctUsers"]).
                 Omit for the API default set.
        dimensions: Dimensions to slice by (e.g., ["versionCode", "countryCode"])
        days_back: Window size ending now, in days (default 7)
        aggregation_period: HOURLY, DAILY or FULL_RANGE (default DAILY)
        filter_expression: API filter string (e.g., 'versionCode = 42')
        user_cohort: OS_PUBLIC, OS_BETA or APP_TESTERS
        max_results: Maximum rows to return across pages (default 1000)

    Returns:
        Timeline rows with dimension and metric values, or an error object
    """
    client = get_client_from_context()
    try:
        result = client.query_metric_set(
            package_name=package_name,
            metric_set=metric_set,
            metrics=metrics,
            dimensions=dimensions,
            days_back=days_back,
            aggregation_period=aggregation_period,
            filter_expression=filter_expression,
            user_cohort=user_cohort,
            max_results=max_results,
        )
    except ReportingClientError as e:
        return {"error": str(e)}
    return result.model_dump()


@mcp.tool()
def list_vitals_anomalies(
    package_name: str,
    filter_expression: str | None = None,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    """List statistical anomalies Play detected across the vitals metric sets.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        filter_expression: Optional API filter string
        max_results: Maximum number of anomalies to return (default 100)

    Returns:
        Detected anomalies with the metric that deviated and its value
    """
    client = get_client_from_context()
    return [
        anomaly.model_dump()
        for anomaly in client.list_anomalies(
            package_name=package_name,
            filter_expression=filter_expression,
            max_results=max_results,
        )
    ]


@mcp.tool()
def search_error_issues(
    package_name: str,
    filter_expression: str | None = None,
    days_back: int = 7,
    order_by: str | None = None,
    sample_report_limit: int | None = None,
    max_results: int = 50,
) -> list[dict[str, Any]]:
    """Search grouped error issues — clusters of similar crashes or ANRs.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        filter_expression: Optional API filter (e.g., 'errorIssueType = CRASH')
        days_back: Window size ending now, in days (default 7)
        order_by: Sort expression (e.g., 'errorReportCount desc')
        sample_report_limit: Sample report names to attach per issue
        max_results: Maximum number of issues to return (default 50)

    Returns:
        Grouped error issues with report counts and affected user counts
    """
    client = get_client_from_context()
    return [
        issue.model_dump()
        for issue in client.search_error_issues(
            package_name=package_name,
            filter_expression=filter_expression,
            days_back=days_back,
            order_by=order_by,
            sample_report_limit=sample_report_limit,
            max_results=max_results,
        )
    ]


@mcp.tool()
def search_error_reports(
    package_name: str,
    filter_expression: str | None = None,
    days_back: int = 7,
    max_results: int = 50,
) -> list[dict[str, Any]]:
    """Search raw error reports — individual crashes and ANRs with stack traces.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        filter_expression: Optional API filter (e.g., 'errorIssueId = abc123')
        days_back: Window size ending now, in days (default 7)
        max_results: Maximum number of reports to return (default 50)

    Returns:
        Individual error reports including report text where available
    """
    client = get_client_from_context()
    return [
        report.model_dump()
        for report in client.search_error_reports(
            package_name=package_name,
            filter_expression=filter_expression,
            days_back=days_back,
            max_results=max_results,
        )
    ]


# =============================================================================
# Publisher v3 — endpoints beyond the core client
# =============================================================================


@mcp.tool()
def validate_edit(package_name: str) -> dict[str, Any]:
    """Dry-run an edit: open one, validate it, then discard it.

    Nothing is committed. Most useful as a permissions probe before a deploy —
    a failure here means the service account cannot publish, which is better
    learned before a release than during one.

    Args:
        package_name: App package name (e.g., com.example.myapp)

    Returns:
        Validation result with success status and any error detail
    """
    if blocked := _read_only_block("validate_edit"):
        return blocked

    client = get_client_from_context()
    return client.validate_edit(package_name=package_name).model_dump()


@mcp.tool()
def get_country_availability(package_name: str, track: str) -> dict[str, Any]:
    """Read which countries a track is distributed to.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        track: Track name (e.g., production, beta, alpha, internal)

    Returns:
        Country codes, rest-of-world flag, and whether the track mirrors production
    """
    client = get_client_from_context()
    return client.get_country_availability(package_name=package_name, track=track).model_dump()


@mcp.tool()
def update_app_details(
    package_name: str,
    default_language: str | None = None,
    contact_email: str | None = None,
    contact_phone: str | None = None,
    contact_website: str | None = None,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Update app-level details: default language and store contact information.

    Only the fields supplied are changed; the rest are read from the current
    details and written back unchanged, because the Play API replaces the whole
    resource on update.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        default_language: Default listing language (e.g., en-US)
        contact_email: Developer contact email shown on the store listing
        contact_phone: Developer contact phone shown on the store listing
        contact_website: Developer website shown on the store listing

    Returns:
        Update result with the app details after the change
    """
    if blocked := _read_only_block("update_app_details"):
        return blocked

    op = Operation(
        tool="update_app_details",
        target=target_for(package_name),
        params={
            "default_language": default_language,
            "contact_email": contact_email,
            "contact_phone": contact_phone,
            "contact_website": contact_website,
        },
        call_args={
            "package_name": package_name,
            "default_language": default_language,
            "contact_email": contact_email,
            "contact_phone": contact_phone,
            "contact_website": contact_website,
        },
    )
    preview = Preview(
        summary="Change app-level details shown on the public store listing.",
        changes=[
            Change(name, None, value)
            for name, value in (
                ("default_language", default_language),
                ("contact_email", contact_email),
                ("contact_phone", contact_phone),
                ("contact_website", contact_website),
            )
            if value is not None
        ],
        warnings=["Contact details are public on the store listing."],
        reversal="Call update_app_details again with the previous values.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()
    with audit_execution(op):
        result = client.update_app_details(
            package_name=package_name,
            default_language=default_language,
            contact_email=contact_email,
            contact_phone=contact_phone,
            contact_website=contact_website,
        )
    return result.model_dump()


@mcp.tool()
def create_track(
    package_name: str,
    track: str,
    releases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a custom closed-testing track.

    The four built-in tracks (internal, alpha, beta, production) always exist.
    This creates an additional named track, which is how closed testing groups
    beyond the built-ins are set up.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        track: Name for the new track
        releases: Optional initial release configuration for the track

    Returns:
        Track action result with the releases on the track afterwards
    """
    if blocked := _read_only_block("create_track"):
        return blocked

    client = get_client_from_context()
    return client.create_track(
        package_name=package_name, track=track, releases=releases
    ).model_dump()


@mcp.tool()
def update_track(
    package_name: str,
    track: str,
    releases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Update a track's release configuration.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        track: Track to update
        releases: Release configuration to write to the track

    Returns:
        Track action result with the releases on the track afterwards
    """
    if blocked := _read_only_block("update_track"):
        return blocked

    client = get_client_from_context()
    return client.update_track(
        package_name=package_name, track=track, releases=releases
    ).model_dump()


@mcp.tool()
def delete_listing(
    package_name: str,
    language: str,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Delete one locale's store listing.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        language: Language code to delete (e.g., de-DE)

    Returns:
        Deletion result
    """
    if blocked := _read_only_block("delete_listing"):
        return blocked

    op = Operation(
        tool="delete_listing",
        target=target_for(package_name),
        params={"language": language},
        call_args={"package_name": package_name, "language": language},
    )
    preview = Preview(
        summary=f"Delete the store listing for locale '{language}'.",
        changes=[],
        warnings=["The Play API has no undo; the listing text must be re-entered to restore it."],
        reversal="Re-create the listing with update_listing.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()
    return client.delete_listing(package_name=package_name, language=language).model_dump()


@mcp.tool()
def delete_all_listings(
    package_name: str,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Delete every localized store listing for an app.

    Destructive: the listing text for all locales is removed in one commit and
    the API offers no undo. Two-step: preview, then confirm.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        confirm: Set True only after the user approved the preview
        confirmation_token: The token from that preview; never invent one

    Returns:
        The preview block when unconfirmed, otherwise the deletion result
    """
    if blocked := _read_only_block("delete_all_listings"):
        return blocked

    op = Operation(
        tool="delete_all_listings",
        target=target_for(package_name),
        params={"scope": "all_locales"},
        call_args={"package_name": package_name},
    )
    preview = Preview(
        summary="Delete the store listing text for EVERY locale of this app.",
        warnings=[
            "This removes the title, descriptions and video for all languages at once.",
            "The Play API has no undo. Restoring means re-uploading every locale by hand.",
        ],
        reversal="None through the API.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()
    with audit_execution(op):
        result = client.delete_all_listings(package_name=package_name)
    return result.model_dump()


@mcp.tool()
def add_externally_hosted_apk(
    package_name: str,
    externally_hosted_apk: dict[str, Any],
) -> dict[str, Any]:
    """Register an APK hosted outside Play (enterprise distribution only).

    Usable only by organisations enrolled in Play for Work private-channel
    distribution; a normal developer account receives a 403.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        externally_hosted_apk: ExternallyHostedApk resource body. Must include
                               externallyHostedUrl, fileSha1Base64,
                               fileSha256Base64, fileSize, packageName,
                               versionCode, and the manifest fields Play would
                               otherwise read from the APK itself.

    Returns:
        The registered externally hosted APK
    """
    if blocked := _read_only_block("add_externally_hosted_apk"):
        return blocked

    client = get_client_from_context()
    return client.add_externally_hosted_apk(
        package_name=package_name, externally_hosted_apk=externally_hosted_apk
    ).model_dump()


@mcp.tool()
def patch_expansion_file(
    package_name: str,
    version_code: int,
    expansion_file_type: str,
    references_version: int,
) -> dict[str, Any]:
    """Point a version's OBB at one already uploaded against another version.

    Large apps reuse the same expansion file across builds. This associates an
    existing OBB with a new version code instead of re-uploading it. Uses the
    API's patch (partial) semantics.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        version_code: Version code to attach the reference to
        expansion_file_type: "main" or "patch"
        references_version: Version code whose expansion file to reference

    Returns:
        The expansion file after the change
    """
    if blocked := _read_only_block("patch_expansion_file"):
        return blocked

    client = get_client_from_context()
    try:
        return client.set_expansion_file_reference(
            package_name=package_name,
            version_code=version_code,
            expansion_file_type=expansion_file_type,
            references_version=references_version,
            patch=True,
        ).model_dump()
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def update_expansion_file(
    package_name: str,
    version_code: int,
    expansion_file_type: str,
    references_version: int,
) -> dict[str, Any]:
    """Set a version's OBB reference, replacing the whole resource.

    Same effect as patch_expansion_file but with the API's update (full
    replace) semantics.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        version_code: Version code to attach the reference to
        expansion_file_type: "main" or "patch"
        references_version: Version code whose expansion file to reference

    Returns:
        The expansion file after the change
    """
    if blocked := _read_only_block("update_expansion_file"):
        return blocked

    client = get_client_from_context()
    try:
        return client.set_expansion_file_reference(
            package_name=package_name,
            version_code=version_code,
            expansion_file_type=expansion_file_type,
            references_version=references_version,
            patch=False,
        ).model_dump()
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def acknowledge_subscription_purchase(
    package_name: str,
    subscription_id: str,
    purchase_token: str,
    developer_payload: str | None = None,
) -> dict[str, Any]:
    """Acknowledge a subscription purchase.

    Google automatically refunds and revokes any subscription purchase that is
    not acknowledged within three days, so this must run for every new
    subscription purchase.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        subscription_id: Subscription product ID
        purchase_token: Purchase token from the client
        developer_payload: Optional payload to store with the purchase

    Returns:
        Action result
    """
    if blocked := _read_only_block("acknowledge_subscription_purchase"):
        return blocked

    client = get_client_from_context()
    return client.acknowledge_subscription_purchase(
        package_name=package_name,
        subscription_id=subscription_id,
        purchase_token=purchase_token,
        developer_payload=developer_payload,
    ).model_dump()


@mcp.tool()
def archive_subscription(package_name: str, product_id: str) -> dict[str, Any]:
    """Archive a subscription.

    Play recommends archiving rather than deleting any subscription that has
    ever been purchased: deletion is rejected once purchases exist, and
    archiving preserves the record for existing subscribers.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        product_id: Subscription product ID

    Returns:
        Action result
    """
    if blocked := _read_only_block("archive_subscription"):
        return blocked

    client = get_client_from_context()
    return client.archive_subscription(
        package_name=package_name, product_id=product_id
    ).model_dump()


@mcp.tool()
def convert_region_prices(
    package_name: str,
    price_micros: str,
    currency_code: str,
) -> dict[str, Any]:
    """Convert one source price into every Play region's price.

    Returns Play's own recommended prices rather than a naive FX conversion —
    values are rounded to each region's local price conventions.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        price_micros: Source price in micros (1 unit = 1,000,000 micros)
        currency_code: Source currency code (e.g., USD)

    Returns:
        Converted price per region
    """
    client = get_client_from_context()
    return client.convert_region_prices(
        package_name=package_name,
        price_micros=price_micros,
        currency_code=currency_code,
    ).model_dump()


@mcp.tool()
def delete_review_reply(
    package_name: str,
    review_id: str,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Retract a developer reply to a review.

    The Play API has no delete-reply endpoint. The reply is retracted by
    replying again with empty text, which clears the visible reply; the result
    records the mechanism so this is not mistaken for erasure from history.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        review_id: Review whose reply should be retracted

    Returns:
        Retraction result
    """
    if blocked := _read_only_block("delete_review_reply"):
        return blocked

    op = Operation(
        tool="delete_review_reply",
        target=target_for(package_name),
        params={"review_id": review_id},
        call_args={"package_name": package_name, "review_id": review_id},
    )
    preview = Preview(
        summary=f"Retract the public developer reply to review {review_id}.",
        changes=[Change("public reply", "visible", "cleared")],
        warnings=[
            "The Play API has no delete-reply endpoint; this publishes an EMPTY reply, "
            "which clears the visible text but does not erase it from history."
        ],
        reversal="Reply again with reply_to_review.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()
    with audit_execution(op):
        result = client.delete_review_reply(package_name=package_name, review_id=review_id)
    return result.model_dump()


# =============================================================================
# Cloud Storage bulk reports — installs, ratings, earnings, search terms
# =============================================================================
#
# A third data source with its own OAuth scope (devstorage.read_only) and its
# own configuration (GOOGLE_PLAY_MCP_REPORTS_BUCKET). These numbers exist in NO Play
# REST API — only as monthly CSVs in a private bucket. All tools here are reads.
#
# Every result carries a `freshness` block. Read it: Play stats land 3-7 days
# late and earnings around the 5th of the following month, so a zero can mean
# "not published yet" rather than "nothing happened".


@mcp.tool()
def check_reports_access() -> dict[str, Any]:
    """Verify the Play reports bucket is configured and readable. Run this first.

    Installs, ratings, earnings and search terms come from a private Cloud
    Storage bucket rather than an API, and it needs separate setup: the bucket
    id in GOOGLE_PLAY_MCP_REPORTS_BUCKET, and the ACCOUNT-level Play Console
    permission "View app information and download bulk reports".

    Returns:
        A diagnosis with the exact remedy when something is missing
    """
    client = get_client_from_context()
    return client.check_reports_access()


@mcp.tool()
def list_report_families() -> list[dict[str, Any]]:
    """List the bulk-report families this server can read.

    Returns:
        Each family's key, bucket prefix, available dimensions and description.
        A family Google no longer publishes carries a `discontinued` note —
        those return no rows for recent months by design, not by failure.
    """
    return [family.model_dump() for family in list_families()]


@mcp.tool()
def list_report_objects(prefix: str, max_results: int = 100) -> dict[str, Any]:
    """List raw object names under a prefix in the reports bucket.

    Diagnostic tool: use it when a report comes back empty to see what Play has
    actually published.

    Args:
        prefix: Object prefix, e.g. "stats/installs/" or "earnings/"
        max_results: Maximum object names to return (default 100)

    Returns:
        The object names found, or an error with the remedy
    """
    client = get_client_from_context()
    try:
        return {"objects": client.list_report_objects(prefix, max_results=max_results)}
    except PlayConsoleError as exc:
        return exc.to_dict()


@mcp.tool()
def get_installs_report(
    package_name: str,
    month: str,
    dimension: str = "overview",
) -> dict[str, Any]:
    """Daily and active-device installs, uninstalls and upgrades for one month.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        month: Month as YYYY-MM (e.g., 2026-07)
        dimension: overview, country, device, language, app_version, os_version
                   or carrier (default: overview)

    Returns:
        Totals per metric, the rows, and a freshness block saying how complete
        the month is
    """
    return _report_tool("installs", package_name, month, dimension)


@mcp.tool()
def get_ratings_report(
    package_name: str,
    month: str,
    dimension: str = "overview",
) -> dict[str, Any]:
    """Daily and total average rating for one month.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        month: Month as YYYY-MM (e.g., 2026-07)
        dimension: overview, country, device, language, app_version, os_version
                   or carrier (default: overview)

    Returns:
        Totals per metric, the rows, and a freshness block
    """
    return _report_tool("ratings", package_name, month, dimension)


@mcp.tool()
def get_crashes_report(
    package_name: str,
    month: str,
    dimension: str = "overview",
) -> dict[str, Any]:
    """Daily crash and ANR counts for one month.

    Distinct from query_vitals: this is a raw daily count from the bulk report,
    while the Reporting API gives user-weighted rates against Google's
    bad-behaviour thresholds. Use query_vitals to judge release health.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        month: Month as YYYY-MM (e.g., 2026-07)
        dimension: overview, device, os_version or app_version (default: overview)

    Returns:
        Totals per metric, the rows, and a freshness block
    """
    return _report_tool("crashes", package_name, month, dimension)


@mcp.tool()
def get_store_performance_report(
    package_name: str,
    month: str,
    dimension: str = "traffic_source",
) -> dict[str, Any]:
    """Store-listing acquisitions by traffic source or country.

    Traffic sources are Play's own: Explore, Search, and Ads / referrals. For
    visitors and conversion rate use get_store_conversion_report.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        month: Month as YYYY-MM (e.g., 2026-07)
        dimension: traffic_source or country (default: traffic_source)

    Returns:
        Totals per metric, the rows, and a freshness block
    """
    return _report_tool("store_performance", package_name, month, dimension)


@mcp.tool()
def get_store_conversion_report(
    package_name: str,
    month: str,
    dimension: str = "traffic_source",
) -> dict[str, Any]:
    """Store-listing visitors, acquisitions and visitor-to-install conversion rate.

    The underlying rows also break down by SEARCH TERM and UTM campaign — this
    is the only place either is available anywhere in Play's APIs. Use
    get_search_terms for the search-term view specifically.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        month: Month as YYYY-MM (e.g., 2026-07)
        dimension: traffic_source or country (default: traffic_source)

    Returns:
        Totals per metric, the rows, and a freshness block
    """
    return _report_tool("store_conversion", package_name, month, dimension)


@mcp.tool()
def get_search_terms(package_name: str, month: str, top: int = 50) -> dict[str, Any]:
    """What users searched to find the app, with acquisitions per term.

    Pure ASO data available from no Play API — it exists only in the store
    performance CSV. Terms are ranked by acquisitions.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        month: Month as YYYY-MM (e.g., 2026-07)
        top: How many terms to return (default 50)

    Returns:
        Ranked search terms with their totals, plus a freshness block
    """
    client = get_client_from_context()
    try:
        report = client.get_stats_report(
            "store_conversion", package_name, month, dimension="search_term"
        )
    except PlayConsoleError as exc:
        return exc.to_dict()

    return {
        "package_name": package_name,
        "month": month,
        "search_terms": group_rows(report, top=top),
        "freshness": report.freshness.model_dump(),
        "source_object": report.source_object,
    }


@mcp.tool()
def get_subscriptions_report(package_name: str, month: str) -> dict[str, Any]:
    """New, cancelled and active subscriptions by country for one month.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        month: Month as YYYY-MM (e.g., 2026-07)

    Returns:
        Totals per metric, the rows, and a freshness block
    """
    return _report_tool("subscriptions", package_name, month, "country")


@mcp.tool()
def get_earnings_report(month: str, package_name: str | None = None) -> dict[str, Any]:
    """Transaction-level earnings for one month, in merchant currency.

    Totals are reported PER CURRENCY and never summed across currencies —
    adding 1000 JPY to 10 EUR would be a wrong answer that looks like a right
    one, and this server has no exchange rates.

    Earnings for a month are published around the 5th of the following month.
    Before that the report does not exist, and the freshness block says so
    rather than reporting zero revenue.

    Args:
        month: Month as YYYY-MM (e.g., 2026-07)
        package_name: Filter to one app; omit for the whole developer account

    Returns:
        Totals per currency, per-app breakdown, and a freshness block
    """
    client = get_client_from_context()
    try:
        report = client.get_earnings(month, package_name=package_name)
    except PlayConsoleError as exc:
        return exc.to_dict()

    return {
        "month": month,
        "package_name": package_name,
        "by_currency": earnings_by_currency(report),
        "by_app": earnings_by_app(report),
        "transaction_count": len(report.rows),
        "freshness": report.freshness.model_dump(),
        "source_object": report.source_object,
    }


# =============================================================================
# Public store scraping — competitors, keywords, review history
# =============================================================================
#
# The only tools here that do not read your own account. No credentials are
# needed; these read the public store the way a user in a given country sees it.
#
# All returned text is flattened to single lines: review bodies and reviewer
# names are written by strangers and reach the model as context, so they must
# never be able to imitate this server's own output. Treat them as data.


@mcp.tool()
def search_store(
    term: str,
    country: str = "us",
    language: str = "en",
    num: int = 20,
    price: str = "all",
) -> dict[str, Any]:
    """Search the public Play Store as a user in a given country would see it.

    Store ranking is country- and language-specific, so those arguments change
    the answer rather than decorate it. Use this for competitor research and to
    see where an app ranks for a term.

    Args:
        term: Search term
        country: Two-letter country code (default: us)
        language: Language code (default: en)
        num: How many results to return (default 20)
        price: all, free or paid (default: all)

    Returns:
        Ranked apps with title, developer, score and summary
    """
    client = get_client_from_context()
    try:
        apps = client.scrape_search(term, country=country, language=language, num=num, price=price)
    except PlayConsoleError as exc:
        return exc.to_dict()
    return {
        "term": term,
        "country": country,
        "results": [app.model_dump() for app in apps],
        "untrusted_content_note": UNTRUSTED_CONTENT_NOTE,
    }


@mcp.tool()
def get_store_listing(app_id: str, country: str = "us", language: str = "en") -> dict[str, Any]:
    """Read any app's public store listing — including a competitor's.

    Args:
        app_id: Package name (e.g., com.example.myapp)
        country: Two-letter country code (default: us)
        language: Language code (default: en)

    Returns:
        The full public listing: description, installs, ratings histogram, price
    """
    client = get_client_from_context()
    try:
        details = client.scrape_app(app_id, country=country, language=language)
    except PlayConsoleError as exc:
        return exc.to_dict()
    return {**details.model_dump(), "untrusted_content_note": UNTRUSTED_CONTENT_NOTE}


@mcp.tool()
def browse_store_chart(
    collection: str = "TOP_FREE",
    category: str | None = None,
    country: str = "us",
    language: str = "en",
    num: int = 50,
    age: str | None = None,
) -> dict[str, Any]:
    """Browse a store chart: top free, top paid or top grossing.

    Args:
        collection: TOP_FREE, TOP_PAID or GROSSING (default: TOP_FREE)
        category: Store category to filter to; omit for all categories
        country: Two-letter country code (default: us)
        language: Language code (default: en)
        num: How many apps to return (default 50)
        age: Age filter — FIVE_UNDER, SIX_EIGHT or NINE_UP

    Returns:
        The ranked chart
    """
    client = get_client_from_context()
    try:
        apps = client.scrape_collection(
            collection=collection,
            category=category,
            country=country,
            language=language,
            num=num,
            age=age,
        )
    except PlayConsoleError as exc:
        return exc.to_dict()
    return {
        "collection": collection,
        "category": category,
        "country": country,
        "results": [app.model_dump() for app in apps],
        "untrusted_content_note": UNTRUSTED_CONTENT_NOTE,
    }


@mcp.tool()
def get_keyword_suggestions(
    keyword: str, country: str = "us", language: str = "en"
) -> dict[str, Any]:
    """Play's own autocomplete suggestions for a keyword.

    These are the terms Play believes users actually type, which is the closest
    thing to keyword volume the public store exposes. Pair with get_search_terms
    (bulk reports) to see which of them actually converted for your app.

    Args:
        keyword: Seed keyword
        country: Two-letter country code (default: us)
        language: Language code (default: en)

    Returns:
        Suggested search terms
    """
    client = get_client_from_context()
    try:
        return {
            "keyword": keyword,
            "country": country,
            "suggestions": client.scrape_suggestions(keyword, country=country, language=language),
        }
    except PlayConsoleError as exc:
        return exc.to_dict()


@mcp.tool()
def scrape_reviews(
    app_id: str,
    country: str = "us",
    language: str = "en",
    num: int = 100,
    sort: str = "NEWEST",
) -> dict[str, Any]:
    """Read public reviews, going back further than the Reviews API can.

    The authenticated get_reviews tool returns roughly ONE WEEK — that is the
    API's limit, not this server's. Scraping is the only way to read older
    reviews, and it works for competitors' apps too.

    Args:
        app_id: Package name (e.g., com.example.myapp)
        country: Two-letter country code (default: us)
        language: Language code (default: en)
        num: How many reviews to return (default 100)
        sort: NEWEST, RATING or HELPFULNESS (default: NEWEST)

    Returns:
        Reviews with rating, text, developer reply and version
    """
    client = get_client_from_context()
    try:
        page = client.scrape_reviews(app_id, country=country, language=language, num=num, sort=sort)
    except PlayConsoleError as exc:
        return exc.to_dict()
    return {**page.model_dump(), "untrusted_content_note": UNTRUSTED_CONTENT_NOTE}


@mcp.tool()
def list_store_enums() -> dict[str, list[str]]:
    """The collection, category, sort and age values the store tools accept.

    Returns:
        Valid values per argument
    """
    return {
        "collections": list_collections(),
        "categories": list_categories(),
        "sort_orders": list_sort_orders(),
        "age_filters": list_age_filters(),
        "price_filters": list(PRICE_FILTERS),
    }


@mcp.tool()
def check_aso_metadata(
    title: str | None = None,
    short_description: str | None = None,
    full_description: str | None = None,
) -> dict[str, Any]:
    """Check listing text against Play POLICY limits, not just what the API accepts.

    The distinction matters: the Android Publisher API accepts a 50-character
    title and Play then rejects the app at review, because policy caps titles at
    30. A title between 31 and 50 characters is reported as a warning — it will
    upload successfully and fail review.

    Use validate_listing_text for the API-level check before an upload; use this
    before deciding on the text.

    Args:
        title: App title
        short_description: Short description
        full_description: Full description

    Returns:
        Lengths, hard errors, policy warnings and the limits applied
    """
    return _check_aso_metadata(
        title=title,
        short_description=short_description,
        full_description=full_description,
    )


# =============================================================================
# Store-image preflight
# =============================================================================
#
# Google Play Console MCP does not generate screenshots. It checks that a rendered file
# actually meets Play's rules before an upload that would otherwise 400 after a
# long render, and then uploads it.
#
# The rule people get backwards: screenshots and the feature graphic must have
# NO alpha channel, while the icon MUST have one. Any exporter defaulting to
# "PNG" produces RGBA, which is right for the icon and rejected for everything
# else.


@mcp.tool()
def list_store_image_specs() -> list[dict[str, Any]]:
    """Play's requirements for every store-listing image type.

    Returns:
        Per type: accepted formats, whether an alpha channel is required or
        forbidden, exact or bounded dimensions, aspect limit and size cap
    """
    return list_image_specs()


@mcp.tool()
def preflight_store_image(image_path: str, image_type: str) -> dict[str, Any]:
    """Check one image against Play's rules before uploading it.

    Reads the file's own header rather than trusting its extension — a .png that
    is really a JPEG is something export pipelines produce, and Play reads the
    bytes too.

    Returns every problem at once rather than the first, so a fix round-trip
    handles all of them.

    Args:
        image_path: Local path to the rendered image
        image_type: phoneScreenshots, sevenInchScreenshots, tenInchScreenshots,
                    tvScreenshots, wearScreenshots, icon, featureGraphic or tvBanner

    Returns:
        valid flag, what was detected, every rule broken, and the spec applied
    """
    try:
        return preflight_image(image_path, image_type)
    except PlayConsoleError as exc:
        return exc.to_dict()


@mcp.tool()
def preflight_screenshot_set(
    image_paths: list[str],
    image_type: str = "phoneScreenshots",
) -> dict[str, Any]:
    """Check a whole screenshot set — every file, plus Play's count rules.

    Play requires at least TWO screenshots per type per locale and caps at
    eight. A set of one is the common failure and it is invisible file by file,
    because each individual image is perfectly valid.

    Args:
        image_paths: Local paths to the rendered screenshots for one locale
        image_type: Screenshot type the set is for (default: phoneScreenshots)

    Returns:
        valid flag, set-level errors, and the per-image report for any that failed
    """
    try:
        return _preflight_screenshot_set(image_paths, image_type)
    except PlayConsoleError as exc:
        return exc.to_dict()


# =============================================================================
# Edit lifecycle, escape hatch, locales
# =============================================================================
#
# Every other mutating tool here opens an edit, does one thing and commits.
# These expose the lifecycle so a multi-step change can be ONE transaction and
# one review submission.
#
# commit_edit is gated, and its preview is read back from Play rather than built
# from arguments — the content was staged by earlier calls, so reading the edit
# is the only honest account of what committing would publish.


@mcp.tool()
def create_edit(package_name: str) -> dict[str, Any]:
    """Open a draft edit and return its ID.

    Nothing reaches users until commit_edit. Stage changes against the returned
    edit ID, inspect with describe_edit, then commit once.

    Edits expire after about seven days. An abandoned one is harmless but worth
    discarding with delete_edit.

    Args:
        package_name: App package name (e.g., com.example.myapp)

    Returns:
        The edit ID and what to do next
    """
    if blocked := _read_only_block("create_edit"):
        return blocked

    client = get_client_from_context()
    return client.open_edit(package_name=package_name).model_dump()


@mcp.tool()
def describe_edit(package_name: str, edit_id: str) -> dict[str, Any]:
    """Read back everything currently staged in an open edit.

    Use before committing. Sections that cannot be read are listed under
    `unreadable` rather than failing — an edit with no listings staged returns
    404 for listings, which is information, not an error.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        edit_id: Edit ID from create_edit

    Returns:
        Staged track releases, listing locales, app details and binaries
    """
    client = get_client_from_context()
    return client.describe_edit(package_name=package_name, edit_id=edit_id).model_dump()


@mcp.tool()
def validate_open_edit(package_name: str, edit_id: str) -> dict[str, Any]:
    """Dry-run an open edit without committing or discarding it.

    Unlike validate_edit, which opens and throws away its own edit, this checks
    one you are building.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        edit_id: Edit ID from create_edit

    Returns:
        Whether the edit would commit cleanly
    """
    client = get_client_from_context()
    return client.validate_open_edit(package_name=package_name, edit_id=edit_id)


@mcp.tool()
def commit_edit(
    package_name: str,
    edit_id: str,
    changes_not_sent_for_review: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Commit an edit, publishing everything staged in it.

    Two-step. The preview is read back from Play — it lists the track releases,
    listing locales and binaries actually staged — because the content came from
    earlier calls rather than this one's arguments.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        edit_id: Edit ID from create_edit
        changes_not_sent_for_review: Hold the changes out of review after a
            rejection until they are sent from the Play Console UI
        confirm: Set True only after the user approved the preview
        confirmation_token: The token from that preview; never invent one

    Returns:
        The preview block when unconfirmed, otherwise the commit result
    """
    if blocked := _read_only_block("commit_edit"):
        return blocked

    client = get_client_from_context()
    op = Operation(
        tool="commit_edit",
        target=target_for(package_name),
        params={
            "edit_id": edit_id,
            "changes_not_sent_for_review": changes_not_sent_for_review,
        },
        call_args={
            "package_name": package_name,
            "edit_id": edit_id,
            "changes_not_sent_for_review": changes_not_sent_for_review,
        },
    )

    def build_preview() -> Preview:
        staged = client.describe_edit(package_name=package_name, edit_id=edit_id)
        changes = [
            Change(
                "track releases",
                None,
                "; ".join(
                    f"{r.get('track')}: {r.get('status')} {r.get('versionCodes')}"
                    for r in staged.track_releases
                )
                or "(none)",
            ),
            Change("listing locales", None, ", ".join(staged.listing_languages) or "(none)"),
            Change(
                "binaries",
                None,
                ", ".join(str(v) for v in staged.apk_version_codes + staged.bundle_version_codes)
                or "(none)",
            ),
        ]
        warnings = []
        if staged.is_empty:
            warnings.append(
                "This edit stages nothing readable. Committing it publishes no change — "
                "check the edit ID is the one you staged against."
            )
        if any(r.get("track") == "production" for r in staged.track_releases):
            warnings.append("A PRODUCTION release is staged — this reaches real users.")
        warnings.extend(f"Could not read {section}" for section in staged.unreadable)

        return Preview(
            summary=f"Commit edit {edit_id}, publishing everything staged in it.",
            changes=changes,
            warnings=warnings,
            reversal=("None directly. A committed edit is published; correct it with a new edit."),
            verified_by="read back from the open edit",
        )

    if early := _gate(op, build_preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    with audit_execution(op):
        result = client.commit_open_edit(
            package_name=package_name,
            edit_id=edit_id,
            changes_not_sent_for_review=changes_not_sent_for_review,
        )
    return result


@mcp.tool()
def delete_edit(package_name: str, edit_id: str) -> dict[str, Any]:
    """Discard an open edit. Nothing staged in it takes effect.

    Not gated: discarding a draft is the safe direction, and an agent that has
    to ask permission to clean up leaves edits open instead.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        edit_id: Edit ID to discard

    Returns:
        Confirmation that the edit was discarded
    """
    if blocked := _read_only_block("delete_edit"):
        return blocked

    op = Operation(
        tool="delete_edit",
        target=target_for(package_name),
        params={"edit_id": edit_id},
    )
    client = get_client_from_context()
    with unguarded(op, reason="discarding a draft edit publishes nothing"):
        result = client.discard_edit(package_name=package_name, edit_id=edit_id)
    return result


@mcp.tool()
def android_publisher_invoke(
    method_path: str,
    params: dict[str, Any] | None = None,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Call any Android Publisher v3 method directly, by dotted path.

    An escape hatch for endpoints this server has not wrapped and for ones
    Google ships after this release. Prefer the dedicated tools: they validate
    input, normalise output and carry the safety rules. Use this when there is
    no dedicated tool.

    Read methods run immediately. Anything that mutates is a two-step confirmed
    write, decided from the HTTP verb the API itself uses.

    Args:
        method_path: Dotted path, e.g. "edits.tracks.list" or
                     "purchases.voidedpurchases.list"
        params: Keyword arguments for the method, as the API names them
                (packageName, editId, ...)
        confirm: Set True only after the user approved the preview
        confirmation_token: The token from that preview; never invent one

    Returns:
        The raw API response, or the preview block for a mutating call
    """
    client = get_client_from_context()

    # Resolving first tells us the HTTP verb, which is what decides whether this
    # needs the gate. A read must not be made to round-trip for confirmation.
    try:
        request, http_method = client.resolve_publisher_request(method_path, params or {})
    except PlayConsoleError as exc:
        return exc.to_dict()

    if http_method in READ_ONLY_HTTP_METHODS:
        try:
            return {"method_path": method_path, "http_method": http_method, "result": request()}
        except PlayConsoleError as exc:
            return exc.to_dict()

    if blocked := _read_only_block(f"android_publisher_invoke({method_path})"):
        return blocked

    op = Operation(
        tool="android_publisher_invoke",
        target=target_for(str((params or {}).get("packageName", "unknown"))),
        params={"method_path": method_path, "params": params or {}},
        call_args={"method_path": method_path, "params": params or {}},
    )
    preview = Preview(
        summary=f"Call {method_path} ({http_method}) directly against the Publisher API.",
        changes=[Change("parameters", None, json.dumps(params or {}, default=str)[:400])],
        warnings=[
            f"This is a raw {http_method} call. Google Play Console MCP does not know what it changes, "
            "cannot validate the arguments, and has no undo for it.",
            "Prefer a dedicated tool where one exists.",
        ],
        reversal="Unknown — this is a generic call.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    with audit_execution(op):
        result = request()
    return {"method_path": method_path, "http_method": http_method, "result": result}


@mcp.tool()
def list_locale_coverage(
    package_name: str,
    target_locales: list[str] | None = None,
) -> dict[str, Any]:
    """Which locales have a store listing, and which targets are still missing.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        target_locales: Locales you intend to ship; omit to just list what exists

    Returns:
        Present locales, plus missing and extra ones when targets are given
    """
    client = get_client_from_context()
    return client.list_locale_coverage(
        package_name=package_name, target_locales=target_locales
    ).model_dump()


@mcp.tool()
def clone_listing_to_locale(
    package_name: str,
    source_language: str,
    target_language: str,
    overwrite: bool = False,
    confirm: bool = False,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Copy a store listing's TEXT from one locale to another.

    Copies title, descriptions and video. Images are not cloned — that means
    downloading and re-uploading every asset, a different operation with
    different failure modes.

    A locale that already has a listing is left alone unless overwrite is set,
    so this cannot silently replace translated copy with the source language.

    Args:
        package_name: App package name (e.g., com.example.myapp)
        source_language: Locale to copy from (e.g., en-US)
        target_language: Locale to copy to (e.g., en-GB)
        overwrite: Replace an existing listing in the target locale
        confirm: Set True only after the user approved the preview
        confirmation_token: The token from that preview; never invent one

    Returns:
        The preview block when unconfirmed, otherwise the result
    """
    if blocked := _read_only_block("clone_listing_to_locale"):
        return blocked

    op = Operation(
        tool="clone_listing_to_locale",
        target=target_for(package_name),
        params={
            "source_language": source_language,
            "target_language": target_language,
            "overwrite": overwrite,
        },
        call_args={
            "package_name": package_name,
            "source_language": source_language,
            "target_language": target_language,
            "overwrite": overwrite,
        },
    )
    preview = Preview(
        summary=f"Copy the {source_language} listing text into {target_language}.",
        changes=[Change("listing text", source_language, target_language)],
        warnings=(
            [
                f"overwrite=True will REPLACE any existing {target_language} listing, "
                f"including human translation, with {source_language} text."
            ]
            if overwrite
            else []
        ),
        reversal="Restore the previous text with update_listing.",
    )
    if early := _gate(op, preview, confirm=confirm, confirmation_token=confirmation_token):
        return early

    client = get_client_from_context()
    with audit_execution(op):
        result = client.clone_listing_to_locale(
            package_name=package_name,
            source_language=source_language,
            target_language=target_language,
            overwrite=overwrite,
        )
    return result.model_dump()


# =============================================================================
# Resources
# =============================================================================
#
# Reference data an agent can pull into context without spending a tool call.
# All of it is static — no API request, no credentials — so reading a resource
# can never fail on auth or cost a round trip.


@mcp.resource("gplay://specs/store-images")
def store_image_specs_resource() -> str:
    """Play's rules for every store-listing image type."""
    return json.dumps(list_image_specs(), indent=2)


@mcp.resource("gplay://specs/report-families")
def report_families_resource() -> str:
    """The Cloud Storage bulk-report families, including discontinued ones."""
    return json.dumps([family.model_dump() for family in list_families()], indent=2)


@mcp.resource("gplay://specs/vitals-metric-sets")
def vitals_metric_sets_resource() -> str:
    """The Play Developer Reporting metric sets and their resource names."""
    return json.dumps(
        [
            {"metric_set": key, "resource_suffix": suffix, "label": label}
            for key, (suffix, _accessor, label) in sorted(METRIC_SETS.items())
        ],
        indent=2,
    )


@mcp.resource("gplay://config")
def config_resource() -> str:
    """What this server has configured, without revealing any of it.

    Reports whether each plane can work, not the values — a resource is read
    into the model's context, so a bucket id or key path does not belong here.
    """
    return json.dumps(
        {
            "read_only": READ_ONLY,
            "planes": {
                "publisher_api": {
                    "configured": bool(
                        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                        or os.environ.get("GOOGLE_PLAY_STORE_CREDENTIALS")
                    ),
                    "needs": "GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_PLAY_STORE_CREDENTIALS",
                },
                "developer_reporting": {
                    "configured": bool(
                        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
                        or os.environ.get("GOOGLE_PLAY_STORE_CREDENTIALS")
                    ),
                    "needs": (
                        "the same key, plus the playdeveloperreporting scope granted in "
                        "Play Console — it is granted separately from publishing"
                    ),
                },
                "bulk_reports": {
                    "configured": bool(os.environ.get("GOOGLE_PLAY_MCP_REPORTS_BUCKET")),
                    "needs": (
                        "GOOGLE_PLAY_MCP_REPORTS_BUCKET, plus the ACCOUNT-level permission "
                        "'View app information and download bulk reports'"
                    ),
                },
                "store_scraping": {
                    "configured": True,
                    "needs": "nothing; GOOGLE_PLAY_MCP_SCRAPE_PROXIES is recommended at scale",
                    "proxies_configured": bool(os.environ.get("GOOGLE_PLAY_MCP_SCRAPE_PROXIES")),
                },
                "image_preflight": {"configured": True, "needs": "nothing"},
            },
            "write_safety": {
                "gate": "two-step confirmation on 16 high-blast-radius tools",
                "audit_log": str(audit_log_path()),
                "max_initial_production_rollout": PRODUCTION_POLICY.max_initial_fraction,
            },
        },
        indent=2,
    )


# =============================================================================
# Prompts
# =============================================================================


@mcp.prompt()
def publish_screenshots(package_name: str, language: str = "en-US") -> str:
    """Walk the generate -> preflight -> upload path for store screenshots.

    Google Play Console MCP does not render screenshots; it validates and uploads them. This
    prompt names the handoff so an agent does not have to infer it, including
    the alpha-channel rule that makes an otherwise clean render fail at upload.
    """
    return f"""Publish store screenshots for {package_name} in {language}.

Google Play Console MCP does not generate screenshots. Render them with whatever tool you
have — if an `appscreens` MCP server is connected, `export_images(render=true)`
produces them and returns download URLs; otherwise use local files.

Then, in this order:

1. `list_store_image_specs` — read the rules before rendering if you can still
   influence the export settings. The one that bites: screenshots and the
   feature graphic must have NO alpha channel, while the icon MUST have one. An
   exporter set to plain "PNG" usually emits RGBA, which is correct for the icon
   and rejected for everything else.

2. `preflight_screenshot_set(image_paths=[...], image_type="phoneScreenshots")`
   — checks every file AND the count. Play requires at least TWO screenshots per
   type per locale and caps at eight; a set of one is valid file-by-file and
   still blocks the release.

3. Fix anything it reports and re-render. Do not upload a set that failed
   preflight — the API rejects it after the transfer, not before.

4. `upload_image(package_name="{package_name}", language="{language}",
   image_type=..., image_path=...)` for each file, once preflight is clean.

5. `list_images` to confirm what is now attached to the listing.

Ask before deleting anything. `delete_all_images` removes every image of a type
for a locale in one commit and the API has no undo.
"""


@mcp.prompt()
def release_health_check(package_name: str) -> str:
    """Decide whether a release that just shipped is healthy enough to widen."""
    return f"""Assess the health of the current release of {package_name}.

1. `get_vitals_freshness(package_name="{package_name}")` FIRST. Play vitals lag
   reality by hours to days. A 7-day window that quietly returns 5 days reads as
   an improvement rather than a gap, so establish what the data can actually
   support before drawing any conclusion.

2. `query_vitals` for `crash_rate` and `anr_rate`, sliced by `versionCode`, so
   the new build is compared against the previous one rather than against a
   blended average.

3. `search_error_issues(order_by="errorReportCount desc")` if either rate moved,
   then `search_error_reports` on the worst issue for the stack trace.

4. `get_releases` to see the current rollout fraction.

Then report, and recommend one of:

- **halt** — `halt_release` acts immediately and needs no confirmation, by
  design: stopping a bad rollout is the one operation that reduces blast radius.
- **hold** — leave the fraction where it is and re-check once the data is fresher.
- **widen** — `update_rollout`. This is a two-step confirmed write: it will
  return a preview first, which you must show the user before confirming.

State which vitals were stale or missing. Do not describe a rollout as healthy
on the strength of data that has not landed yet.
"""


# =============================================================================
# HTTP Endpoints for Streamable Transport
# =============================================================================


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:  # noqa: ARG001
    """Health check endpoint for monitoring and load balancers."""
    return JSONResponse({"status": "healthy", "service": "google-play-console-mcp"})


def _authorize_credentials_request(request: Request) -> JSONResponse | None:
    """Authorize a POST to the /credentials management endpoint.

    If GOOGLE_PLAY_MCP_ADMIN_TOKEN is set, an ``Authorization: Bearer <token>``
    header matching it (constant-time comparison) is required, and the request
    is accepted from any host. This is the correct mode behind a reverse proxy,
    where ``request.client.host`` is the proxy address and cannot be trusted as
    a "localhost" signal.

    If no admin token is configured, the endpoint accepts loopback (localhost)
    peers only — the historical behavior.

    Returns an error ``JSONResponse`` if the request is not authorized, else None.
    """
    admin_token = os.environ.get("GOOGLE_PLAY_MCP_ADMIN_TOKEN")
    if admin_token:
        provided = request.headers.get("authorization", "")
        expected = f"Bearer {admin_token}"
        # Compare as bytes: secrets.compare_digest raises TypeError on non-ASCII
        # str operands, and Starlette decodes header values as latin-1, so a
        # crafted header could otherwise turn a 401 into an uncaught 500.
        if not (
            provided and secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
        ):
            return JSONResponse(
                {"success": False, "error": "Missing or invalid admin token"},
                status_code=401,
            )
        return None

    # No admin token configured: only allow loopback peers.
    client_host = request.client.host if request.client else None
    try:
        is_loopback = client_host is not None and ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        return JSONResponse(
            {
                "success": False,
                "error": (
                    "This endpoint is only accessible from localhost. Set "
                    "GOOGLE_PLAY_MCP_ADMIN_TOKEN and send an 'Authorization: Bearer' header "
                    "to allow authenticated access (required when running behind a proxy, "
                    "where the peer address is the proxy and not the real client)."
                ),
            },
            status_code=403,
        )
    return None


@mcp.custom_route("/credentials", methods=["POST"])
async def update_credentials(request: Request) -> JSONResponse:
    """Update Google Play Store credentials via HTTP POST.

    Management endpoint - restricted to localhost only.

    This endpoint allows local clients to provide credentials when using
    streamable-http transport. Accepts JSON credentials in the request body.

    Request body should be one of:
    - {"credentials": {...}} - Service account JSON object
    - {"credentials": "..."} - Service account JSON string
    - {"credentials_base64": "..."} - Base64-encoded service account JSON

    Returns:
        JSON response with success status
    """
    # Management endpoint: authorize by admin token (if configured) or localhost.
    auth_error = _authorize_credentials_request(request)
    if auth_error is not None:
        return auth_error

    new_client: PlayStoreClient | None = None
    try:
        body = await request.json()

        credentials = body.get("credentials")
        credentials_base64 = body.get("credentials_base64")

        if not credentials and not credentials_base64:
            return JSONResponse(
                {
                    "success": False,
                    "error": "Missing 'credentials' or 'credentials_base64' in request body",
                },
                status_code=400,
            )

        # Create new client with provided credentials
        if credentials_base64:
            # Decode base64 credentials
            try:
                decoded = base64.b64decode(credentials_base64).decode("utf-8")
                credentials_dict = json.loads(decoded)
                new_client = PlayStoreClient(credentials_json=credentials_dict)
            except (binascii.Error, UnicodeDecodeError) as e:
                return JSONResponse(
                    {"success": False, "error": f"Invalid base64 encoding: {e}"},
                    status_code=400,
                )
            except json.JSONDecodeError:
                return JSONResponse(
                    {"success": False, "error": "Invalid JSON in base64-decoded credentials"},
                    status_code=400,
                )
        elif credentials:
            if isinstance(credentials, str):
                # Validate it's valid JSON
                try:
                    json.loads(credentials)
                except json.JSONDecodeError:
                    return JSONResponse(
                        {"success": False, "error": "Invalid JSON in credentials string"},
                        status_code=400,
                    )
                new_client = PlayStoreClient(credentials_json=credentials)
            elif isinstance(credentials, dict):
                new_client = PlayStoreClient(credentials_json=credentials)
            else:
                return JSONResponse(
                    {"success": False, "error": "credentials must be a string or object"},
                    status_code=400,
                )

        if (
            new_client is None
        ):  # pragma: no cover - defensive; branches above always assign or return
            return JSONResponse(
                {"success": False, "error": "No credentials could be parsed from the request"},
                status_code=400,
            )

        # Validate credentials by attempting to build the service. This does
        # blocking network I/O, so run it off the event loop.
        try:
            await asyncio.to_thread(new_client._get_service)
        except PlayStoreClientError:
            logger.warning("Credential validation failed for /credentials request")
            return JSONResponse(
                {"success": False, "error": "Invalid credentials"},
                status_code=401,
            )

        # Update the client in the shared state
        _shared_state["client"] = new_client
        _shared_state["credentials_updated"] = True

        logger.info("Credentials updated successfully via HTTP endpoint")

        return JSONResponse(
            {"success": True, "message": "Credentials updated successfully"},
            status_code=200,
        )

    except json.JSONDecodeError:
        return JSONResponse(
            {"success": False, "error": "Invalid JSON in request body"},
            status_code=400,
        )
    except Exception:
        logger.exception("Error updating credentials")
        return JSONResponse(
            {"success": False, "error": "Internal server error"},
            status_code=500,
        )


# =============================================================================
# Entry Point
# =============================================================================


def _dns_rebinding_disabled() -> bool:
    """Return True when DNS-rebinding (Host-header) protection is disabled."""
    return bool(os.environ.get("GOOGLE_PLAY_MCP_DISABLE_DNS_REBINDING"))


def _is_wildcard_bind(host: str) -> bool:
    """Return True if host binds all interfaces (unspecified address) or is unset."""
    if not host:
        return True
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


def _run_http(transport: str, host: str, port: int) -> None:
    """Serve a network transport with DNS-rebinding (Host-header) protection.

    fastmcp v3 has no constructor-level transport security; we attach Starlette
    TrustedHostMiddleware (which is exactly Host-header validation) unless
    GOOGLE_PLAY_MCP_DISABLE_DNS_REBINDING is set.

    Downloads are always confined to a base directory by the client (defaulting
    to the working directory when GOOGLE_PLAY_MCP_DOWNLOAD_DIR is unset), so a
    network transport is safe to start either way. When the variable is unset we
    only warn — a network-exposed deployment should point it at a writable
    directory to control where APK/AAB downloads land, rather than defaulting to
    the process working directory (which may be read-only on some hosts).
    """
    if not os.environ.get("GOOGLE_PLAY_MCP_DOWNLOAD_DIR"):
        logger.warning(
            "GOOGLE_PLAY_MCP_DOWNLOAD_DIR is not set; APK/AAB downloads will be confined to "
            "the server's working directory. Set it to a writable directory to control where "
            "downloads are written on a network-exposed deployment.",
            transport=transport,
        )
    middleware: list[Middleware] = []
    if not _dns_rebinding_disabled():
        allowed = ["localhost", "127.0.0.1", "[::1]"]
        if _is_wildcard_bind(host):
            # Wildcard bind: the reachable Host header is unknown, so protection
            # stays localhost-only. Remote deployments should terminate at a
            # reverse proxy and set GOOGLE_PLAY_MCP_DISABLE_DNS_REBINDING.
            logger.warning(
                "DNS-rebinding protection allows only localhost on a wildcard bind; "
                "set GOOGLE_PLAY_MCP_DISABLE_DNS_REBINDING=1 for remote access behind a proxy",
                host=host,
            )
        else:
            allowed.append(host)
        middleware.append(Middleware(TrustedHostMiddleware, allowed_hosts=allowed))
    # transport is constrained to the non-stdio argparse choices ("sse" /
    # "streamable-http"), both valid http_app transports; argparse types it as str.
    app = mcp.http_app(transport=transport, middleware=middleware)  # type: ignore[arg-type]
    uvicorn.run(app, host=host, port=port)


def main(argv: list[str] | None = None) -> None:
    """Run the Google Play Console MCP Server."""
    args_list = sys.argv[1:] if argv is None else list(argv)

    # `install` is a subcommand, but the bare invocation must stay exactly what
    # it was: MCP clients launch this executable with no arguments and speak
    # stdio to it, so the server can never sit behind a required subcommand.
    if args_list and args_list[0] == "install":
        raise SystemExit(run_install_cli(args_list[1:]))

    parser = argparse.ArgumentParser(description="Google Play Console MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="Transport protocol (default: stdio, or set MCP_TRANSPORT env var)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", "127.0.0.1"),
        help="Host to bind to for network transports (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", "8000")),
        help="Port to bind to for network transports (default: 8000)",
    )
    parser.add_argument(
        "--credentials",
        default=os.environ.get("GOOGLE_PLAY_STORE_CREDENTIALS"),
        help="Path to service account JSON key or JSON content (default: GOOGLE_PLAY_STORE_CREDENTIALS env var)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=_env_read_only(),
        help="Disable all write operations (or set GOOGLE_PLAY_MCP_READ_ONLY=1)",
    )
    args = parser.parse_args(args_list)

    if args.credentials:
        os.environ["GOOGLE_PLAY_STORE_CREDENTIALS"] = args.credentials

    set_read_only(args.read_only)

    logger.info(
        "Starting Google Play Console MCP Server",
        transport=args.transport,
        host=args.host if args.transport != "stdio" else None,
        port=args.port if args.transport != "stdio" else None,
        read_only=READ_ONLY,
    )

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        _run_http(args.transport, args.host, args.port)


if __name__ == "__main__":
    main()
