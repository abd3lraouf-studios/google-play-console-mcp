"""Does the bundled API description still match the live one?

Marked ``network`` and deselected by default, so it never runs offline and
never counts toward coverage. It exists to answer the one question the rest of
the suite cannot: whether Google has shipped surface that the pinned
``google-api-python-client`` — and therefore this server — cannot see.

Three axes matter, for different reasons:

* **methods** — new ones are unreachable, including through
  ``android_publisher_invoke``, because ``build()`` reads the bundled document.
* **schema properties** — a field added since the pin would be *rejected* by
  body validation as an unknown field. This is the axis that can break a caller.
* **enum values** — only a warning today, but the same drift.

The fix for all three is the same: bump ``google-api-python-client``.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from google_play_console_mcp.discovery import PUBLISHER_API, REPORTING_API, load_spec

pytestmark = pytest.mark.network

_DISCOVERY_URL = "https://{api}.googleapis.com/$discovery/rest?version={version}"


def _live(api: str, version: str) -> dict:
    url = _DISCOVERY_URL.format(api=api, version=version)
    with urllib.request.urlopen(url) as response:  # noqa: S310 - a fixed https URL
        return dict(json.load(response))


def _method_ids(document: dict) -> set[str]:
    found: set[str] = set()

    def walk(node: dict) -> None:
        for method in node.get("methods", {}).values():
            found.add(method["id"])
        for child in node.get("resources", {}).values():
            walk(child)

    for root in document.get("resources", {}).values():
        walk(root)
    return found


def _properties(document: dict, *, only: set[str] | None = None) -> set[str]:
    return {
        f"{name}.{prop}"
        for name, schema in document.get("schemas", {}).items()
        if only is None or name in only
        for prop in (schema.get("properties") or {})
    }


def _enum_values(document: dict) -> set[str]:
    return {
        f"{name}.{prop}={value}"
        for name, schema in document.get("schemas", {}).items()
        for prop, declared in (schema.get("properties") or {}).items()
        for value in (declared.get("enum") or [])
    }


@pytest.mark.parametrize("api", [PUBLISHER_API, REPORTING_API], ids=lambda a: a[0])
def test_bundled_document_still_matches_live(api):
    name, version = api
    spec = load_spec(name, version)
    assert spec is not None
    live = _live(name, version)
    bundled = json.loads(
        __import__("googleapiclient.discovery_cache", fromlist=["x"]).get_static_doc(name, version)
    )

    # Only schemas the bundled document already has can break a caller: a field
    # added to one of those is a field body validation would reject as unknown.
    # A wholly new schema belongs to wholly new surface, which is method drift,
    # not a false-rejection risk.
    known = set(bundled.get("schemas", {}))
    new_properties = sorted(_properties(live, only=known) - _properties(bundled))
    new_methods = sorted(_method_ids(live) - _method_ids(bundled))
    new_enums = sorted(
        e for e in _enum_values(live) - _enum_values(bundled) if e.split(".")[0] in known
    )

    assert not new_properties, (
        f"{name} gained fields on EXISTING schemas since revision {spec.revision}. Body "
        f"validation would reject these as unknown fields, so this can break a caller. "
        f"Bump google-api-python-client. New: {new_properties[:20]}"
    )
    assert not new_enums, (
        f"{name} gained enum values on existing schemas since revision {spec.revision}; "
        f"they currently produce a spurious warning. New: {new_enums[:20]}"
    )
    assert not new_methods, (
        f"{name} gained methods since revision {spec.revision}. They are unreachable, "
        f"including through android_publisher_invoke, until google-api-python-client is "
        f"bumped. New: {new_methods}"
    )
