"""A test double that knows which API calls actually exist.

The suite's default double is a bare ``MagicMock``, which accepts any attribute
and any keyword. That makes it silent about the one class of bug it is best
placed to catch: ``service.applications().dataSafetyLabels(package_name=...)`` —
a method *and* a parameter that do not exist — passes every assertion in every
test in this repo.

``verified()`` wraps the caller's mock rather than replacing it. Attribute
lookups are resolved against the real discovery document first, then forwarded,
so the mock still records the call and every existing
``service.applications.return_value.dataSafety.assert_called_once_with(...)``
keeps working untouched. The only change a test module needs is one line in its
``_client`` helper.

Method calls are checked by building the *real* request against an offline
service rather than by reimplementing googleapiclient's parameter rules (dotted
parameters, global parameters, repeated parameters, media upload). Letting the
library raise is both less code and more faithful.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from googleapiclient.discovery import build_from_document
from googleapiclient.discovery_cache import get_static_doc
from googleapiclient.http import HttpMockSequence

from google_play_console_mcp.discovery import PUBLISHER_API, REPORTING_API, load_spec

_APIS = {"androidpublisher": PUBLISHER_API, "playdeveloperreporting": REPORTING_API}

#: Names googleapiclient and the mock protocol own; never resolved against the
#: document, or ``copy.deepcopy`` and pytest introspection would explode.
_PASSTHROUGH_PREFIXES = ("_", "assert_", "call_", "reset_", "side_", "return_")


@cache
def _offline_service(name: str, version: str) -> Any:
    """A real service object with a canned transport, built with no credentials."""
    return build_from_document(
        get_static_doc(name, version),
        credentials=None,
        developerKey="contract-test",
        http=HttpMockSequence([({"status": "200"}, "{}")] * 64),
    )


def _resolvable(kwargs: dict[str, Any]) -> bool:
    """Whether these kwargs can be used to build a real request.

    A media upload carries a file handle and a body-less mock carries
    ``MagicMock`` values; neither survives request construction, and failing on
    them would be noise about the double rather than about the contract. Name
    and keyword checking still happens in those cases — only URI construction is
    skipped.
    """
    if "media_body" in kwargs or "media_mime_type" in kwargs:
        return False
    return all(
        isinstance(v, (str, int, float, bool, list, dict, type(None))) for v in kwargs.values()
    )


class _VerifiedResource:
    """One node of the API tree: checks names, then defers to the real mock."""

    def __init__(self, delegate: Any, node: dict[str, Any], api: tuple[str, str], path: str):
        self._delegate = delegate
        self._node = node
        self._api = api
        self._path = path

    def __getattr__(self, name: str) -> Any:
        if name.startswith(_PASSTHROUGH_PREFIXES):
            return getattr(self._delegate, name)
        methods = self._node.get("methods", {})
        resources = self._node.get("resources", {})
        if name in resources:
            return _VerifiedFactory(
                getattr(self._delegate, name), resources[name], self._api, f"{self._path}{name}."
            )
        if name in methods:
            return _VerifiedMethod(
                getattr(self._delegate, name), methods[name], self._api, f"{self._path}{name}"
            )
        available = sorted([*resources, *methods])
        raise AttributeError(
            f"{self._path}{name} is not part of {self._api[0]} {self._api[1]}. "
            f"Available here: {', '.join(available)}"
        )


class _VerifiedFactory:
    """A resource accessor — ``service.applications`` before it is called."""

    def __init__(self, delegate: Any, node: dict[str, Any], api: tuple[str, str], path: str):
        self._delegate = delegate
        self._node = node
        self._api = api
        self._path = path

    def __call__(self, *args: Any, **kwargs: Any) -> _VerifiedResource:
        return _VerifiedResource(self._delegate(*args, **kwargs), self._node, self._api, self._path)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _VerifiedMethod:
    """A callable API method — validates the call, then performs it on the mock."""

    def __init__(self, delegate: Any, spec: dict[str, Any], api: tuple[str, str], path: str):
        self._delegate = delegate
        self._spec = spec
        self._api = api
        self._path = path

    def __call__(self, **kwargs: Any) -> Any:
        declared = set(self._spec.get("parameters", {}))
        declared.update({"body", "media_body", "media_mime_type", "fields", "alt", "x__xgafv"})
        # Dotted API parameters reach Python with underscores.
        declared.update(p.replace(".", "_") for p in self._spec.get("parameters", {}))
        unknown = sorted(set(kwargs) - declared)
        if unknown:
            raise TypeError(
                f"{self._path} got unexpected keyword argument(s) {', '.join(unknown)}. "
                f"Valid parameters: {', '.join(sorted(declared - {'x__xgafv'}))}"
            )
        if _resolvable(kwargs):
            self._build_real_request(kwargs)
        return self._delegate(**kwargs)

    def _build_real_request(self, kwargs: dict[str, Any]) -> None:
        node: Any = _offline_service(*self._api)
        parts = self._path.split(".")
        for step in parts[:-1]:
            node = getattr(node, step)()
        getattr(node, parts[-1])(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def unwrap(node: Any) -> Any:
    """The mock a proxy is standing in front of, for identity assertions."""
    return node._delegate if isinstance(node, _VerifiedResource) else node


def verified(service: Any, *, api: str = "androidpublisher") -> Any:
    """Wrap a mock service so that unreal methods and parameters raise.

    Args:
        service: The ``MagicMock`` the test built.
        api: Which discovery-backed plane it stands in for.

    Returns:
        A proxy that behaves exactly like ``service`` for every real call.
    """
    name, version = _APIS[api]
    spec = load_spec(name, version)
    assert spec is not None, f"googleapiclient does not ship {name} {version}"
    root = {"resources": {r: _raw_resource(name, version, r) for r in spec.roots}}
    return _VerifiedResource(service, root, (name, version), "")


@cache
def _document(name: str, version: str) -> dict[str, Any]:
    import json

    return dict(json.loads(get_static_doc(name, version)))


def _raw_resource(name: str, version: str, root: str) -> dict[str, Any]:
    resource: dict[str, Any] = _document(name, version)["resources"][root]
    return resource
