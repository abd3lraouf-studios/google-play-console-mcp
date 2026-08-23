"""Property-based tests for the Data Safety CSV parser.

The example-based tests in ``test_data_safety.py`` cover the shapes we thought
of. These cover the ones we did not: the parser is the only thing standing
between an arbitrary file on disk and a legal declaration, and a file it cannot
handle must always come back as a ``ValidationError`` carrying a remedy — never
as an unhandled exception.

That is not hypothetical. This approach is what surfaced the bare ``_csv.Error``
raised on a field past csv's 128 KB limit, which an unclosed quote reaches by
swallowing the rest of the file into one cell.
"""

from __future__ import annotations

import csv
import io

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from google_play_console_mcp.data_safety import (
    EXPECTED_HEADERS,
    build_request,
    parse_csv,
    validate,
)
from google_play_console_mcp.errors import ValidationError

# Anything a spreadsheet or a translator might put in a cell: quotes, commas,
# newlines, control characters and non-BMP text all reach these files in
# practice, because the label column carries free text in the app's locale.
_CELL = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=40,
)
_ROW = st.tuples(
    _CELL,
    _CELL,
    st.sampled_from(["true", "false", "TRUE", "FALSE", "", " ", "Yes", "1"]),
    st.sampled_from(
        ["REQUIRED", "OPTIONAL", "SINGLE_CHOICE", "MULTIPLE_CHOICE", "MAYBE_REQUIRED", "NEW"]
    ),
    _CELL,
)

_SETTINGS = settings(max_examples=250, suppress_health_check=list(HealthCheck), deadline=None)


def _render(rows: list[tuple[str, ...]]) -> str:
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(EXPECTED_HEADERS)
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


@_SETTINGS
@given(st.lists(_ROW, max_size=12))
def test_the_uploaded_text_is_always_the_input_verbatim(rows):
    """Whatever else happens, the bytes sent to Play are the bytes we were given.

    The confirmation gate binds a human's approval to a digest of this text, so
    any transformation here would mean approving one thing and sending another.
    """
    text = _render(rows)
    assert build_request(parse_csv(text))["safetyLabels"] == text


@_SETTINGS
@given(st.lists(_ROW, max_size=12))
def test_a_report_is_always_internally_consistent(rows):
    parsed = parse_csv(_render(rows))
    report = validate(parsed)

    assert report["valid"] is (not report["errors"])
    # Every row belongs to exactly one question group.
    assert sum(len(q.rows) for q in parsed.questions) == len(parsed.rows)
    assert report["stats"]["rows"] == len(parsed.rows)
    assert report["stats"]["questions"] == len(parsed.questions)
    assert report["stats"]["questions_answered"] <= len(parsed.questions)
    assert report["stats"]["answered_true"] <= len(parsed.rows)


@_SETTINGS
@given(st.lists(_ROW, max_size=12))
def test_warnings_never_decide_validity(rows):
    """Conditional findings must never block. See the module docstring in
    ``data_safety`` for why: most of a real export is contingent on answers
    elsewhere in it, so warnings fire on perfectly good files."""
    report = validate(parse_csv(_render(rows)))
    assert report["valid"] is (not report["errors"])


@_SETTINGS
@given(st.binary(max_size=600))
def test_arbitrary_bytes_fail_only_as_a_validation_error(data):
    """A file we cannot read is a refusal with a remedy, never a crash."""
    try:
        parse_csv(data)
    except ValidationError as exc:
        assert exc.remedy


@_SETTINGS
@given(st.text(max_size=600))
def test_arbitrary_text_fails_only_as_a_validation_error(data):
    try:
        parse_csv(data)
    except ValidationError as exc:
        assert exc.remedy
