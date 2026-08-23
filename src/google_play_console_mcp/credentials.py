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

A caller that supplies nothing gets ``None`` and is expected to raise its own
error naming the environment variables it reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog
from google.oauth2 import service_account

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


def load_service_account_credentials(
    credentials_json: str | dict[str, Any] | None,
    credentials_path: str | None,
    scopes: Sequence[str],
) -> service_account.Credentials | None:
    """Resolve service-account credentials for the given scopes.

    Args:
        credentials_json: Inline service-account JSON (string or dict), or a
            path to a key file.
        credentials_path: Path to a service-account key file.
        scopes: OAuth scopes to request.

    Returns:
        Scoped credentials, or None if nothing resolvable was supplied.
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

    return None
