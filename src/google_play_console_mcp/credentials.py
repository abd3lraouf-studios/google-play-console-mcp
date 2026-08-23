"""Service-account credential resolution, shared across API surfaces.

Google Play Console MCP talks to more than one Google API, and each wants credentials scoped
differently: the Android Publisher API needs ``androidpublisher``, the Play
Developer Reporting API needs ``playdeveloperreporting``, and the Cloud Storage
bulk reports need ``devstorage.read_only``. The resolution order is identical in
every case, so it lives here rather than being repeated per client.

Resolution order, highest precedence first:

1. ``credentials_json`` as an inline JSON object (string or dict)
2. ``credentials_json`` as a path to a key file
3. ``credentials_path`` as a path to a key file
4. a service-account key held in the OS keychain
5. Application Default Credentials, when ``GOOGLE_PLAY_MCP_USE_ADC`` is set

Explicit arguments win, so nothing that already worked changes behaviour: the
two new sources are only consulted once the caller has supplied nothing, which
previously returned ``None``.

Step 4 keeps the key out of both the filesystem and the agent config files that
would otherwise carry a path to it — see :mod:`google_play_console_mcp.keychain`.

Step 5 is what makes user OAuth work without this project implementing any of
it. ``google.auth.default()`` already resolves, in its own order, the
``GOOGLE_APPLICATION_CREDENTIALS`` file, the user credentials written by
``gcloud auth application-default login``, and the metadata server on GCE,
Cloud Run and GKE — refreshing tokens as it goes. There is no client secret to
ship, no redirect listener to run and no token file to look after here.

The one thing user OAuth needs is the right scopes, and they are fixed when the
login happens rather than when the token is used: unlike a service account, a
user credential arrives pre-scoped and ``google.auth.default(scopes=...)``
cannot widen it. So the login has to name them::

    gcloud auth application-default login \
      --scopes=https://www.googleapis.com/auth/androidpublisher,\
    https://www.googleapis.com/auth/playdeveloperreporting,\
    https://www.googleapis.com/auth/devstorage.read_only

A caller that supplies nothing and has none of the above gets ``None`` and is
expected to raise its own error naming the sources it reads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import google.auth
import structlog
from google.auth.credentials import Credentials
from google.auth.exceptions import DefaultCredentialsError
from google.oauth2 import service_account

from google_play_console_mcp import keychain

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = structlog.get_logger(__name__)

# Google Play Developer API (androidpublisher v3).
PUBLISHER_SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]

# Play Developer Reporting API (playdeveloperreporting v1beta1) — app vitals,
# error issues/reports and anomalies. A separate scope: a service account that
# works for publishing is not automatically permitted here, and Play Console
# grants the two independently.
REPORTING_SCOPES = ["https://www.googleapis.com/auth/playdeveloperreporting"]

# Cloud Storage, read-only — for the private bulk-reports bucket that holds
# installs, ratings, earnings and search terms. A third distinct scope: the Play
# Console permission behind it ("View app information and download bulk
# reports") is account-level, so an app-level grant does not reach the bucket.
STORAGE_SCOPES = ["https://www.googleapis.com/auth/devstorage.read_only"]

# Opt-in switch for Application Default Credentials. See _from_application_default.
USE_ADC_ENV = "GOOGLE_PLAY_MCP_USE_ADC"

# All three scopes, for the `gcloud auth application-default login` line that a
# user OAuth setup needs. Order is stable so the printed command is stable.
ALL_SCOPES = [*PUBLISHER_SCOPES, *REPORTING_SCOPES, *STORAGE_SCOPES]


def load_service_account_credentials(
    credentials_json: str | dict[str, Any] | None,
    credentials_path: str | None,
    scopes: Sequence[str],
) -> Credentials | None:
    """Resolve service-account credentials for the given scopes.

    Args:
        credentials_json: Inline service-account JSON (string or dict), or a
            path to a key file.
        credentials_path: Path to a service-account key file.
        scopes: OAuth scopes to request.

    Returns:
        Scoped credentials, or None when no source resolved.
    """
    scope_list = list(scopes)

    if credentials_json:
        if isinstance(credentials_json, str):
            try:
                # Either inline JSON or a path to a key file.
                if credentials_json.strip().startswith("{"):
                    creds_info = json.loads(credentials_json)
                    return cast(
                        "service_account.Credentials",
                        service_account.Credentials.from_service_account_info(
                            creds_info, scopes=scope_list
                        ),
                    )
                if Path(credentials_json).exists():
                    return cast(
                        "service_account.Credentials",
                        service_account.Credentials.from_service_account_file(
                            credentials_json, scopes=scope_list
                        ),
                    )
            except json.JSONDecodeError:
                # Not JSON, and not an existing path either.
                logger.warning(
                    "credentials_json string is not valid JSON and not a valid file path",
                )
        elif isinstance(credentials_json, dict):
            return cast(
                "service_account.Credentials",
                service_account.Credentials.from_service_account_info(
                    credentials_json, scopes=scope_list
                ),
            )

    if credentials_path:
        creds_path = Path(credentials_path)
        if creds_path.exists():
            return cast(
                "service_account.Credentials",
                service_account.Credentials.from_service_account_file(
                    str(creds_path), scopes=scope_list
                ),
            )

    stored = _from_keychain(scope_list)
    if stored is not None:
        return stored

    return _from_application_default(scope_list)


def _from_keychain(scope_list: list[str]) -> service_account.Credentials | None:
    """Load a service-account key held in the OS keychain, if there is one."""
    raw = keychain.load()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Keychain entry is not valid JSON; ignoring it")
        return None
    try:
        return cast(
            "service_account.Credentials",
            service_account.Credentials.from_service_account_info(info, scopes=scope_list),
        )
    except ValueError:
        # Present but not a service-account key — a wrong paste, most likely.
        logger.warning("Keychain entry is not a service-account key; ignoring it")
        return None


def adc_enabled() -> bool:
    """Whether Application Default Credentials may be used."""
    return os.environ.get(USE_ADC_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _from_application_default(scope_list: list[str]) -> Credentials | None:
    """Fall back to Application Default Credentials, when opted in.

    Covers ``gcloud auth application-default login`` (user OAuth) and the
    metadata server on Google infrastructure. Absence is the normal case, not an
    error: it just means the caller has to be told what to configure.

    **Opt-in on purpose.** The other sources are specific to this server — an
    argument passed to it, or a keychain entry filed under its own service name.
    ADC is not: it is ambient machine state shared with every Google tool, and
    on a developer laptop it is usually a personal account left behind by an
    unrelated ``gcloud`` login. Falling back to it silently would let this
    server publish a release, or read what an account earned, as whoever logged
    in last — with nothing in the invocation to say so. Setting
    ``GOOGLE_PLAY_MCP_USE_ADC=1`` (or passing ``--use-adc``) is that saying-so.
    """
    if not adc_enabled():
        return None
    try:
        credentials, _ = google.auth.default(scopes=scope_list)
    except DefaultCredentialsError:
        return None
    logger.info("Using Application Default Credentials", kind=type(credentials).__name__)
    return credentials
