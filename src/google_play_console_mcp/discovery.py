"""The API description this server is actually built from.

``googleapiclient`` ships the Android Publisher and Play Developer Reporting
discovery documents inside the wheel, and :func:`googleapiclient.discovery.build`
reads them rather than fetching anything: ``static_discovery`` defaults to true
whenever no ``discoveryServiceUrl`` is given, which is how ``client.py`` calls it.
(``cache_discovery=False`` disables the *fetched-document* cache, not this.)

So this module deliberately does **not** vendor a copy. A vendored document is a
second source of truth that is allowed to disagree with the one the client is
built from, and validating against a description of the API that production code
does not follow is worse than not validating at all. Reading the same bundled
file means "what the validator believes" and "what the client believes" are the
same bytes, and refreshing them is exactly one action: bump
``google-api-python-client``.

The cost is honest and already paid: the bundled document lags the live one by up
to a few weeks. That lag is not introduced here — a method Google shipped last
week is not callable through ``build()`` either way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from typing import Any

from googleapiclient.discovery_cache import get_static_doc

#: The two discovery-backed planes. The bulk-reports (Cloud Storage) and public
#: store-scraping planes have no discovery document and are not described here.
PUBLISHER_API = ("androidpublisher", "v3")
REPORTING_API = ("playdeveloperreporting", "v1beta1")


@dataclass(frozen=True)
class MethodSpec:
    """One callable API method, keyed the way a built request identifies itself."""

    method_id: str
    http_method: str
    path: str
    #: Name of the schema this method's request body must match, if it takes one.
    request_schema: str | None

    @property
    def takes_body(self) -> bool:
        return self.request_schema is not None


@dataclass(frozen=True)
class ApiSpec:
    """A parsed discovery document, indexed for the two questions we ask of it."""

    name: str
    version: str
    revision: str
    methods: dict[str, MethodSpec]
    schemas: dict[str, Any]
    roots: frozenset[str]

    def method(self, method_id: str) -> MethodSpec | None:
        """The method with this id, or None if the document does not describe it."""
        return self.methods.get(method_id)

    def schema(self, name: str) -> dict[str, Any] | None:
        """The named schema, or None for a reference the document does not resolve."""
        found = self.schemas.get(name)
        return found if isinstance(found, dict) else None

    @property
    def method_ids(self) -> frozenset[str]:
        return frozenset(self.methods)


def _index_methods(node: dict[str, Any], into: dict[str, MethodSpec]) -> None:
    """Flatten a discovery resource tree into ``method_id -> MethodSpec``."""
    for method in node.get("methods", {}).values():
        request = method.get("request") or {}
        spec = MethodSpec(
            method_id=method["id"],
            http_method=method.get("httpMethod", ""),
            path=method.get("path", ""),
            request_schema=request.get("$ref"),
        )
        into[spec.method_id] = spec
    for child in node.get("resources", {}).values():
        _index_methods(child, into)


@cache
def load_spec(name: str, version: str) -> ApiSpec | None:
    """The bundled description of one API, parsed once per process.

    Returns None when ``googleapiclient`` does not ship the document — a
    dependency that moved or renamed its data files must degrade this server to
    "no local validation", never break publishing.
    """
    raw = get_static_doc(name, version)
    if not raw:
        return None
    document = json.loads(raw)
    methods: dict[str, MethodSpec] = {}
    for root in document.get("resources", {}).values():
        _index_methods(root, methods)
    return ApiSpec(
        name=name,
        version=version,
        revision=str(document.get("revision", "")),
        methods=methods,
        schemas=document.get("schemas", {}),
        roots=frozenset(document.get("resources", {})),
    )


def publisher_spec() -> ApiSpec | None:
    """The Android Publisher v3 description ``client.py`` builds its service from."""
    return load_spec(*PUBLISHER_API)


def reporting_spec() -> ApiSpec | None:
    """The Play Developer Reporting v1beta1 description ``reporting.py`` uses."""
    return load_spec(*REPORTING_API)


def spec_for_method(method_id: str) -> ApiSpec | None:
    """The spec that describes ``method_id``, chosen by its API prefix.

    A method id is fully qualified (``androidpublisher.applications.dataSafety``),
    so the plane it belongs to is readable straight off the front of it.
    """
    api = method_id.split(".", 1)[0]
    if api == PUBLISHER_API[0]:
        return publisher_spec()
    if api == REPORTING_API[0]:
        return reporting_spec()
    return None
