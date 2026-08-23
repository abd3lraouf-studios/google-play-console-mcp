"""Every API call this server makes is one the API actually has.

The rest of the suite proves the *behaviour* of each client method against a
double. These tests prove the double is not lying: that the method names and
parameters in `client.py`, `publisher_extras.py` and `edits.py` exist in the
discovery document `client.py` builds its service from, and that the tables
which reach the API through `getattr` — where neither static analysis nor a mock
can see them — resolve too.

Deliberately separate from `tests/api_double.py`, which only sees calls the
suite happens to exercise. These see the source.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from google_play_console_mcp.discovery import (
    PUBLISHER_API,
    REPORTING_API,
    load_spec,
    publisher_spec,
    reporting_spec,
    spec_for_method,
)
from google_play_console_mcp.edits import INVOKABLE_ROOTS
from google_play_console_mcp.reporting import METRIC_SETS
from tests.api_double import unwrap, verified

_SOURCE_DIR = Path(__file__).resolve().parent.parent / "src" / "google_play_console_mcp"
#: The modules that talk to Android Publisher directly.
_API_MODULES = ("client.py", "publisher_extras.py", "edits.py")

#: Roots the escape hatch deliberately does not expose. Listing them makes the
#: exclusion a decision rather than an oversight, and makes a newly shipped root
#: fail this test instead of appearing silently.
_DELIBERATELY_NOT_INVOKABLE = frozenset({"appstoreappsreview", "appstorecatalog"})


def _service_chains(tree: ast.AST) -> list[list[str]]:
    """Every ``service.a().b().c(...)`` chain in a module, as name lists."""
    chains: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        parts: list[str] = []
        cursor: ast.expr = node.func
        while isinstance(cursor, ast.Attribute):
            parts.append(cursor.attr)
            cursor = cursor.value
            if isinstance(cursor, ast.Call):
                cursor = cursor.func
        if isinstance(cursor, ast.Name) and cursor.id == "service":
            chains.append(list(reversed(parts)))
    return chains


def _wrapped_method_ids() -> set[str]:
    ids: set[str] = set()
    spec = publisher_spec()
    assert spec is not None
    for filename in _API_MODULES:
        tree = ast.parse((_SOURCE_DIR / filename).read_text())
        for chain in _service_chains(tree):
            candidate = f"androidpublisher.{'.'.join(chain)}"
            if candidate in spec.method_ids:
                ids.add(candidate)
    return ids


def test_every_api_call_in_the_source_is_a_real_method():
    """No ``service....`` chain names a method the document does not have.

    A chain that resolves to nothing is either a typo or surface that was
    removed; both should fail here rather than at a user's first call.
    """
    spec = publisher_spec()
    assert spec is not None
    unresolved: list[str] = []
    for filename in _API_MODULES:
        tree = ast.parse((_SOURCE_DIR / filename).read_text())
        for chain in _service_chains(tree):
            dotted = ".".join(chain)
            qualified = f"androidpublisher.{dotted}"
            if qualified in spec.method_ids:
                continue
            # A prefix of a real method id is a resource accessor, not a call.
            if any(m.startswith(f"{qualified}.") for m in spec.method_ids):
                continue
            unresolved.append(f"{filename}: service.{dotted}")
    assert not unresolved, "these do not exist in androidpublisher v3:\n" + "\n".join(unresolved)


def test_wrapped_surface_is_deliberate():
    """The set of wrapped methods only changes when someone means it to."""
    wrapped = _wrapped_method_ids()
    spec = publisher_spec()
    assert spec is not None
    unwrapped = spec.method_ids - wrapped
    # Reachable through android_publisher_invoke; wrapping them is a choice, not
    # a bug, so this asserts the shape of the gap rather than closing it.
    assert wrapped, "no API calls found — the AST scan is broken, not the source"
    assert wrapped <= spec.method_ids
    assert len(wrapped) >= 120, f"wrapped surface shrank to {len(wrapped)}"
    assert len(unwrapped) <= 25, f"unwrapped surface grew to {len(unwrapped)}: {sorted(unwrapped)}"


@pytest.mark.parametrize("key", sorted(METRIC_SETS))
def test_metric_set_accessor_paths_resolve(key):
    """The vitals table is dereferenced by ``getattr``, so nothing else checks it.

    ``reporting.py`` walks these tuples at runtime with ``getattr(node, step)``.
    A typo in one is invisible to the AST scan above *and* to a MagicMock, and
    surfaces only as an AttributeError on a live call.
    """
    _resource_name, accessor_path, _label = METRIC_SETS[key]
    node = verified(MagicMock(), api="playdeveloperreporting")
    for step in accessor_path:
        # Raises AttributeError from the double if the step is not a real
        # resource in the reporting discovery document.
        node = getattr(node, step)()
    assert hasattr(node, "query"), f"{'.'.join(accessor_path)} has no query method"


def test_metric_set_accessor_path_typo_is_caught():
    """The guard above only means something if a wrong path really fails."""
    node = verified(MagicMock(), api="playdeveloperreporting")
    with pytest.raises(AttributeError, match="crashrat is not part of"):
        node.vitals().crashrat()


def test_invokable_roots_are_real_and_exhaustive():
    """Every allowlisted root exists, and every real root is a decision."""
    spec = publisher_spec()
    assert spec is not None
    unreal = sorted(set(INVOKABLE_ROOTS) - spec.roots)
    assert not unreal, f"INVOKABLE_ROOTS names resources that do not exist: {unreal}"
    unaccounted = sorted(spec.roots - set(INVOKABLE_ROOTS) - _DELIBERATELY_NOT_INVOKABLE)
    assert not unaccounted, (
        f"new Android Publisher root(s) {unaccounted}: either add them to "
        f"INVOKABLE_ROOTS or to _DELIBERATELY_NOT_INVOKABLE."
    )


# ---------------------------------------------------------------------------
# The double itself
# ---------------------------------------------------------------------------


def test_double_rejects_a_method_that_does_not_exist():
    service = verified(MagicMock())
    with pytest.raises(AttributeError, match="dataSafetyLabels is not part of"):
        service.applications().dataSafetyLabels(packageName="p", body={})


def test_double_rejects_a_resource_that_does_not_exist():
    service = verified(MagicMock())
    with pytest.raises(AttributeError, match="aplications is not part of"):
        service.aplications()


def test_double_rejects_a_parameter_that_does_not_exist():
    service = verified(MagicMock())
    with pytest.raises(TypeError, match="unexpected keyword argument\\(s\\) package_name"):
        service.applications().dataSafety(package_name="p", body={})


def test_double_rejects_a_missing_required_parameter():
    service = verified(MagicMock())
    with pytest.raises(TypeError, match='Missing required parameter "packageName"'):
        service.applications().dataSafety(body={})


def test_double_still_records_calls_on_the_underlying_mock():
    mock = MagicMock()
    verified(mock).reviews().list(packageName="p")
    mock.reviews.return_value.list.assert_called_once_with(packageName="p")


def test_double_skips_request_building_for_media_uploads():
    # A file handle cannot survive request construction, and failing on it would
    # be noise about the double rather than about the contract.
    mock = MagicMock()
    verified(mock).edits().apks().upload(
        packageName="p",
        editId="e",
        media_body=object(),
        media_mime_type="application/vnd.android.package-archive",
    )
    assert mock.edits.return_value.apks.return_value.upload.called


def test_double_skips_request_building_for_unserialisable_values():
    mock = MagicMock()
    verified(mock).reviews().list(packageName="p", maxResults=MagicMock())
    assert mock.reviews.return_value.list.called


def test_double_passes_mock_protocol_attributes_through():
    mock = MagicMock()
    service = verified(mock)
    assert service._delegate is mock
    assert unwrap(service) is mock
    assert unwrap("not a proxy") == "not a proxy"


def test_spec_for_method_covers_both_planes_and_rejects_others():
    assert spec_for_method("androidpublisher.reviews.list") is publisher_spec()
    assert spec_for_method("playdeveloperreporting.vitals.crashrate.query") is reporting_spec()
    assert spec_for_method("storage.objects.get") is None


def test_bundled_documents_are_present_and_indexed():
    publisher = load_spec(*PUBLISHER_API)
    reporting = load_spec(*REPORTING_API)
    assert publisher is not None and reporting is not None
    # A downgrade that silently removed surface should fail here.
    assert publisher.revision >= "20260723"
    assert len(publisher.method_ids) >= 143
    assert len(reporting.method_ids) >= 25
    assert publisher.method("androidpublisher.applications.dataSafety") is not None
    assert publisher.method("androidpublisher.nope.nope") is None
    assert publisher.schema("SafetyLabelsUpdateRequest") is not None
    assert publisher.schema("NoSuchSchema") is None
