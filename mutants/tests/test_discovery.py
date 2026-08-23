"""Tests for the bundled-discovery-document loader."""

from __future__ import annotations

from google_play_console_mcp import discovery
from google_play_console_mcp.discovery import (
    PUBLISHER_API,
    MethodSpec,
    load_spec,
    publisher_spec,
)


def test_a_method_that_takes_a_body_says_so():
    spec = publisher_spec()
    assert spec is not None
    assert spec.method("androidpublisher.applications.dataSafety").takes_body is True
    assert spec.method("androidpublisher.reviews.list").takes_body is False


def test_method_spec_records_the_verb_and_path():
    spec = publisher_spec()
    method = spec.method("androidpublisher.applications.dataSafety")
    assert isinstance(method, MethodSpec)
    assert method.http_method == "POST"
    assert method.path.endswith("/dataSafety")
    assert method.request_schema == "SafetyLabelsUpdateRequest"


def test_a_non_dict_schema_entry_is_treated_as_absent():
    spec = publisher_spec()
    assert spec.schema("SafetyLabelsUpdateRequest") is not None
    assert spec.schema("definitely-not-a-schema") is None
    # Defensive: a document that stores something other than an object under a
    # schema name must not reach the walker as if it were one.
    spec.schemas["BrokenEntry"] = "not an object"
    try:
        assert spec.schema("BrokenEntry") is None
    finally:
        del spec.schemas["BrokenEntry"]


def test_the_document_is_parsed_once(monkeypatch):
    load_spec.cache_clear()
    calls = []
    real = discovery.get_static_doc

    def counted(name, version):
        calls.append((name, version))
        return real(name, version)

    monkeypatch.setattr(discovery, "get_static_doc", counted)
    first = load_spec(*PUBLISHER_API)
    second = load_spec(*PUBLISHER_API)
    assert first is second
    assert len(calls) == 1
    load_spec.cache_clear()


def test_a_missing_document_degrades_to_none(monkeypatch):
    # A googleapiclient that moved or renamed its data files must switch this
    # server's local validation off, never break publishing.
    load_spec.cache_clear()
    monkeypatch.setattr(discovery, "get_static_doc", lambda *_args: None)
    assert load_spec("androidpublisher", "v3") is None
    load_spec.cache_clear()


def test_roots_are_indexed():
    spec = publisher_spec()
    assert "edits" in spec.roots
    assert "monetization" in spec.roots
