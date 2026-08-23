"""Check a request body against the API description before it is sent.

Thirty-eight tools take an opaque ``dict`` and hand it straight to the API, so a
misspelled field used to travel all the way to Google and come back as a 400 that
does not say which field was wrong. This checks it locally, against the same
bundled discovery document the client is built from (see :mod:`.discovery`).

**Why not ``jsonschema``.** Discovery is a JSON-Schema-*flavoured* dialect, not
JSON Schema, and the difference is exactly where the value is. It carries no
``required`` and no ``additionalProperties: false`` anywhere in its 383 schemas,
so a real validator reports ``{"safteyLables": "typo"}`` and ``{}`` as *valid*
against ``SafetyLabelsUpdateRequest`` — the two checks worth making are the two
it structurally cannot make. It also spells ``format: int64`` as ``type: string``
(proto3 JSON, where both a string and an int are accepted), which JSON Schema has
no way to express. So the walk below is written directly.

**Strictness is the design.** A false rejection of a valid body is worse than a
bad body reaching Google, because Google's own error is at least authoritative.
So only the checks that cannot drift are errors: an unknown field name and a
wrong JSON type. Enum values are the most-churned part of discovery and Google
returns a clean, specific 400 for a bad one, so those are warnings. Requiredness
is described in prose only, and the obvious heuristic is wrong by construction
for every ``patch_*`` with an ``updateMask`` and every ``batch*`` wrapper, so it
is a warning too. ``GOOGLE_PLAY_MCP_BODY_VALIDATION`` moves the whole layer.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

from .discovery import ApiSpec, spec_for_method
from .errors import ValidationError

Severity = Literal["error", "warning"]

#: Env switch. ``on`` is the table in the module docstring; ``strict`` promotes
#: enum and prose-required to errors; ``warn`` blocks nothing; ``off`` skips.
MODE_ENV = "GOOGLE_PLAY_MCP_BODY_VALIDATION"
_MODES = ("strict", "on", "warn", "off")

#: Deep enough for the deepest real Play resource, shallow enough that a
#: pathological body cannot spend the process's stack.
_MAX_DEPTH = 32
#: A wildly wrong body should not produce a 500-line error message.
_MAX_FINDINGS = 20

#: int64/uint64 ride the wire as strings, but proto3 JSON accepts a number too,
#: so rejecting either one would be a false positive on a hand-written body.
_INT_STRING_FORMATS = frozenset({"int64", "uint64"})


@dataclass(frozen=True)
class Finding:
    """One problem with a body, located by the path a caller can act on."""

    path: str
    message: str
    severity: Severity


def _mode() -> str:
    raw = os.environ.get(MODE_ENV, "on").strip().lower()
    return raw if raw in _MODES else "on"


def _did_you_mean(name: str, candidates: Any) -> str:
    near = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return f" Did you mean {near[0]!r}?" if near else ""


def _is_int(value: Any) -> bool:
    # bool subclasses int in Python, so it must be excluded explicitly or `True`
    # would satisfy every integer field.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int64(value: Any) -> bool:
    # int64 rides the wire as a string, but proto3 JSON accepts a number too, so
    # rejecting either spelling would be a false positive on a hand-written body.
    return isinstance(value, str) or _is_int(value)


#: type -> (predicate, what to say when it fails). One entry per JSON type
#: discovery actually uses; anything else is passed through unchecked.
_TYPE_CHECKS: dict[str, tuple[Callable[[Any], bool], str]] = {
    "string": (lambda v: isinstance(v, str), "expected a string"),
    "integer": (_is_int, "expected an integer"),
    "number": (_is_number, "expected a number"),
    "boolean": (lambda v: isinstance(v, bool), "expected a boolean"),
    "array": (lambda v: isinstance(v, list), "expected an array"),
    "object": (lambda v: isinstance(v, dict), "expected an object"),
}


def _type_error(value: Any, schema: dict[str, Any]) -> str | None:
    """The message for a value whose JSON type cannot match, or None if it can."""
    declared = schema.get("type")
    if declared == "string" and schema.get("format") in _INT_STRING_FORMATS:
        return None if _is_int64(value) else "expected an int64 (a string or a number)"
    check = _TYPE_CHECKS.get(str(declared))
    if check is None:
        return None
    predicate, message = check
    return None if predicate(value) else message


class _Walker:
    """Collects findings while walking the body, using the schema as a lookup.

    The recursion is over the *data*, which is finite, so it terminates on its
    own; ``_MAX_DEPTH`` is a guard against a hostile body, not a base case.
    """

    def __init__(self, spec: ApiSpec, root_required: bool) -> None:
        self._spec = spec
        self._root_required = root_required
        self.findings: list[Finding] = []

    def _add(self, path: str, message: str, severity: Severity) -> None:
        if len(self.findings) < _MAX_FINDINGS:
            self.findings.append(Finding(path=path, message=message, severity=severity))

    def walk(self, value: Any, schema: dict[str, Any], path: str, depth: int) -> None:
        if depth > _MAX_DEPTH or value is None:
            # A null is how JSON spells "absent"; there is nothing to check.
            return
        ref = schema.get("$ref")
        if ref is not None:
            resolved = self._spec.schema(ref)
            if resolved is None:
                # A document that disagrees with itself must not block a write.
                return
            self.walk(value, resolved, path, depth)
            return
        problem = _type_error(value, schema)
        if problem is not None:
            self._add(path, f"{problem}, got {type(value).__name__}", "error")
            return
        if isinstance(value, dict):
            self._walk_object(value, schema, path, depth)
        elif isinstance(value, list):
            items = schema.get("items")
            if isinstance(items, dict):
                for index, element in enumerate(value):
                    self.walk(element, items, f"{path}[{index}]", depth + 1)
        else:
            self._check_enum(value, schema, path)

    def _walk_object(
        self, value: dict[str, Any], schema: dict[str, Any], path: str, depth: int
    ) -> None:
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            # A map: the keys are data, so only the values are checked.
            for key, element in value.items():
                self.walk(element, extra, f"{path}.{key}", depth + 1)
            return
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return
        for key, element in value.items():
            declared = properties.get(key)
            if declared is None:
                self._add(
                    f"{path}.{key}",
                    f"is not a field of this request.{_did_you_mean(key, properties)}",
                    "error",
                )
                continue
            self._note_field_status(declared, f"{path}.{key}")
            self.walk(element, declared, f"{path}.{key}", depth + 1)
        if self._root_required and depth == 0:
            self._check_prose_required(value, properties, path)

    def _note_field_status(self, declared: dict[str, Any], path: str) -> None:
        if declared.get("readOnly"):
            self._add(path, "is read-only; Play ignores it in a request.", "warning")
        if declared.get("deprecated"):
            self._add(path, "is deprecated.", "warning")

    def _check_prose_required(
        self, value: dict[str, Any], properties: dict[str, Any], path: str
    ) -> None:
        for key, declared in properties.items():
            if key in value:
                continue
            description = declared.get("description", "")
            if isinstance(description, str) and description.startswith("Required."):
                self._add(
                    f"{path}.{key}",
                    "is documented as Required but is absent.",
                    "warning",
                )

    def _check_enum(self, value: Any, schema: dict[str, Any], path: str) -> None:
        allowed = schema.get("enum")
        if isinstance(allowed, list) and isinstance(value, str) and value not in allowed:
            shown = ", ".join(repr(a) for a in allowed[:6])
            more = f", … {len(allowed) - 6} more" if len(allowed) > 6 else ""
            self._add(path, f"{value!r} is not one of {shown}{more}.", "warning")


def validate_request_body(method_id: str, body: Any) -> list[Finding]:
    """Everything wrong with ``body`` as a request to ``method_id``.

    Returns an empty list when the method is not described, so an API this
    server has not been taught about is passed through rather than refused.
    """
    spec = spec_for_method(method_id)
    if spec is None:
        return []
    method = spec.method(method_id)
    if method is None:
        return []
    if method.request_schema is None:
        return [Finding("body", "this method does not take a request body.", "error")]
    schema = spec.schema(method.request_schema)
    if schema is None:
        return []
    if not isinstance(body, dict):
        return [Finding("body", f"expected an object, got {type(body).__name__}", "error")]
    walker = _Walker(spec, root_required=_mode() != "warn")
    walker.walk(body, schema, "body", 0)
    return walker.findings


def _blocking(findings: list[Finding], mode: str) -> list[Finding]:
    if mode == "warn":
        return []
    if mode == "strict":
        return list(findings)
    return [f for f in findings if f.severity == "error"]


def enforce(method_id: str, body: Any) -> list[Finding]:
    """Raise if ``body`` is wrong in a way worth refusing; return the warnings.

    The remedy names the escape hatch, because the one failure mode that matters
    here is refusing a body that is actually fine — a field Google added after
    the bundled discovery document was cut.
    """
    mode = _mode()
    if mode == "off":
        return []
    findings = validate_request_body(method_id, body)
    if not findings:
        return []
    blocking = _blocking(findings, mode)
    if not blocking:
        return findings
    spec = spec_for_method(method_id)
    revision = spec.revision if spec else "unknown"
    detail = "; ".join(f"{f.path} {f.message}" for f in blocking)
    raise ValidationError(
        f"Request body for {method_id} has {len(blocking)} problem(s): {detail}",
        remedy=(
            "Fix the field(s) named above. If Google added this field after discovery "
            f"revision {revision}, upgrade google-api-python-client, or set "
            f"{MODE_ENV}=warn to pass the body through unchecked."
        ),
        details={
            "method_id": method_id,
            "discovery_revision": revision,
            "findings": [
                {"path": f.path, "message": f.message, "severity": f.severity} for f in blocking
            ],
        },
    )
