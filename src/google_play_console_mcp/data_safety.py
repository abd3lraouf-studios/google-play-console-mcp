"""Read and check a Play Data Safety CSV before it is declared.

The Data Safety form is a legal declaration about what an app collects and
shares, and ``applications.dataSafety`` is **write-only**: there is no endpoint
that reads the current one back. So the CSV can only come from Play Console
(App content > Data safety > Export to CSV), the write replaces the declaration
wholesale, and the exported file a caller started from is the only thing that
could ever undo it. That asymmetry is why this module exists: the upload is the
last moment anything can be checked, and Play's own rejection arrives after the
transfer without naming the row.

Modelled on :mod:`.store_images` — no network, no state, returns a report of
everything that is wrong rather than raising on the first problem, because a
caller that fixes one issue per round trip is worse than one told all four.

**What is deliberately not checked.** Google adds and renames Data Safety
questions without notice and does not publish the catalogue, so an unrecognised
``PSL_*`` id is a warning, never a refusal. Most of the choice-cardinality rules
are warnings for a sharper reason: a real export is ~780 rows across ~217
questions, and whole blocks of it are conditional — the purpose questions under
``PSL_DATA_USAGE_RESPONSES:PSL_NAME:...`` only apply if the app declares it
collects a name. An app that collects nothing legitimately leaves hundreds of
groups blank, so enforcing "at least one TRUE" would reject correct files.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

from .data_safety_ids import KNOWN_ID_TOKENS, SNAPSHOT_DATE, SNAPSHOT_PARTIAL
from .errors import ValidationError

#: The header Play writes, in order. Column 2 is spelled "Response ID
#: (machine readable)" in a real export; the Help Center prose says "Response
#: (machine readable)", so both are accepted rather than guessing which is live.
EXPECTED_HEADERS: tuple[str, ...] = (
    "Question ID (machine readable)",
    "Response ID (machine readable)",
    "Response value",
    "Answer requirement",
    "Human-friendly question label",
)
_HEADER_ALIASES: dict[int, frozenset[str]] = {
    1: frozenset({"response id (machine readable)", "response (machine readable)"}),
}

COLUMN_COUNT = len(EXPECTED_HEADERS)
#: Case-folded, because real exports disagree with the Help Center. Google's
#: prose says "enter TRUE or FALSE", but five of six exports sampled from real
#: apps (2022-2026) write lowercase ``true``/``false``, and one writes upper.
#: Refusing either spelling would reject most genuine files, so both are
#: accepted and only a value that is not a boolean at all is an error.
RESPONSE_VALUES = frozenset({"TRUE", "FALSE", ""})
ANSWER_REQUIREMENTS = frozenset(
    {"OPTIONAL", "REQUIRED", "MULTIPLE_CHOICE", "SINGLE_CHOICE", "MAYBE_REQUIRED"}
)

#: A real export is a few hundred KB. Past this it is not a Data Safety CSV, and
#: reading it into a JSON request body is a way to wedge the server.
MAX_CSV_BYTES = 4 * 1024 * 1024

#: Past this share of unrecognised ids the snapshot is simply out of date, and
#: one warning per question would teach the caller to ignore warnings entirely.
#: The floor keeps a small file from suppressing the handful of warnings that
#: are the whole point of reading it.
_UNKNOWN_RATIO_SUPPRESS = 0.3
_UNKNOWN_SUPPRESS_FLOOR = 5

_EXPORT_REMEDY = (
    "Export a fresh copy from Play Console > App content > Data safety > "
    "Export to CSV, and upload it unmodified."
)


@dataclass(frozen=True)
class Row:
    """One response option, located by the physical line it came from."""

    line: int
    question_id: str
    response_id: str
    value: str
    requirement: str
    label: str
    raw_value: str


@dataclass(frozen=True)
class Question:
    """Every response option sharing one question id."""

    question_id: str
    label: str
    requirement: str
    rows: tuple[Row, ...]

    @property
    def selected(self) -> tuple[str, ...]:
        return tuple(r.response_id for r in self.rows if r.value == "TRUE")

    @property
    def answered(self) -> bool:
        """True once any option carries a value; a blank group is unanswered."""
        return any(r.value for r in self.rows)


@dataclass(frozen=True)
class DataSafetyCsv:
    """A parsed export, plus the exact text that will be sent to Play."""

    text: str
    rows: tuple[Row, ...]
    questions: tuple[Question, ...]
    sha256: str
    byte_length: int
    encoding: str
    line_ending: str
    source_path: str | None = None


def decode_csv_bytes(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationError(
            "This file is UTF-16, not UTF-8.",
            remedy=(
                "Play's Data Safety export is UTF-8; a UTF-16 file has been re-saved by a "
                f"spreadsheet program, which also rewrites values and quoting. {_EXPORT_REMEDY}"
            ),
        )
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def _line_ending(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def _headers_match(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def _iter_records(reader: Any) -> Iterator[list[str]]:
    """Yield rows, turning csv's own failures into an error that carries a remedy.

    ``csv`` raises a bare ``_csv.Error`` on a field past its 128 KB limit — which
    an unclosed quote reaches easily, by swallowing the rest of the file into a
    single cell. Left alone that escapes as an unexpected error with no remedy,
    which is the one thing ``errors.py`` exists to prevent.
    """
    try:
        yield from reader
    except csv.Error as exc:
        raise ValidationError(
            f"This file could not be read as CSV: {exc}",
            remedy=(
                "An unclosed quote is the usual cause — it swallows the rest of the file "
                f"into one field. {_EXPORT_REMEDY}"
            ),
        ) from exc


def parse_csv(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
    """Parse an export into rows and questions, preserving the upload payload.

    The only change made to ``text`` is stripping a UTF-8 BOM. Line endings are
    left as found and the file is never round-tripped through ``csv.writer``:
    re-serialising would silently alter quoting, and the approval a human gives
    in the confirmation gate is bound to a digest of exactly these bytes.
    """
    if isinstance(source, bytes):
        text, encoding = decode_csv_bytes(source)
    else:
        text, encoding = source.lstrip("﻿"), "utf-8"
    # load_csv checks the file on disk; this catches the inline path, which
    # would otherwise have no ceiling at all.
    encoded_size = len(text.encode("utf-8"))
    if encoded_size > MAX_CSV_BYTES:
        raise ValidationError(
            f"This is {encoded_size / 1024 / 1024:.1f} MB; a Data Safety export is well "
            f"under {MAX_CSV_BYTES // 1024 // 1024} MB.",
            remedy="Check this is the Data Safety CSV and not another file.",
        )

    reader = csv.reader(io.StringIO(text, newline=""))
    rows: list[Row] = []
    header: list[str] | None = None
    for record in _iter_records(reader):
        if header is None:
            header = record
            continue
        if not any(cell.strip() for cell in record):
            continue
        padded = [*record, *([""] * COLUMN_COUNT)][:COLUMN_COUNT]
        rows.append(
            Row(
                line=reader.line_num,
                question_id=padded[0].strip(),
                response_id=padded[1].strip(),
                value=padded[2].strip().upper(),
                requirement=padded[3].strip().upper(),
                label=padded[4].strip(),
                raw_value=record[2] if len(record) > 2 else "",
            )
        )

    grouped: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        grouped[row.question_id].append(row)
    questions = tuple(
        Question(
            question_id=qid,
            label=next((r.label for r in group if r.label), ""),
            requirement=group[0].requirement,
            rows=tuple(group),
        )
        for qid, group in grouped.items()
    )

    encoded = text.encode("utf-8")
    return DataSafetyCsv(
        text=text,
        rows=tuple(rows),
        questions=questions,
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def load_csv(path: str | Path) -> DataSafetyCsv:
    """Read and parse an export from disk."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ValidationError(
            f"No Data Safety CSV at {resolved}.",
            remedy=_EXPORT_REMEDY,
        )
    size = resolved.stat().st_size
    if size > MAX_CSV_BYTES:
        raise ValidationError(
            f"{resolved.name} is {size / 1024 / 1024:.1f} MB; a Data Safety export is well "
            f"under {MAX_CSV_BYTES // 1024 // 1024} MB.",
            remedy="Check this is the Data Safety CSV and not another file.",
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"Could not read {resolved}: {exc}",
            remedy="Check the path and file permissions.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def _header_errors(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def _row_errors(csv_file: DataSafetyCsv) -> list[str]:
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for row in csv_file.rows:
        if not row.question_id:
            errors.append(f"Line {row.line} has no Question ID.")
            continue
        if row.value not in RESPONSE_VALUES:
            errors.append(
                f"Line {row.line} Response value is {row.raw_value.strip()!r}; "
                f"expected true, false or blank."
            )
        if row.response_id:
            key = (row.question_id, row.response_id)
            if key in seen:
                errors.append(
                    f"Line {row.line} repeats {row.question_id} / {row.response_id}, "
                    f"already on line {seen[key]}."
                )
            else:
                seen[key] = row.line
    return errors


def _question_findings(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Per-question errors and warnings.

    Only "a single-choice question picked two answers" is an error: that is
    invalid however the rest of the form is filled in. Everything else about
    cardinality depends on answers elsewhere in the file, so it is a warning.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for question in csv_file.questions:
        if question.requirement not in ANSWER_REQUIREMENTS:
            warnings.append(
                f"{question.question_id} has Answer requirement "
                f"{question.requirement!r}, which this server does not know; its "
                f"choice rules were not checked."
            )
            continue
        if not question.answered:
            continue
        chosen = question.selected
        if question.requirement == "SINGLE_CHOICE" and len(chosen) > 1:
            errors.append(
                f"{question.question_id} is SINGLE_CHOICE but {len(chosen)} responses are "
                f"TRUE ({', '.join(chosen)}); exactly one is allowed."
            )
        elif question.requirement == "SINGLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is SINGLE_CHOICE and no response is TRUE.")
        elif question.requirement == "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def _unknown_ids(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = [s for s in question.question_id.split(":") if s]
        if any(s not in KNOWN_ID_TOKENS for s in segments):
            unknown.append(question.question_id)

    if not unknown or not KNOWN_ID_TOKENS:
        # An empty snapshot means silence, not a warning per question.
        return unknown, []

    total = len(csv_file.questions)
    qualifier = "the partial snapshot" if SNAPSHOT_PARTIAL else "the snapshot"
    if (
        len(unknown) > _UNKNOWN_SUPPRESS_FLOOR
        and total
        and len(unknown) / total > _UNKNOWN_RATIO_SUPPRESS
    ):
        return unknown, [
            f"{len(unknown)} of {total} question IDs are not in {qualifier} dated "
            f"{SNAPSHOT_DATE}. That points at an out-of-date snapshot rather than a bad "
            f"file, so per-question warnings are suppressed; the full list is in "
            f"unknown_question_ids."
        ]
    return unknown, [
        f"{qid} is not in {qualifier} dated {SNAPSHOT_DATE}; check it is intended."
        for qid in unknown
    ]


def validate(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if not csv_file.rows:
            errors.append("CSV has a header but no response rows.")
        else:
            errors += _row_errors(csv_file)
            question_errors, question_warnings = _question_findings(csv_file)
            errors += question_errors
            warnings += question_warnings
            unknown, id_warnings = _unknown_ids(csv_file)
            warnings += id_warnings

    if csv_file.encoding == "utf-8-sig":
        warnings.append("A UTF-8 BOM was present; it is stripped from the text sent to Play.")
    if csv_file.line_ending == "mixed":
        warnings.append("Line endings are mixed; the file is sent unchanged.")

    requirements = Counter(q.requirement for q in csv_file.questions)
    maybe_blank = sum(1 for r in csv_file.rows if r.requirement == "MAYBE_REQUIRED" and not r.value)
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "unknown_question_ids": unknown,
        "snapshot": {
            "date": SNAPSHOT_DATE,
            "size": len(KNOWN_ID_TOKENS),
            "partial": SNAPSHOT_PARTIAL,
        },
        "stats": {
            "rows": len(csv_file.rows),
            "questions": len(csv_file.questions),
            "answered_true": sum(1 for r in csv_file.rows if r.value == "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def declared(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
    """Only the questions answered YES — the declaration a human needs to read.

    A full export runs to a few hundred questions, nearly all of them answered
    no or left blank. Returning all of them buries the handful that actually say
    something about the app.
    """
    return [
        {
            "question_id": q.question_id,
            "label": q.label,
            "requirement": q.requirement,
            "selected": list(q.selected),
        }
        for q in csv_file.questions
        if q.selected
    ]


def build_request(csv_file: DataSafetyCsv) -> dict[str, str]:
    """The ``SafetyLabelsUpdateRequest`` body for this export."""
    return {"safetyLabels": csv_file.text}


def preflight_csv(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["valid"],
        "path": csv_file.source_path,
        "detected": {
            "encoding": csv_file.encoding,
            "line_ending": csv_file.line_ending,
            "byte_length": csv_file.byte_length,
            "sha256": csv_file.sha256,
            **report["stats"],
        },
        "errors": report["errors"],
        "warnings": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }
