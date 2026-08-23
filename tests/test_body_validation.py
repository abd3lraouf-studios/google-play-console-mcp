"""Tests for request-body validation against the bundled discovery document."""

from __future__ import annotations

import pytest

from google_play_console_mcp import body_validation
from google_play_console_mcp.body_validation import (
    MODE_ENV,
    enforce,
    validate_request_body,
)
from google_play_console_mcp.errors import ValidationError

DS = "androidpublisher.applications.dataSafety"
SUB = "androidpublisher.monetization.subscriptions.create"
LIST = "androidpublisher.reviews.list"


def _messages(method_id, body):
    return [f"{f.path} {f.message}" for f in validate_request_body(method_id, body)]


def _severities(method_id, body):
    return {f.severity for f in validate_request_body(method_id, body)}


# ---------------------------------------------------------------------------
# Method resolution
# ---------------------------------------------------------------------------


def test_unknown_api_is_passed_through():
    assert validate_request_body("storage.objects.insert", {"anything": 1}) == []


def test_unknown_method_is_passed_through():
    assert validate_request_body("androidpublisher.nope.nope", {"anything": 1}) == []


def test_method_without_a_request_body_rejects_one():
    findings = validate_request_body(LIST, {"anything": 1})
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert "does not take a request body" in findings[0].message


def test_body_must_be_an_object():
    findings = validate_request_body(DS, ["not", "an", "object"])
    assert findings[0].message.startswith("expected an object")


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


def test_valid_body_has_no_findings():
    assert validate_request_body(DS, {"safetyLabels": "a,b\nc,d"}) == []


def test_unknown_field_is_an_error_with_a_suggestion():
    (finding,) = [
        f for f in validate_request_body(DS, {"safteyLables": "x"}) if f.severity == "error"
    ]
    assert finding.path == "body.safteyLables"
    assert "Did you mean 'safetyLabels'?" in finding.message


def test_unknown_field_without_a_near_match_says_nothing_extra():
    (finding,) = [f for f in validate_request_body(DS, {"zzzzzzzz": "x"}) if f.severity == "error"]
    assert "Did you mean" not in finding.message


def test_unknown_field_nested_in_an_array_is_located():
    body = {"packageName": "p", "listings": [{"languageCode": "en-US", "tilte": "x"}]}
    (finding,) = [f for f in validate_request_body(SUB, body) if f.severity == "error"]
    assert finding.path == "body.listings[0].tilte"
    assert "Did you mean 'title'?" in finding.message


def test_absent_required_field_is_only_a_warning():
    assert _severities(DS, {}) == {"warning"}


def test_read_only_field_is_a_warning():
    body = {"packageName": "p", "basePlans": [{"basePlanId": "b", "state": "ACTIVE"}]}
    assert any("read-only" in m for m in _messages(SUB, body))


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("csv", None),
        (123, "expected a string"),
        (True, "expected a string"),
        ({"a": 1}, "expected a string"),
    ],
)
def test_string_typing(value, expected):
    findings = validate_request_body(DS, {"safetyLabels": value})
    errors = [f for f in findings if f.severity == "error"]
    if expected is None:
        assert not errors
    else:
        assert expected in errors[0].message


def test_array_field_must_be_a_list():
    (finding,) = [
        f
        for f in validate_request_body(SUB, {"packageName": "p", "listings": "nope"})
        if f.severity == "error"
    ]
    assert "expected an array" in finding.message


def test_object_field_must_be_an_object():
    body = {"packageName": "p", "basePlans": [{"basePlanId": "b", "regionalConfigs": "no"}]}
    assert any("expected an array" in m for m in _messages(SUB, body))


def test_int64_accepts_both_a_string_and_a_number():
    # Discovery declares int64 as type "string"; proto3 JSON accepts either, so
    # rejecting one would be a false positive on a hand-written body.
    for value in ("12", 12):
        body = {
            "packageName": "p",
            "basePlans": [
                {
                    "basePlanId": "b",
                    "otherRegionsConfig": {
                        "usdPrice": {"currencyCode": "USD", "units": value, "nanos": 0}
                    },
                }
            ],
        }
        assert not [f for f in validate_request_body(SUB, body) if f.severity == "error"]


def test_int64_rejects_a_boolean():
    body = {
        "packageName": "p",
        "basePlans": [
            {
                "basePlanId": "b",
                "otherRegionsConfig": {
                    "usdPrice": {"currencyCode": "USD", "units": True, "nanos": 0}
                },
            }
        ],
    }
    assert any("int64" in m for m in _messages(SUB, body))


def test_integer_rejects_a_boolean_and_a_string():
    for value, expected in ((True, "expected an integer"), ("5", "expected an integer")):
        body = {
            "packageName": "p",
            "basePlans": [
                {
                    "basePlanId": "b",
                    "otherRegionsConfig": {
                        "usdPrice": {"currencyCode": "USD", "units": "1", "nanos": value}
                    },
                }
            ],
        }
        assert any(expected in m for m in _messages(SUB, body))


def test_null_is_treated_as_absent():
    assert not [
        f for f in validate_request_body(DS, {"safetyLabels": None}) if f.severity == "error"
    ]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def test_bad_enum_is_a_warning_not_an_error():
    body = {"packageName": "p", "basePlans": [{"basePlanId": "b", "state": "BOGUS"}]}
    findings = validate_request_body(SUB, body)
    enum_findings = [f for f in findings if "not one of" in f.message]
    assert enum_findings and all(f.severity == "warning" for f in enum_findings)


def test_good_enum_is_silent():
    body = {"packageName": "p", "basePlans": [{"basePlanId": "b", "state": "ACTIVE"}]}
    assert not [f for f in validate_request_body(SUB, body) if "not one of" in f.message]


# ---------------------------------------------------------------------------
# Modes and enforcement
# ---------------------------------------------------------------------------


def test_enforce_raises_on_an_unknown_field():
    with pytest.raises(ValidationError, match="safteyLables"):
        enforce(DS, {"safteyLables": "x"})


def test_enforce_error_names_the_escape_hatch_and_the_revision():
    with pytest.raises(ValidationError) as excinfo:
        enforce(DS, {"safteyLables": "x"})
    assert MODE_ENV in excinfo.value.remedy
    assert excinfo.value.details["discovery_revision"]
    assert excinfo.value.details["findings"][0]["path"] == "body.safteyLables"


def test_enforce_returns_warnings_without_raising():
    warnings = enforce(DS, {})
    assert warnings and all(w.severity == "warning" for w in warnings)


def test_enforce_is_silent_on_a_clean_body():
    assert enforce(DS, {"safetyLabels": "csv"}) == []


def test_off_mode_skips_everything(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "off")
    assert enforce(DS, {"safteyLables": "x"}) == []


def test_warn_mode_blocks_nothing(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "warn")
    assert enforce(DS, {"safteyLables": "x"})


def test_strict_mode_promotes_warnings_to_errors(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "strict")
    with pytest.raises(ValidationError, match="documented as Required"):
        enforce(DS, {})


def test_an_unrecognised_mode_falls_back_to_on(monkeypatch):
    monkeypatch.setenv(MODE_ENV, "banana")
    with pytest.raises(ValidationError):
        enforce(DS, {"safteyLables": "x"})


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_findings_are_capped(monkeypatch):
    monkeypatch.setattr(body_validation, "_MAX_FINDINGS", 3)
    body = {f"unknownField{i}": 1 for i in range(20)}
    body["packageName"] = "p"
    assert len(validate_request_body(SUB, body)) == 3


def test_depth_is_capped(monkeypatch):
    monkeypatch.setattr(body_validation, "_MAX_DEPTH", 1)
    body = {"packageName": "p", "listings": [{"tilte": "x"}]}
    assert not [f for f in validate_request_body(SUB, body) if f.severity == "error"]


def test_a_dangling_ref_skips_the_subtree():
    # A document that disagrees with itself must not be able to block a write.
    class _NoSchemas:
        def schema(self, _name):
            return None

    walker = body_validation._Walker(_NoSchemas(), root_required=True)
    walker.walk({"a": 1}, {"$ref": "Nope"}, "body", 0)
    assert walker.findings == []


def test_a_map_field_validates_values_not_keys():
    spec = body_validation.spec_for_method(DS)
    walker = body_validation._Walker(spec, root_required=False)
    schema = {"type": "object", "additionalProperties": {"type": "string"}}
    walker.walk({"anything at all": "ok", "another": 5}, schema, "body", 0)
    assert len(walker.findings) == 1
    assert walker.findings[0].path == "body.another"


def test_an_object_without_properties_is_not_walked():
    spec = body_validation.spec_for_method(DS)
    walker = body_validation._Walker(spec, root_required=False)
    walker.walk({"a": 1}, {"type": "object"}, "body", 0)
    assert walker.findings == []


def test_an_array_without_item_schema_is_not_walked():
    spec = body_validation.spec_for_method(DS)
    walker = body_validation._Walker(spec, root_required=False)
    walker.walk([{"a": 1}], {"type": "array"}, "body", 0)
    assert walker.findings == []


def test_an_untyped_schema_is_accepted():
    spec = body_validation.spec_for_method(DS)
    walker = body_validation._Walker(spec, root_required=False)
    walker.walk("anything", {}, "body", 0)
    assert walker.findings == []


def test_a_long_enum_list_is_truncated_in_the_message():
    spec = body_validation.spec_for_method(DS)
    walker = body_validation._Walker(spec, root_required=False)
    walker.walk("NOPE", {"type": "string", "enum": [f"V{i}" for i in range(10)]}, "body", 0)
    assert "4 more" in walker.findings[0].message


def test_a_deprecated_field_is_a_warning():
    spec = body_validation.spec_for_method(DS)
    walker = body_validation._Walker(spec, root_required=False)
    schema = {"type": "object", "properties": {"old": {"type": "string", "deprecated": True}}}
    walker.walk({"old": "x"}, schema, "body", 0)
    assert any("deprecated" in f.message for f in walker.findings)


def test_number_typing_accepts_ints_and_floats_but_not_booleans():
    spec = body_validation.spec_for_method(DS)
    walker = body_validation._Walker(spec, root_required=False)
    schema = {"type": "number"}
    for value in (0.5, 1):
        walker.walk(value, schema, "body.userFraction", 0)
    assert walker.findings == []
    walker.walk(True, schema, "body.userFraction", 0)
    assert "expected a number" in walker.findings[0].message


def test_a_request_schema_the_document_does_not_define_is_passed_through(monkeypatch):
    # Defensive: a method whose $ref dangles must not block the write.
    spec = body_validation.spec_for_method(DS)
    method = spec.method(DS)
    monkeypatch.setitem(spec.methods, DS, method.__class__(DS, "POST", method.path, "NoSuchSchema"))
    assert validate_request_body(DS, {"anything": 1}) == []
