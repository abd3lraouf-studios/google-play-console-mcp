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


from mutmut.mutation.trampoline import wrap_in_trampoline as _mutmut_mutated, MutantDict


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
mutants_x_decode_csv_bytes__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x_decode_csv_bytes__mutmut)
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


def x_decode_csv_bytes__mutmut_orig(data: bytes) -> tuple[str, str]:
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


def x_decode_csv_bytes__mutmut_1(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith(None):
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


def x_decode_csv_bytes__mutmut_2(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"XX\xff\xfeXX", b"\xfe\xff")):
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


def x_decode_csv_bytes__mutmut_3(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xFF\xFE", b"\xfe\xff")):
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


def x_decode_csv_bytes__mutmut_4(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xff\xfe", b"XX\xfe\xffXX")):
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


def x_decode_csv_bytes__mutmut_5(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xff\xfe", b"\xFE\xFF")):
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


def x_decode_csv_bytes__mutmut_6(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationError(
            None,
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


def x_decode_csv_bytes__mutmut_7(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationError(
            "This file is UTF-16, not UTF-8.",
            remedy=None,
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


def x_decode_csv_bytes__mutmut_8(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationError(
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


def x_decode_csv_bytes__mutmut_9(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationError(
            "This file is UTF-16, not UTF-8.",
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


def x_decode_csv_bytes__mutmut_10(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationError(
            "XXThis file is UTF-16, not UTF-8.XX",
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


def x_decode_csv_bytes__mutmut_11(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationError(
            "this file is utf-16, not utf-8.",
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


def x_decode_csv_bytes__mutmut_12(data: bytes) -> tuple[str, str]:
    """Decode a Data Safety export as UTF-8, naming the cause when it is not.

    UTF-16 is rejected rather than transcoded. Play exports UTF-8; a UTF-16 file
    has been opened and re-saved by a spreadsheet, which is also what turns
    ``TRUE`` into ``True`` and reflows quoting. Silently accepting it would mean
    uploading a declaration nobody inspected in the form it now has.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValidationError(
            "THIS FILE IS UTF-16, NOT UTF-8.",
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


def x_decode_csv_bytes__mutmut_13(data: bytes) -> tuple[str, str]:
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
                "XXPlay's Data Safety export is UTF-8; a UTF-16 file has been re-saved by a XX"
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


def x_decode_csv_bytes__mutmut_14(data: bytes) -> tuple[str, str]:
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
                "play's data safety export is utf-8; a utf-16 file has been re-saved by a "
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


def x_decode_csv_bytes__mutmut_15(data: bytes) -> tuple[str, str]:
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
                "PLAY'S DATA SAFETY EXPORT IS UTF-8; A UTF-16 FILE HAS BEEN RE-SAVED BY A "
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


def x_decode_csv_bytes__mutmut_16(data: bytes) -> tuple[str, str]:
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
    if data.startswith(None):
        return data[3:].decode("utf-8"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_17(data: bytes) -> tuple[str, str]:
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
    if data.startswith(b"XX\xef\xbb\xbfXX"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_18(data: bytes) -> tuple[str, str]:
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
    if data.startswith(b"\xEF\xBB\xBF"):
        return data[3:].decode("utf-8"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_19(data: bytes) -> tuple[str, str]:
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
        return data[3:].decode(None), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_20(data: bytes) -> tuple[str, str]:
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
        return data[4:].decode("utf-8"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_21(data: bytes) -> tuple[str, str]:
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
        return data[3:].decode("XXutf-8XX"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_22(data: bytes) -> tuple[str, str]:
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
        return data[3:].decode("UTF-8"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_23(data: bytes) -> tuple[str, str]:
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
        return data[3:].decode("utf-8"), "XXutf-8-sigXX"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_24(data: bytes) -> tuple[str, str]:
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
        return data[3:].decode("utf-8"), "UTF-8-SIG"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_25(data: bytes) -> tuple[str, str]:
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
        return data.decode(None), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_26(data: bytes) -> tuple[str, str]:
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
        return data.decode("XXutf-8XX"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_27(data: bytes) -> tuple[str, str]:
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
        return data.decode("UTF-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_28(data: bytes) -> tuple[str, str]:
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
        return data.decode("utf-8"), "XXutf-8XX"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_29(data: bytes) -> tuple[str, str]:
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
        return data.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError as exc:
        raise ValidationError(
            "This file is not valid UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_30(data: bytes) -> tuple[str, str]:
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
            None,
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_31(data: bytes) -> tuple[str, str]:
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
            remedy=None,
        ) from exc


def x_decode_csv_bytes__mutmut_32(data: bytes) -> tuple[str, str]:
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
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_33(data: bytes) -> tuple[str, str]:
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
            ) from exc


def x_decode_csv_bytes__mutmut_34(data: bytes) -> tuple[str, str]:
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
            "XXThis file is not valid UTF-8.XX",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_35(data: bytes) -> tuple[str, str]:
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
            "this file is not valid utf-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc


def x_decode_csv_bytes__mutmut_36(data: bytes) -> tuple[str, str]:
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
            "THIS FILE IS NOT VALID UTF-8.",
            remedy=_EXPORT_REMEDY,
        ) from exc

mutants_x_decode_csv_bytes__mutmut['_mutmut_orig'] = x_decode_csv_bytes__mutmut_orig # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_1'] = x_decode_csv_bytes__mutmut_1 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_2'] = x_decode_csv_bytes__mutmut_2 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_3'] = x_decode_csv_bytes__mutmut_3 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_4'] = x_decode_csv_bytes__mutmut_4 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_5'] = x_decode_csv_bytes__mutmut_5 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_6'] = x_decode_csv_bytes__mutmut_6 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_7'] = x_decode_csv_bytes__mutmut_7 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_8'] = x_decode_csv_bytes__mutmut_8 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_9'] = x_decode_csv_bytes__mutmut_9 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_10'] = x_decode_csv_bytes__mutmut_10 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_11'] = x_decode_csv_bytes__mutmut_11 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_12'] = x_decode_csv_bytes__mutmut_12 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_13'] = x_decode_csv_bytes__mutmut_13 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_14'] = x_decode_csv_bytes__mutmut_14 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_15'] = x_decode_csv_bytes__mutmut_15 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_16'] = x_decode_csv_bytes__mutmut_16 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_17'] = x_decode_csv_bytes__mutmut_17 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_18'] = x_decode_csv_bytes__mutmut_18 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_19'] = x_decode_csv_bytes__mutmut_19 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_20'] = x_decode_csv_bytes__mutmut_20 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_21'] = x_decode_csv_bytes__mutmut_21 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_22'] = x_decode_csv_bytes__mutmut_22 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_23'] = x_decode_csv_bytes__mutmut_23 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_24'] = x_decode_csv_bytes__mutmut_24 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_25'] = x_decode_csv_bytes__mutmut_25 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_26'] = x_decode_csv_bytes__mutmut_26 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_27'] = x_decode_csv_bytes__mutmut_27 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_28'] = x_decode_csv_bytes__mutmut_28 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_29'] = x_decode_csv_bytes__mutmut_29 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_30'] = x_decode_csv_bytes__mutmut_30 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_31'] = x_decode_csv_bytes__mutmut_31 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_32'] = x_decode_csv_bytes__mutmut_32 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_33'] = x_decode_csv_bytes__mutmut_33 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_34'] = x_decode_csv_bytes__mutmut_34 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_35'] = x_decode_csv_bytes__mutmut_35 # type: ignore # mutmut generated
mutants_x_decode_csv_bytes__mutmut['x_decode_csv_bytes__mutmut_36'] = x_decode_csv_bytes__mutmut_36 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x__line_ending__mutmut)
def _line_ending(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_orig(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_1(text: str) -> str:
    crlf = None
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_2(text: str) -> str:
    crlf = text.count(None)
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_3(text: str) -> str:
    crlf = text.count("XX\r\nXX")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_4(text: str) -> str:
    crlf = text.count("\r\n")
    lf = None
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_5(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") + crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_6(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count(None) - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_7(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("XX\nXX") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_8(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf or lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_9(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "XXmixedXX"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_10(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "MIXED"
    if crlf:
        return "CRLF"
    return "LF" if lf else "none"


def x__line_ending__mutmut_11(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "XXCRLFXX"
    return "LF" if lf else "none"


def x__line_ending__mutmut_12(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "crlf"
    return "LF" if lf else "none"


def x__line_ending__mutmut_13(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "XXLFXX" if lf else "none"


def x__line_ending__mutmut_14(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "lf" if lf else "none"


def x__line_ending__mutmut_15(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "XXnoneXX"


def x__line_ending__mutmut_16(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    if crlf:
        return "CRLF"
    return "LF" if lf else "NONE"

mutants_x__line_ending__mutmut['_mutmut_orig'] = x__line_ending__mutmut_orig # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_1'] = x__line_ending__mutmut_1 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_2'] = x__line_ending__mutmut_2 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_3'] = x__line_ending__mutmut_3 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_4'] = x__line_ending__mutmut_4 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_5'] = x__line_ending__mutmut_5 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_6'] = x__line_ending__mutmut_6 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_7'] = x__line_ending__mutmut_7 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_8'] = x__line_ending__mutmut_8 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_9'] = x__line_ending__mutmut_9 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_10'] = x__line_ending__mutmut_10 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_11'] = x__line_ending__mutmut_11 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_12'] = x__line_ending__mutmut_12 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_13'] = x__line_ending__mutmut_13 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_14'] = x__line_ending__mutmut_14 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_15'] = x__line_ending__mutmut_15 # type: ignore # mutmut generated
mutants_x__line_ending__mutmut['x__line_ending__mutmut_16'] = x__line_ending__mutmut_16 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x__headers_match__mutmut)
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


def x__headers_match__mutmut_orig(found: list[str]) -> str | None:
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


def x__headers_match__mutmut_1(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) <= COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_2(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = None
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_3(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(None)
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_4(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = "XX, XX".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_5(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(None) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_6(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(None):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_7(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = None
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_8(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip(None)
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_9(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().rstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_10(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("XX﻿XX")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_11(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = None
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_12(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(None, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_13(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, None)
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_14(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_15(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, )
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_16(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset(None))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_17(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.upper()}))
        if actual.lower() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_18(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.upper() not in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_19(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() in accepted:
            return f"Column {index + 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_20(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index - 1} header is {actual!r}; expected {expected!r}."
    return None


def x__headers_match__mutmut_21(found: list[str]) -> str | None:
    """None when the header row is Play's, else what is wrong with it."""
    if len(found) < COLUMN_COUNT:
        missing = ", ".join(repr(h) for h in EXPECTED_HEADERS[len(found) :])
        return f"Header has {len(found)} columns; expected {COLUMN_COUNT}. Missing: {missing}."
    for index, expected in enumerate(EXPECTED_HEADERS):
        actual = found[index].strip().lstrip("﻿")
        accepted = _HEADER_ALIASES.get(index, frozenset({expected.lower()}))
        if actual.lower() not in accepted:
            return f"Column {index + 2} header is {actual!r}; expected {expected!r}."
    return None

mutants_x__headers_match__mutmut['_mutmut_orig'] = x__headers_match__mutmut_orig # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_1'] = x__headers_match__mutmut_1 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_2'] = x__headers_match__mutmut_2 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_3'] = x__headers_match__mutmut_3 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_4'] = x__headers_match__mutmut_4 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_5'] = x__headers_match__mutmut_5 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_6'] = x__headers_match__mutmut_6 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_7'] = x__headers_match__mutmut_7 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_8'] = x__headers_match__mutmut_8 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_9'] = x__headers_match__mutmut_9 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_10'] = x__headers_match__mutmut_10 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_11'] = x__headers_match__mutmut_11 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_12'] = x__headers_match__mutmut_12 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_13'] = x__headers_match__mutmut_13 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_14'] = x__headers_match__mutmut_14 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_15'] = x__headers_match__mutmut_15 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_16'] = x__headers_match__mutmut_16 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_17'] = x__headers_match__mutmut_17 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_18'] = x__headers_match__mutmut_18 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_19'] = x__headers_match__mutmut_19 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_20'] = x__headers_match__mutmut_20 # type: ignore # mutmut generated
mutants_x__headers_match__mutmut['x__headers_match__mutmut_21'] = x__headers_match__mutmut_21 # type: ignore # mutmut generated
mutants_x__iter_records__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x__iter_records__mutmut)
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


def x__iter_records__mutmut_orig(reader: Any) -> Iterator[list[str]]:
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


def x__iter_records__mutmut_1(reader: Any) -> Iterator[list[str]]:
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
            None,
            remedy=(
                "An unclosed quote is the usual cause — it swallows the rest of the file "
                f"into one field. {_EXPORT_REMEDY}"
            ),
        ) from exc


def x__iter_records__mutmut_2(reader: Any) -> Iterator[list[str]]:
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
            remedy=None,
        ) from exc


def x__iter_records__mutmut_3(reader: Any) -> Iterator[list[str]]:
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
            remedy=(
                "An unclosed quote is the usual cause — it swallows the rest of the file "
                f"into one field. {_EXPORT_REMEDY}"
            ),
        ) from exc


def x__iter_records__mutmut_4(reader: Any) -> Iterator[list[str]]:
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
            ) from exc


def x__iter_records__mutmut_5(reader: Any) -> Iterator[list[str]]:
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
                "XXAn unclosed quote is the usual cause — it swallows the rest of the file XX"
                f"into one field. {_EXPORT_REMEDY}"
            ),
        ) from exc


def x__iter_records__mutmut_6(reader: Any) -> Iterator[list[str]]:
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
                "an unclosed quote is the usual cause — it swallows the rest of the file "
                f"into one field. {_EXPORT_REMEDY}"
            ),
        ) from exc


def x__iter_records__mutmut_7(reader: Any) -> Iterator[list[str]]:
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
                "AN UNCLOSED QUOTE IS THE USUAL CAUSE — IT SWALLOWS THE REST OF THE FILE "
                f"into one field. {_EXPORT_REMEDY}"
            ),
        ) from exc

mutants_x__iter_records__mutmut['_mutmut_orig'] = x__iter_records__mutmut_orig # type: ignore # mutmut generated
mutants_x__iter_records__mutmut['x__iter_records__mutmut_1'] = x__iter_records__mutmut_1 # type: ignore # mutmut generated
mutants_x__iter_records__mutmut['x__iter_records__mutmut_2'] = x__iter_records__mutmut_2 # type: ignore # mutmut generated
mutants_x__iter_records__mutmut['x__iter_records__mutmut_3'] = x__iter_records__mutmut_3 # type: ignore # mutmut generated
mutants_x__iter_records__mutmut['x__iter_records__mutmut_4'] = x__iter_records__mutmut_4 # type: ignore # mutmut generated
mutants_x__iter_records__mutmut['x__iter_records__mutmut_5'] = x__iter_records__mutmut_5 # type: ignore # mutmut generated
mutants_x__iter_records__mutmut['x__iter_records__mutmut_6'] = x__iter_records__mutmut_6 # type: ignore # mutmut generated
mutants_x__iter_records__mutmut['x__iter_records__mutmut_7'] = x__iter_records__mutmut_7 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x_parse_csv__mutmut)
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


def x_parse_csv__mutmut_orig(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_1(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
    """Parse an export into rows and questions, preserving the upload payload.

    The only change made to ``text`` is stripping a UTF-8 BOM. Line endings are
    left as found and the file is never round-tripped through ``csv.writer``:
    re-serialising would silently alter quoting, and the approval a human gives
    in the confirmation gate is bound to a digest of exactly these bytes.
    """
    if isinstance(source, bytes):
        text, encoding = None
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


def x_parse_csv__mutmut_2(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
    """Parse an export into rows and questions, preserving the upload payload.

    The only change made to ``text`` is stripping a UTF-8 BOM. Line endings are
    left as found and the file is never round-tripped through ``csv.writer``:
    re-serialising would silently alter quoting, and the approval a human gives
    in the confirmation gate is bound to a digest of exactly these bytes.
    """
    if isinstance(source, bytes):
        text, encoding = decode_csv_bytes(None)
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


def x_parse_csv__mutmut_3(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
    """Parse an export into rows and questions, preserving the upload payload.

    The only change made to ``text`` is stripping a UTF-8 BOM. Line endings are
    left as found and the file is never round-tripped through ``csv.writer``:
    re-serialising would silently alter quoting, and the approval a human gives
    in the confirmation gate is bound to a digest of exactly these bytes.
    """
    if isinstance(source, bytes):
        text, encoding = decode_csv_bytes(source)
    else:
        text, encoding = None
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


def x_parse_csv__mutmut_4(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
    """Parse an export into rows and questions, preserving the upload payload.

    The only change made to ``text`` is stripping a UTF-8 BOM. Line endings are
    left as found and the file is never round-tripped through ``csv.writer``:
    re-serialising would silently alter quoting, and the approval a human gives
    in the confirmation gate is bound to a digest of exactly these bytes.
    """
    if isinstance(source, bytes):
        text, encoding = decode_csv_bytes(source)
    else:
        text, encoding = source.lstrip(None), "utf-8"
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


def x_parse_csv__mutmut_5(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
    """Parse an export into rows and questions, preserving the upload payload.

    The only change made to ``text`` is stripping a UTF-8 BOM. Line endings are
    left as found and the file is never round-tripped through ``csv.writer``:
    re-serialising would silently alter quoting, and the approval a human gives
    in the confirmation gate is bound to a digest of exactly these bytes.
    """
    if isinstance(source, bytes):
        text, encoding = decode_csv_bytes(source)
    else:
        text, encoding = source.rstrip("﻿"), "utf-8"
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


def x_parse_csv__mutmut_6(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
    """Parse an export into rows and questions, preserving the upload payload.

    The only change made to ``text`` is stripping a UTF-8 BOM. Line endings are
    left as found and the file is never round-tripped through ``csv.writer``:
    re-serialising would silently alter quoting, and the approval a human gives
    in the confirmation gate is bound to a digest of exactly these bytes.
    """
    if isinstance(source, bytes):
        text, encoding = decode_csv_bytes(source)
    else:
        text, encoding = source.lstrip("XX﻿XX"), "utf-8"
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


def x_parse_csv__mutmut_7(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
    """Parse an export into rows and questions, preserving the upload payload.

    The only change made to ``text`` is stripping a UTF-8 BOM. Line endings are
    left as found and the file is never round-tripped through ``csv.writer``:
    re-serialising would silently alter quoting, and the approval a human gives
    in the confirmation gate is bound to a digest of exactly these bytes.
    """
    if isinstance(source, bytes):
        text, encoding = decode_csv_bytes(source)
    else:
        text, encoding = source.lstrip("﻿"), "XXutf-8XX"
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


def x_parse_csv__mutmut_8(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
    """Parse an export into rows and questions, preserving the upload payload.

    The only change made to ``text`` is stripping a UTF-8 BOM. Line endings are
    left as found and the file is never round-tripped through ``csv.writer``:
    re-serialising would silently alter quoting, and the approval a human gives
    in the confirmation gate is bound to a digest of exactly these bytes.
    """
    if isinstance(source, bytes):
        text, encoding = decode_csv_bytes(source)
    else:
        text, encoding = source.lstrip("﻿"), "UTF-8"
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


def x_parse_csv__mutmut_9(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
    encoded_size = None
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


def x_parse_csv__mutmut_10(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
    if encoded_size >= MAX_CSV_BYTES:
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


def x_parse_csv__mutmut_11(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            None,
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


def x_parse_csv__mutmut_12(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            remedy=None,
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


def x_parse_csv__mutmut_13(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_14(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_15(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            f"This is {encoded_size / 1024 * 1024:.1f} MB; a Data Safety export is well "
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


def x_parse_csv__mutmut_16(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            f"This is {encoded_size * 1024 / 1024:.1f} MB; a Data Safety export is well "
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


def x_parse_csv__mutmut_17(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            f"This is {encoded_size / 1025 / 1024:.1f} MB; a Data Safety export is well "
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


def x_parse_csv__mutmut_18(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            f"This is {encoded_size / 1024 / 1025:.1f} MB; a Data Safety export is well "
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


def x_parse_csv__mutmut_19(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            f"under {MAX_CSV_BYTES // 1024 / 1024} MB.",
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


def x_parse_csv__mutmut_20(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            f"under {MAX_CSV_BYTES / 1024 // 1024} MB.",
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


def x_parse_csv__mutmut_21(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            f"under {MAX_CSV_BYTES // 1025 // 1024} MB.",
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


def x_parse_csv__mutmut_22(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            f"under {MAX_CSV_BYTES // 1024 // 1025} MB.",
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


def x_parse_csv__mutmut_23(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            remedy="XXCheck this is the Data Safety CSV and not another file.XX",
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


def x_parse_csv__mutmut_24(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            remedy="check this is the data safety csv and not another file.",
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


def x_parse_csv__mutmut_25(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            remedy="CHECK THIS IS THE DATA SAFETY CSV AND NOT ANOTHER FILE.",
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


def x_parse_csv__mutmut_26(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    reader = None
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


def x_parse_csv__mutmut_27(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    reader = csv.reader(None)
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


def x_parse_csv__mutmut_28(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    reader = csv.reader(io.StringIO(None, newline=""))
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


def x_parse_csv__mutmut_29(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    reader = csv.reader(io.StringIO(text, newline=None))
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


def x_parse_csv__mutmut_30(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    reader = csv.reader(io.StringIO(newline=""))
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


def x_parse_csv__mutmut_31(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    reader = csv.reader(io.StringIO(text, ))
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


def x_parse_csv__mutmut_32(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    reader = csv.reader(io.StringIO(text, newline="XXXX"))
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


def x_parse_csv__mutmut_33(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
    rows: list[Row] = None
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


def x_parse_csv__mutmut_34(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
    header: list[str] | None = ""
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


def x_parse_csv__mutmut_35(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
    for record in _iter_records(None):
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


def x_parse_csv__mutmut_36(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        if header is not None:
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


def x_parse_csv__mutmut_37(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            header = None
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


def x_parse_csv__mutmut_38(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            break
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


def x_parse_csv__mutmut_39(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        if any(cell.strip() for cell in record):
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


def x_parse_csv__mutmut_40(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        if not any(None):
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


def x_parse_csv__mutmut_41(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            break
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


def x_parse_csv__mutmut_42(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        padded = None
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


def x_parse_csv__mutmut_43(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        padded = [*record, *([""] / COLUMN_COUNT)][:COLUMN_COUNT]
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


def x_parse_csv__mutmut_44(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        padded = [*record, *(["XXXX"] * COLUMN_COUNT)][:COLUMN_COUNT]
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


def x_parse_csv__mutmut_45(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            None
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


def x_parse_csv__mutmut_46(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                line=None,
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


def x_parse_csv__mutmut_47(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                question_id=None,
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


def x_parse_csv__mutmut_48(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                response_id=None,
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


def x_parse_csv__mutmut_49(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                value=None,
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


def x_parse_csv__mutmut_50(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                requirement=None,
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


def x_parse_csv__mutmut_51(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                label=None,
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


def x_parse_csv__mutmut_52(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                raw_value=None,
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


def x_parse_csv__mutmut_53(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_54(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_55(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_56(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_57(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_58(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_59(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_60(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                question_id=padded[1].strip(),
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


def x_parse_csv__mutmut_61(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                response_id=padded[2].strip(),
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


def x_parse_csv__mutmut_62(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                value=padded[2].strip().lower(),
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


def x_parse_csv__mutmut_63(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                value=padded[3].strip().upper(),
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


def x_parse_csv__mutmut_64(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                requirement=padded[3].strip().lower(),
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


def x_parse_csv__mutmut_65(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                requirement=padded[4].strip().upper(),
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


def x_parse_csv__mutmut_66(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                label=padded[5].strip(),
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


def x_parse_csv__mutmut_67(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                raw_value=record[3] if len(record) > 2 else "",
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


def x_parse_csv__mutmut_68(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                raw_value=record[2] if len(record) >= 2 else "",
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


def x_parse_csv__mutmut_69(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                raw_value=record[2] if len(record) > 3 else "",
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


def x_parse_csv__mutmut_70(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
                raw_value=record[2] if len(record) > 2 else "XXXX",
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


def x_parse_csv__mutmut_71(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    grouped: dict[str, list[Row]] = None
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


def x_parse_csv__mutmut_72(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    grouped: dict[str, list[Row]] = defaultdict(None)
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


def x_parse_csv__mutmut_73(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        grouped[row.question_id].append(None)
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


def x_parse_csv__mutmut_74(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
    questions = None

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


def x_parse_csv__mutmut_75(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        None
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


def x_parse_csv__mutmut_76(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            question_id=None,
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


def x_parse_csv__mutmut_77(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            label=None,
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


def x_parse_csv__mutmut_78(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            requirement=None,
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


def x_parse_csv__mutmut_79(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            rows=None,
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


def x_parse_csv__mutmut_80(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_81(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_82(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_83(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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


def x_parse_csv__mutmut_84(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            label=next(None, ""),
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


def x_parse_csv__mutmut_85(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            label=next((r.label for r in group if r.label), None),
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


def x_parse_csv__mutmut_86(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            label=next(""),
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


def x_parse_csv__mutmut_87(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            label=next((r.label for r in group if r.label), ),
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


def x_parse_csv__mutmut_88(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            label=next((r.label for r in group if r.label), "XXXX"),
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


def x_parse_csv__mutmut_89(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            requirement=group[1].requirement,
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


def x_parse_csv__mutmut_90(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
            rows=tuple(None),
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


def x_parse_csv__mutmut_91(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    encoded = None
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


def x_parse_csv__mutmut_92(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    encoded = text.encode(None)
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


def x_parse_csv__mutmut_93(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    encoded = text.encode("XXutf-8XX")
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


def x_parse_csv__mutmut_94(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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

    encoded = text.encode("UTF-8")
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


def x_parse_csv__mutmut_95(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        text=None,
        rows=tuple(rows),
        questions=questions,
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_96(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        rows=None,
        questions=questions,
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_97(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        questions=None,
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_98(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        sha256=None,
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_99(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        byte_length=None,
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_100(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        encoding=None,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_101(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        line_ending=None,
        source_path=source_path,
    )


def x_parse_csv__mutmut_102(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        source_path=None,
    )


def x_parse_csv__mutmut_103(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        rows=tuple(rows),
        questions=questions,
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_104(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        questions=questions,
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_105(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_106(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_107(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_108(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_109(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        source_path=source_path,
    )


def x_parse_csv__mutmut_110(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        )


def x_parse_csv__mutmut_111(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        rows=tuple(None),
        questions=questions,
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_112(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        sha256=hashlib.sha256(None).hexdigest(),
        byte_length=len(encoded),
        encoding=encoding,
        line_ending=_line_ending(text),
        source_path=source_path,
    )


def x_parse_csv__mutmut_113(source: bytes | str, *, source_path: str | None = None) -> DataSafetyCsv:
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
        line_ending=_line_ending(None),
        source_path=source_path,
    )

mutants_x_parse_csv__mutmut['_mutmut_orig'] = x_parse_csv__mutmut_orig # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_1'] = x_parse_csv__mutmut_1 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_2'] = x_parse_csv__mutmut_2 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_3'] = x_parse_csv__mutmut_3 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_4'] = x_parse_csv__mutmut_4 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_5'] = x_parse_csv__mutmut_5 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_6'] = x_parse_csv__mutmut_6 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_7'] = x_parse_csv__mutmut_7 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_8'] = x_parse_csv__mutmut_8 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_9'] = x_parse_csv__mutmut_9 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_10'] = x_parse_csv__mutmut_10 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_11'] = x_parse_csv__mutmut_11 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_12'] = x_parse_csv__mutmut_12 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_13'] = x_parse_csv__mutmut_13 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_14'] = x_parse_csv__mutmut_14 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_15'] = x_parse_csv__mutmut_15 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_16'] = x_parse_csv__mutmut_16 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_17'] = x_parse_csv__mutmut_17 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_18'] = x_parse_csv__mutmut_18 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_19'] = x_parse_csv__mutmut_19 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_20'] = x_parse_csv__mutmut_20 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_21'] = x_parse_csv__mutmut_21 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_22'] = x_parse_csv__mutmut_22 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_23'] = x_parse_csv__mutmut_23 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_24'] = x_parse_csv__mutmut_24 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_25'] = x_parse_csv__mutmut_25 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_26'] = x_parse_csv__mutmut_26 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_27'] = x_parse_csv__mutmut_27 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_28'] = x_parse_csv__mutmut_28 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_29'] = x_parse_csv__mutmut_29 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_30'] = x_parse_csv__mutmut_30 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_31'] = x_parse_csv__mutmut_31 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_32'] = x_parse_csv__mutmut_32 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_33'] = x_parse_csv__mutmut_33 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_34'] = x_parse_csv__mutmut_34 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_35'] = x_parse_csv__mutmut_35 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_36'] = x_parse_csv__mutmut_36 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_37'] = x_parse_csv__mutmut_37 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_38'] = x_parse_csv__mutmut_38 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_39'] = x_parse_csv__mutmut_39 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_40'] = x_parse_csv__mutmut_40 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_41'] = x_parse_csv__mutmut_41 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_42'] = x_parse_csv__mutmut_42 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_43'] = x_parse_csv__mutmut_43 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_44'] = x_parse_csv__mutmut_44 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_45'] = x_parse_csv__mutmut_45 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_46'] = x_parse_csv__mutmut_46 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_47'] = x_parse_csv__mutmut_47 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_48'] = x_parse_csv__mutmut_48 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_49'] = x_parse_csv__mutmut_49 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_50'] = x_parse_csv__mutmut_50 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_51'] = x_parse_csv__mutmut_51 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_52'] = x_parse_csv__mutmut_52 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_53'] = x_parse_csv__mutmut_53 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_54'] = x_parse_csv__mutmut_54 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_55'] = x_parse_csv__mutmut_55 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_56'] = x_parse_csv__mutmut_56 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_57'] = x_parse_csv__mutmut_57 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_58'] = x_parse_csv__mutmut_58 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_59'] = x_parse_csv__mutmut_59 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_60'] = x_parse_csv__mutmut_60 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_61'] = x_parse_csv__mutmut_61 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_62'] = x_parse_csv__mutmut_62 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_63'] = x_parse_csv__mutmut_63 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_64'] = x_parse_csv__mutmut_64 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_65'] = x_parse_csv__mutmut_65 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_66'] = x_parse_csv__mutmut_66 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_67'] = x_parse_csv__mutmut_67 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_68'] = x_parse_csv__mutmut_68 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_69'] = x_parse_csv__mutmut_69 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_70'] = x_parse_csv__mutmut_70 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_71'] = x_parse_csv__mutmut_71 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_72'] = x_parse_csv__mutmut_72 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_73'] = x_parse_csv__mutmut_73 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_74'] = x_parse_csv__mutmut_74 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_75'] = x_parse_csv__mutmut_75 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_76'] = x_parse_csv__mutmut_76 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_77'] = x_parse_csv__mutmut_77 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_78'] = x_parse_csv__mutmut_78 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_79'] = x_parse_csv__mutmut_79 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_80'] = x_parse_csv__mutmut_80 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_81'] = x_parse_csv__mutmut_81 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_82'] = x_parse_csv__mutmut_82 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_83'] = x_parse_csv__mutmut_83 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_84'] = x_parse_csv__mutmut_84 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_85'] = x_parse_csv__mutmut_85 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_86'] = x_parse_csv__mutmut_86 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_87'] = x_parse_csv__mutmut_87 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_88'] = x_parse_csv__mutmut_88 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_89'] = x_parse_csv__mutmut_89 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_90'] = x_parse_csv__mutmut_90 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_91'] = x_parse_csv__mutmut_91 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_92'] = x_parse_csv__mutmut_92 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_93'] = x_parse_csv__mutmut_93 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_94'] = x_parse_csv__mutmut_94 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_95'] = x_parse_csv__mutmut_95 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_96'] = x_parse_csv__mutmut_96 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_97'] = x_parse_csv__mutmut_97 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_98'] = x_parse_csv__mutmut_98 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_99'] = x_parse_csv__mutmut_99 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_100'] = x_parse_csv__mutmut_100 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_101'] = x_parse_csv__mutmut_101 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_102'] = x_parse_csv__mutmut_102 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_103'] = x_parse_csv__mutmut_103 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_104'] = x_parse_csv__mutmut_104 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_105'] = x_parse_csv__mutmut_105 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_106'] = x_parse_csv__mutmut_106 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_107'] = x_parse_csv__mutmut_107 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_108'] = x_parse_csv__mutmut_108 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_109'] = x_parse_csv__mutmut_109 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_110'] = x_parse_csv__mutmut_110 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_111'] = x_parse_csv__mutmut_111 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_112'] = x_parse_csv__mutmut_112 # type: ignore # mutmut generated
mutants_x_parse_csv__mutmut['x_parse_csv__mutmut_113'] = x_parse_csv__mutmut_113 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x_load_csv__mutmut)
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


def x_load_csv__mutmut_orig(path: str | Path) -> DataSafetyCsv:
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


def x_load_csv__mutmut_1(path: str | Path) -> DataSafetyCsv:
    """Read and parse an export from disk."""
    resolved = None
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


def x_load_csv__mutmut_2(path: str | Path) -> DataSafetyCsv:
    """Read and parse an export from disk."""
    resolved = Path(None).expanduser()
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


def x_load_csv__mutmut_3(path: str | Path) -> DataSafetyCsv:
    """Read and parse an export from disk."""
    resolved = Path(path).expanduser()
    if resolved.is_file():
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


def x_load_csv__mutmut_4(path: str | Path) -> DataSafetyCsv:
    """Read and parse an export from disk."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ValidationError(
            None,
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


def x_load_csv__mutmut_5(path: str | Path) -> DataSafetyCsv:
    """Read and parse an export from disk."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ValidationError(
            f"No Data Safety CSV at {resolved}.",
            remedy=None,
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


def x_load_csv__mutmut_6(path: str | Path) -> DataSafetyCsv:
    """Read and parse an export from disk."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ValidationError(
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


def x_load_csv__mutmut_7(path: str | Path) -> DataSafetyCsv:
    """Read and parse an export from disk."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ValidationError(
            f"No Data Safety CSV at {resolved}.",
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


def x_load_csv__mutmut_8(path: str | Path) -> DataSafetyCsv:
    """Read and parse an export from disk."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ValidationError(
            f"No Data Safety CSV at {resolved}.",
            remedy=_EXPORT_REMEDY,
        )
    size = None
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


def x_load_csv__mutmut_9(path: str | Path) -> DataSafetyCsv:
    """Read and parse an export from disk."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise ValidationError(
            f"No Data Safety CSV at {resolved}.",
            remedy=_EXPORT_REMEDY,
        )
    size = resolved.stat().st_size
    if size >= MAX_CSV_BYTES:
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


def x_load_csv__mutmut_10(path: str | Path) -> DataSafetyCsv:
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
            None,
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


def x_load_csv__mutmut_11(path: str | Path) -> DataSafetyCsv:
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
            remedy=None,
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"Could not read {resolved}: {exc}",
            remedy="Check the path and file permissions.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_12(path: str | Path) -> DataSafetyCsv:
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


def x_load_csv__mutmut_13(path: str | Path) -> DataSafetyCsv:
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
            )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"Could not read {resolved}: {exc}",
            remedy="Check the path and file permissions.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_14(path: str | Path) -> DataSafetyCsv:
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
            f"{resolved.name} is {size / 1024 * 1024:.1f} MB; a Data Safety export is well "
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


def x_load_csv__mutmut_15(path: str | Path) -> DataSafetyCsv:
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
            f"{resolved.name} is {size * 1024 / 1024:.1f} MB; a Data Safety export is well "
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


def x_load_csv__mutmut_16(path: str | Path) -> DataSafetyCsv:
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
            f"{resolved.name} is {size / 1025 / 1024:.1f} MB; a Data Safety export is well "
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


def x_load_csv__mutmut_17(path: str | Path) -> DataSafetyCsv:
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
            f"{resolved.name} is {size / 1024 / 1025:.1f} MB; a Data Safety export is well "
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


def x_load_csv__mutmut_18(path: str | Path) -> DataSafetyCsv:
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
            f"under {MAX_CSV_BYTES // 1024 / 1024} MB.",
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


def x_load_csv__mutmut_19(path: str | Path) -> DataSafetyCsv:
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
            f"under {MAX_CSV_BYTES / 1024 // 1024} MB.",
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


def x_load_csv__mutmut_20(path: str | Path) -> DataSafetyCsv:
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
            f"under {MAX_CSV_BYTES // 1025 // 1024} MB.",
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


def x_load_csv__mutmut_21(path: str | Path) -> DataSafetyCsv:
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
            f"under {MAX_CSV_BYTES // 1024 // 1025} MB.",
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


def x_load_csv__mutmut_22(path: str | Path) -> DataSafetyCsv:
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
            remedy="XXCheck this is the Data Safety CSV and not another file.XX",
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"Could not read {resolved}: {exc}",
            remedy="Check the path and file permissions.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_23(path: str | Path) -> DataSafetyCsv:
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
            remedy="check this is the data safety csv and not another file.",
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"Could not read {resolved}: {exc}",
            remedy="Check the path and file permissions.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_24(path: str | Path) -> DataSafetyCsv:
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
            remedy="CHECK THIS IS THE DATA SAFETY CSV AND NOT ANOTHER FILE.",
        )
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ValidationError(
            f"Could not read {resolved}: {exc}",
            remedy="Check the path and file permissions.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_25(path: str | Path) -> DataSafetyCsv:
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
        data = None
    except OSError as exc:
        raise ValidationError(
            f"Could not read {resolved}: {exc}",
            remedy="Check the path and file permissions.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_26(path: str | Path) -> DataSafetyCsv:
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
            None,
            remedy="Check the path and file permissions.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_27(path: str | Path) -> DataSafetyCsv:
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
            remedy=None,
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_28(path: str | Path) -> DataSafetyCsv:
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
            remedy="Check the path and file permissions.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_29(path: str | Path) -> DataSafetyCsv:
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
            ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_30(path: str | Path) -> DataSafetyCsv:
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
            remedy="XXCheck the path and file permissions.XX",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_31(path: str | Path) -> DataSafetyCsv:
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
            remedy="check the path and file permissions.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_32(path: str | Path) -> DataSafetyCsv:
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
            remedy="CHECK THE PATH AND FILE PERMISSIONS.",
        ) from exc
    return parse_csv(data, source_path=str(resolved))


def x_load_csv__mutmut_33(path: str | Path) -> DataSafetyCsv:
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
    return parse_csv(None, source_path=str(resolved))


def x_load_csv__mutmut_34(path: str | Path) -> DataSafetyCsv:
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
    return parse_csv(data, source_path=None)


def x_load_csv__mutmut_35(path: str | Path) -> DataSafetyCsv:
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
    return parse_csv(source_path=str(resolved))


def x_load_csv__mutmut_36(path: str | Path) -> DataSafetyCsv:
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
    return parse_csv(data, )


def x_load_csv__mutmut_37(path: str | Path) -> DataSafetyCsv:
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
    return parse_csv(data, source_path=str(None))

mutants_x_load_csv__mutmut['_mutmut_orig'] = x_load_csv__mutmut_orig # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_1'] = x_load_csv__mutmut_1 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_2'] = x_load_csv__mutmut_2 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_3'] = x_load_csv__mutmut_3 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_4'] = x_load_csv__mutmut_4 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_5'] = x_load_csv__mutmut_5 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_6'] = x_load_csv__mutmut_6 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_7'] = x_load_csv__mutmut_7 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_8'] = x_load_csv__mutmut_8 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_9'] = x_load_csv__mutmut_9 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_10'] = x_load_csv__mutmut_10 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_11'] = x_load_csv__mutmut_11 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_12'] = x_load_csv__mutmut_12 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_13'] = x_load_csv__mutmut_13 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_14'] = x_load_csv__mutmut_14 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_15'] = x_load_csv__mutmut_15 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_16'] = x_load_csv__mutmut_16 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_17'] = x_load_csv__mutmut_17 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_18'] = x_load_csv__mutmut_18 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_19'] = x_load_csv__mutmut_19 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_20'] = x_load_csv__mutmut_20 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_21'] = x_load_csv__mutmut_21 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_22'] = x_load_csv__mutmut_22 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_23'] = x_load_csv__mutmut_23 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_24'] = x_load_csv__mutmut_24 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_25'] = x_load_csv__mutmut_25 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_26'] = x_load_csv__mutmut_26 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_27'] = x_load_csv__mutmut_27 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_28'] = x_load_csv__mutmut_28 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_29'] = x_load_csv__mutmut_29 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_30'] = x_load_csv__mutmut_30 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_31'] = x_load_csv__mutmut_31 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_32'] = x_load_csv__mutmut_32 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_33'] = x_load_csv__mutmut_33 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_34'] = x_load_csv__mutmut_34 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_35'] = x_load_csv__mutmut_35 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_36'] = x_load_csv__mutmut_36 # type: ignore # mutmut generated
mutants_x_load_csv__mutmut['x_load_csv__mutmut_37'] = x_load_csv__mutmut_37 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x__header_errors__mutmut)
def _header_errors(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_orig(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_1(csv_file: DataSafetyCsv) -> list[str]:
    reader = None
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_2(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(None)
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_3(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(None, newline=""))
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_4(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=None))
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_5(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(newline=""))
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_6(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, ))
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_7(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline="XXXX"))
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_8(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = None
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_9(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(None, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_10(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_11(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(reader, )
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_12(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(reader, None)
    if header is not None:
        return ["CSV is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_13(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(reader, None)
    if header is None:
        return ["XXCSV is empty.XX"]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_14(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(reader, None)
    if header is None:
        return ["csv is empty."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_15(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(reader, None)
    if header is None:
        return ["CSV IS EMPTY."]
    problem = _headers_match(header)
    return [problem] if problem else []


def x__header_errors__mutmut_16(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = None
    return [problem] if problem else []


def x__header_errors__mutmut_17(csv_file: DataSafetyCsv) -> list[str]:
    reader = csv.reader(io.StringIO(csv_file.text, newline=""))
    header = next(reader, None)
    if header is None:
        return ["CSV is empty."]
    problem = _headers_match(None)
    return [problem] if problem else []

mutants_x__header_errors__mutmut['_mutmut_orig'] = x__header_errors__mutmut_orig # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_1'] = x__header_errors__mutmut_1 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_2'] = x__header_errors__mutmut_2 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_3'] = x__header_errors__mutmut_3 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_4'] = x__header_errors__mutmut_4 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_5'] = x__header_errors__mutmut_5 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_6'] = x__header_errors__mutmut_6 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_7'] = x__header_errors__mutmut_7 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_8'] = x__header_errors__mutmut_8 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_9'] = x__header_errors__mutmut_9 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_10'] = x__header_errors__mutmut_10 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_11'] = x__header_errors__mutmut_11 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_12'] = x__header_errors__mutmut_12 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_13'] = x__header_errors__mutmut_13 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_14'] = x__header_errors__mutmut_14 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_15'] = x__header_errors__mutmut_15 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_16'] = x__header_errors__mutmut_16 # type: ignore # mutmut generated
mutants_x__header_errors__mutmut['x__header_errors__mutmut_17'] = x__header_errors__mutmut_17 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x__row_errors__mutmut)
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


def x__row_errors__mutmut_orig(csv_file: DataSafetyCsv) -> list[str]:
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


def x__row_errors__mutmut_1(csv_file: DataSafetyCsv) -> list[str]:
    errors: list[str] = None
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


def x__row_errors__mutmut_2(csv_file: DataSafetyCsv) -> list[str]:
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = None
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


def x__row_errors__mutmut_3(csv_file: DataSafetyCsv) -> list[str]:
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for row in csv_file.rows:
        if row.question_id:
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


def x__row_errors__mutmut_4(csv_file: DataSafetyCsv) -> list[str]:
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for row in csv_file.rows:
        if not row.question_id:
            errors.append(None)
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


def x__row_errors__mutmut_5(csv_file: DataSafetyCsv) -> list[str]:
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for row in csv_file.rows:
        if not row.question_id:
            errors.append(f"Line {row.line} has no Question ID.")
            break
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


def x__row_errors__mutmut_6(csv_file: DataSafetyCsv) -> list[str]:
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for row in csv_file.rows:
        if not row.question_id:
            errors.append(f"Line {row.line} has no Question ID.")
            continue
        if row.value in RESPONSE_VALUES:
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


def x__row_errors__mutmut_7(csv_file: DataSafetyCsv) -> list[str]:
    errors: list[str] = []
    seen: dict[tuple[str, str], int] = {}
    for row in csv_file.rows:
        if not row.question_id:
            errors.append(f"Line {row.line} has no Question ID.")
            continue
        if row.value not in RESPONSE_VALUES:
            errors.append(
                None
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


def x__row_errors__mutmut_8(csv_file: DataSafetyCsv) -> list[str]:
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
            key = None
            if key in seen:
                errors.append(
                    f"Line {row.line} repeats {row.question_id} / {row.response_id}, "
                    f"already on line {seen[key]}."
                )
            else:
                seen[key] = row.line
    return errors


def x__row_errors__mutmut_9(csv_file: DataSafetyCsv) -> list[str]:
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
            if key not in seen:
                errors.append(
                    f"Line {row.line} repeats {row.question_id} / {row.response_id}, "
                    f"already on line {seen[key]}."
                )
            else:
                seen[key] = row.line
    return errors


def x__row_errors__mutmut_10(csv_file: DataSafetyCsv) -> list[str]:
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
                    None
                )
            else:
                seen[key] = row.line
    return errors


def x__row_errors__mutmut_11(csv_file: DataSafetyCsv) -> list[str]:
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
                seen[key] = None
    return errors

mutants_x__row_errors__mutmut['_mutmut_orig'] = x__row_errors__mutmut_orig # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_1'] = x__row_errors__mutmut_1 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_2'] = x__row_errors__mutmut_2 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_3'] = x__row_errors__mutmut_3 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_4'] = x__row_errors__mutmut_4 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_5'] = x__row_errors__mutmut_5 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_6'] = x__row_errors__mutmut_6 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_7'] = x__row_errors__mutmut_7 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_8'] = x__row_errors__mutmut_8 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_9'] = x__row_errors__mutmut_9 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_10'] = x__row_errors__mutmut_10 # type: ignore # mutmut generated
mutants_x__row_errors__mutmut['x__row_errors__mutmut_11'] = x__row_errors__mutmut_11 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x__question_findings__mutmut)
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


def x__question_findings__mutmut_orig(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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


def x__question_findings__mutmut_1(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Per-question errors and warnings.

    Only "a single-choice question picked two answers" is an error: that is
    invalid however the rest of the form is filled in. Everything else about
    cardinality depends on answers elsewhere in the file, so it is a warning.
    """
    errors: list[str] = None
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


def x__question_findings__mutmut_2(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Per-question errors and warnings.

    Only "a single-choice question picked two answers" is an error: that is
    invalid however the rest of the form is filled in. Everything else about
    cardinality depends on answers elsewhere in the file, so it is a warning.
    """
    errors: list[str] = []
    warnings: list[str] = None
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


def x__question_findings__mutmut_3(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Per-question errors and warnings.

    Only "a single-choice question picked two answers" is an error: that is
    invalid however the rest of the form is filled in. Everything else about
    cardinality depends on answers elsewhere in the file, so it is a warning.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for question in csv_file.questions:
        if question.requirement in ANSWER_REQUIREMENTS:
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


def x__question_findings__mutmut_4(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
                None
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


def x__question_findings__mutmut_5(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
            break
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


def x__question_findings__mutmut_6(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        if question.answered:
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


def x__question_findings__mutmut_7(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
            break
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


def x__question_findings__mutmut_8(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        chosen = None
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


def x__question_findings__mutmut_9(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        if question.requirement == "SINGLE_CHOICE" or len(chosen) > 1:
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


def x__question_findings__mutmut_10(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        if question.requirement != "SINGLE_CHOICE" and len(chosen) > 1:
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


def x__question_findings__mutmut_11(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        if question.requirement == "XXSINGLE_CHOICEXX" and len(chosen) > 1:
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


def x__question_findings__mutmut_12(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        if question.requirement == "single_choice" and len(chosen) > 1:
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


def x__question_findings__mutmut_13(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        if question.requirement == "SINGLE_CHOICE" and len(chosen) >= 1:
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


def x__question_findings__mutmut_14(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        if question.requirement == "SINGLE_CHOICE" and len(chosen) > 2:
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


def x__question_findings__mutmut_15(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
                None
            )
        elif question.requirement == "SINGLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is SINGLE_CHOICE and no response is TRUE.")
        elif question.requirement == "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_16(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
                f"TRUE ({', '.join(None)}); exactly one is allowed."
            )
        elif question.requirement == "SINGLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is SINGLE_CHOICE and no response is TRUE.")
        elif question.requirement == "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_17(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
                f"TRUE ({'XX, XX'.join(chosen)}); exactly one is allowed."
            )
        elif question.requirement == "SINGLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is SINGLE_CHOICE and no response is TRUE.")
        elif question.requirement == "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_18(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "SINGLE_CHOICE" or not chosen:
            warnings.append(f"{question.question_id} is SINGLE_CHOICE and no response is TRUE.")
        elif question.requirement == "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_19(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement != "SINGLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is SINGLE_CHOICE and no response is TRUE.")
        elif question.requirement == "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_20(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "XXSINGLE_CHOICEXX" and not chosen:
            warnings.append(f"{question.question_id} is SINGLE_CHOICE and no response is TRUE.")
        elif question.requirement == "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_21(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "single_choice" and not chosen:
            warnings.append(f"{question.question_id} is SINGLE_CHOICE and no response is TRUE.")
        elif question.requirement == "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_22(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "SINGLE_CHOICE" and chosen:
            warnings.append(f"{question.question_id} is SINGLE_CHOICE and no response is TRUE.")
        elif question.requirement == "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_23(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
            warnings.append(None)
        elif question.requirement == "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_24(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "MULTIPLE_CHOICE" or not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_25(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement != "MULTIPLE_CHOICE" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_26(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "XXMULTIPLE_CHOICEXX" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_27(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "multiple_choice" and not chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_28(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "MULTIPLE_CHOICE" and chosen:
            warnings.append(f"{question.question_id} is MULTIPLE_CHOICE and no response is TRUE.")
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_29(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
            warnings.append(None)
        elif question.requirement == "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_30(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "REQUIRED" or not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_31(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement != "REQUIRED" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_32(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "XXREQUIREDXX" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_33(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "required" and not chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_34(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        elif question.requirement == "REQUIRED" and chosen:
            warnings.append(f"{question.question_id} is REQUIRED and no response is TRUE.")
    return errors, warnings


def x__question_findings__mutmut_35(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
            warnings.append(None)
    return errors, warnings

mutants_x__question_findings__mutmut['_mutmut_orig'] = x__question_findings__mutmut_orig # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_1'] = x__question_findings__mutmut_1 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_2'] = x__question_findings__mutmut_2 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_3'] = x__question_findings__mutmut_3 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_4'] = x__question_findings__mutmut_4 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_5'] = x__question_findings__mutmut_5 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_6'] = x__question_findings__mutmut_6 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_7'] = x__question_findings__mutmut_7 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_8'] = x__question_findings__mutmut_8 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_9'] = x__question_findings__mutmut_9 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_10'] = x__question_findings__mutmut_10 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_11'] = x__question_findings__mutmut_11 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_12'] = x__question_findings__mutmut_12 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_13'] = x__question_findings__mutmut_13 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_14'] = x__question_findings__mutmut_14 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_15'] = x__question_findings__mutmut_15 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_16'] = x__question_findings__mutmut_16 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_17'] = x__question_findings__mutmut_17 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_18'] = x__question_findings__mutmut_18 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_19'] = x__question_findings__mutmut_19 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_20'] = x__question_findings__mutmut_20 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_21'] = x__question_findings__mutmut_21 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_22'] = x__question_findings__mutmut_22 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_23'] = x__question_findings__mutmut_23 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_24'] = x__question_findings__mutmut_24 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_25'] = x__question_findings__mutmut_25 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_26'] = x__question_findings__mutmut_26 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_27'] = x__question_findings__mutmut_27 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_28'] = x__question_findings__mutmut_28 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_29'] = x__question_findings__mutmut_29 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_30'] = x__question_findings__mutmut_30 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_31'] = x__question_findings__mutmut_31 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_32'] = x__question_findings__mutmut_32 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_33'] = x__question_findings__mutmut_33 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_34'] = x__question_findings__mutmut_34 # type: ignore # mutmut generated
mutants_x__question_findings__mutmut['x__question_findings__mutmut_35'] = x__question_findings__mutmut_35 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x__unknown_ids__mutmut)
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


def x__unknown_ids__mutmut_orig(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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


def x__unknown_ids__mutmut_1(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = None
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


def x__unknown_ids__mutmut_2(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = None
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


def x__unknown_ids__mutmut_3(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = [s for s in question.question_id.split(None) if s]
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


def x__unknown_ids__mutmut_4(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = [s for s in question.question_id.split("XX:XX") if s]
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


def x__unknown_ids__mutmut_5(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = [s for s in question.question_id.split(":") if s]
        if any(None):
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


def x__unknown_ids__mutmut_6(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = [s for s in question.question_id.split(":") if s]
        if any(s in KNOWN_ID_TOKENS for s in segments):
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


def x__unknown_ids__mutmut_7(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = [s for s in question.question_id.split(":") if s]
        if any(s not in KNOWN_ID_TOKENS for s in segments):
            unknown.append(None)

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


def x__unknown_ids__mutmut_8(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = [s for s in question.question_id.split(":") if s]
        if any(s not in KNOWN_ID_TOKENS for s in segments):
            unknown.append(question.question_id)

    if not unknown and not KNOWN_ID_TOKENS:
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


def x__unknown_ids__mutmut_9(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = [s for s in question.question_id.split(":") if s]
        if any(s not in KNOWN_ID_TOKENS for s in segments):
            unknown.append(question.question_id)

    if unknown or not KNOWN_ID_TOKENS:
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


def x__unknown_ids__mutmut_10(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = [s for s in question.question_id.split(":") if s]
        if any(s not in KNOWN_ID_TOKENS for s in segments):
            unknown.append(question.question_id)

    if not unknown or KNOWN_ID_TOKENS:
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


def x__unknown_ids__mutmut_11(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
    """Question ids whose segments are outside the snapshot, and how to report them."""
    unknown: list[str] = []
    for question in csv_file.questions:
        segments = [s for s in question.question_id.split(":") if s]
        if any(s not in KNOWN_ID_TOKENS for s in segments):
            unknown.append(question.question_id)

    if not unknown or not KNOWN_ID_TOKENS:
        # An empty snapshot means silence, not a warning per question.
        return unknown, []

    total = None
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


def x__unknown_ids__mutmut_12(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
    qualifier = None
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


def x__unknown_ids__mutmut_13(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
    qualifier = "XXthe partial snapshotXX" if SNAPSHOT_PARTIAL else "the snapshot"
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


def x__unknown_ids__mutmut_14(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
    qualifier = "THE PARTIAL SNAPSHOT" if SNAPSHOT_PARTIAL else "the snapshot"
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


def x__unknown_ids__mutmut_15(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
    qualifier = "the partial snapshot" if SNAPSHOT_PARTIAL else "XXthe snapshotXX"
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


def x__unknown_ids__mutmut_16(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
    qualifier = "the partial snapshot" if SNAPSHOT_PARTIAL else "THE SNAPSHOT"
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


def x__unknown_ids__mutmut_17(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        and total or len(unknown) / total > _UNKNOWN_RATIO_SUPPRESS
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


def x__unknown_ids__mutmut_18(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        len(unknown) > _UNKNOWN_SUPPRESS_FLOOR or total
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


def x__unknown_ids__mutmut_19(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        len(unknown) >= _UNKNOWN_SUPPRESS_FLOOR
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


def x__unknown_ids__mutmut_20(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        and len(unknown) * total > _UNKNOWN_RATIO_SUPPRESS
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


def x__unknown_ids__mutmut_21(csv_file: DataSafetyCsv) -> tuple[list[str], list[str]]:
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
        and len(unknown) / total >= _UNKNOWN_RATIO_SUPPRESS
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

mutants_x__unknown_ids__mutmut['_mutmut_orig'] = x__unknown_ids__mutmut_orig # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_1'] = x__unknown_ids__mutmut_1 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_2'] = x__unknown_ids__mutmut_2 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_3'] = x__unknown_ids__mutmut_3 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_4'] = x__unknown_ids__mutmut_4 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_5'] = x__unknown_ids__mutmut_5 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_6'] = x__unknown_ids__mutmut_6 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_7'] = x__unknown_ids__mutmut_7 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_8'] = x__unknown_ids__mutmut_8 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_9'] = x__unknown_ids__mutmut_9 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_10'] = x__unknown_ids__mutmut_10 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_11'] = x__unknown_ids__mutmut_11 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_12'] = x__unknown_ids__mutmut_12 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_13'] = x__unknown_ids__mutmut_13 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_14'] = x__unknown_ids__mutmut_14 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_15'] = x__unknown_ids__mutmut_15 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_16'] = x__unknown_ids__mutmut_16 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_17'] = x__unknown_ids__mutmut_17 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_18'] = x__unknown_ids__mutmut_18 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_19'] = x__unknown_ids__mutmut_19 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_20'] = x__unknown_ids__mutmut_20 # type: ignore # mutmut generated
mutants_x__unknown_ids__mutmut['x__unknown_ids__mutmut_21'] = x__unknown_ids__mutmut_21 # type: ignore # mutmut generated
mutants_x_validate__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x_validate__mutmut)
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


def x_validate__mutmut_orig(csv_file: DataSafetyCsv) -> dict[str, Any]:
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


def x_validate__mutmut_1(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = None
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


def x_validate__mutmut_2(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(None)
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


def x_validate__mutmut_3(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = None
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


def x_validate__mutmut_4(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = None

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


def x_validate__mutmut_5(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if errors:
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


def x_validate__mutmut_6(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if csv_file.rows:
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


def x_validate__mutmut_7(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if not csv_file.rows:
            errors.append(None)
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


def x_validate__mutmut_8(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if not csv_file.rows:
            errors.append("XXCSV has a header but no response rows.XX")
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


def x_validate__mutmut_9(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if not csv_file.rows:
            errors.append("csv has a header but no response rows.")
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


def x_validate__mutmut_10(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if not csv_file.rows:
            errors.append("CSV HAS A HEADER BUT NO RESPONSE ROWS.")
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


def x_validate__mutmut_11(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if not csv_file.rows:
            errors.append("CSV has a header but no response rows.")
        else:
            errors = _row_errors(csv_file)
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


def x_validate__mutmut_12(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if not csv_file.rows:
            errors.append("CSV has a header but no response rows.")
        else:
            errors -= _row_errors(csv_file)
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


def x_validate__mutmut_13(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if not csv_file.rows:
            errors.append("CSV has a header but no response rows.")
        else:
            errors += _row_errors(None)
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


def x_validate__mutmut_14(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if not csv_file.rows:
            errors.append("CSV has a header but no response rows.")
        else:
            errors += _row_errors(csv_file)
            question_errors, question_warnings = None
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


def x_validate__mutmut_15(csv_file: DataSafetyCsv) -> dict[str, Any]:
    """Everything wrong with an export, split into what blocks and what does not."""
    errors = _header_errors(csv_file)
    warnings: list[str] = []
    unknown: list[str] = []

    if not errors:
        if not csv_file.rows:
            errors.append("CSV has a header but no response rows.")
        else:
            errors += _row_errors(csv_file)
            question_errors, question_warnings = _question_findings(None)
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


def x_validate__mutmut_16(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            errors = question_errors
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


def x_validate__mutmut_17(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            errors -= question_errors
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


def x_validate__mutmut_18(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            warnings = question_warnings
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


def x_validate__mutmut_19(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            warnings -= question_warnings
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


def x_validate__mutmut_20(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            unknown, id_warnings = None
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


def x_validate__mutmut_21(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            unknown, id_warnings = _unknown_ids(None)
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


def x_validate__mutmut_22(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            warnings = id_warnings

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


def x_validate__mutmut_23(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            warnings -= id_warnings

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


def x_validate__mutmut_24(csv_file: DataSafetyCsv) -> dict[str, Any]:
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

    if csv_file.encoding != "utf-8-sig":
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


def x_validate__mutmut_25(csv_file: DataSafetyCsv) -> dict[str, Any]:
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

    if csv_file.encoding == "XXutf-8-sigXX":
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


def x_validate__mutmut_26(csv_file: DataSafetyCsv) -> dict[str, Any]:
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

    if csv_file.encoding == "UTF-8-SIG":
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


def x_validate__mutmut_27(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        warnings.append(None)
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


def x_validate__mutmut_28(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        warnings.append("XXA UTF-8 BOM was present; it is stripped from the text sent to Play.XX")
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


def x_validate__mutmut_29(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        warnings.append("a utf-8 bom was present; it is stripped from the text sent to play.")
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


def x_validate__mutmut_30(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        warnings.append("A UTF-8 BOM WAS PRESENT; IT IS STRIPPED FROM THE TEXT SENT TO PLAY.")
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


def x_validate__mutmut_31(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    if csv_file.line_ending != "mixed":
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


def x_validate__mutmut_32(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    if csv_file.line_ending == "XXmixedXX":
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


def x_validate__mutmut_33(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    if csv_file.line_ending == "MIXED":
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


def x_validate__mutmut_34(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        warnings.append(None)

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


def x_validate__mutmut_35(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        warnings.append("XXLine endings are mixed; the file is sent unchanged.XX")

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


def x_validate__mutmut_36(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        warnings.append("line endings are mixed; the file is sent unchanged.")

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


def x_validate__mutmut_37(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        warnings.append("LINE ENDINGS ARE MIXED; THE FILE IS SENT UNCHANGED.")

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


def x_validate__mutmut_38(csv_file: DataSafetyCsv) -> dict[str, Any]:
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

    requirements = None
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


def x_validate__mutmut_39(csv_file: DataSafetyCsv) -> dict[str, Any]:
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

    requirements = Counter(None)
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


def x_validate__mutmut_40(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    maybe_blank = None
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


def x_validate__mutmut_41(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    maybe_blank = sum(None)
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


def x_validate__mutmut_42(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    maybe_blank = sum(2 for r in csv_file.rows if r.requirement == "MAYBE_REQUIRED" and not r.value)
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


def x_validate__mutmut_43(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    maybe_blank = sum(1 for r in csv_file.rows if r.requirement == "MAYBE_REQUIRED" or not r.value)
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


def x_validate__mutmut_44(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    maybe_blank = sum(1 for r in csv_file.rows if r.requirement != "MAYBE_REQUIRED" and not r.value)
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


def x_validate__mutmut_45(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    maybe_blank = sum(1 for r in csv_file.rows if r.requirement == "XXMAYBE_REQUIREDXX" and not r.value)
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


def x_validate__mutmut_46(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    maybe_blank = sum(1 for r in csv_file.rows if r.requirement == "maybe_required" and not r.value)
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


def x_validate__mutmut_47(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
    maybe_blank = sum(1 for r in csv_file.rows if r.requirement == "MAYBE_REQUIRED" and r.value)
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


def x_validate__mutmut_48(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "XXvalidXX": not errors,
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


def x_validate__mutmut_49(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "VALID": not errors,
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


def x_validate__mutmut_50(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "valid": errors,
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


def x_validate__mutmut_51(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "XXerrorsXX": errors,
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


def x_validate__mutmut_52(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "ERRORS": errors,
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


def x_validate__mutmut_53(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "XXwarningsXX": warnings,
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


def x_validate__mutmut_54(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "WARNINGS": warnings,
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


def x_validate__mutmut_55(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "XXunknown_question_idsXX": unknown,
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


def x_validate__mutmut_56(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "UNKNOWN_QUESTION_IDS": unknown,
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


def x_validate__mutmut_57(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "XXsnapshotXX": {
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


def x_validate__mutmut_58(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "SNAPSHOT": {
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


def x_validate__mutmut_59(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "XXdateXX": SNAPSHOT_DATE,
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


def x_validate__mutmut_60(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "DATE": SNAPSHOT_DATE,
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


def x_validate__mutmut_61(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "XXsizeXX": len(KNOWN_ID_TOKENS),
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


def x_validate__mutmut_62(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "SIZE": len(KNOWN_ID_TOKENS),
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


def x_validate__mutmut_63(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "XXpartialXX": SNAPSHOT_PARTIAL,
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


def x_validate__mutmut_64(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "PARTIAL": SNAPSHOT_PARTIAL,
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


def x_validate__mutmut_65(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "XXstatsXX": {
            "rows": len(csv_file.rows),
            "questions": len(csv_file.questions),
            "answered_true": sum(1 for r in csv_file.rows if r.value == "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_66(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
        "STATS": {
            "rows": len(csv_file.rows),
            "questions": len(csv_file.questions),
            "answered_true": sum(1 for r in csv_file.rows if r.value == "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_67(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "XXrowsXX": len(csv_file.rows),
            "questions": len(csv_file.questions),
            "answered_true": sum(1 for r in csv_file.rows if r.value == "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_68(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "ROWS": len(csv_file.rows),
            "questions": len(csv_file.questions),
            "answered_true": sum(1 for r in csv_file.rows if r.value == "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_69(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "XXquestionsXX": len(csv_file.questions),
            "answered_true": sum(1 for r in csv_file.rows if r.value == "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_70(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "QUESTIONS": len(csv_file.questions),
            "answered_true": sum(1 for r in csv_file.rows if r.value == "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_71(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "XXanswered_trueXX": sum(1 for r in csv_file.rows if r.value == "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_72(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "ANSWERED_TRUE": sum(1 for r in csv_file.rows if r.value == "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_73(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "answered_true": sum(None),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_74(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "answered_true": sum(2 for r in csv_file.rows if r.value == "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_75(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "answered_true": sum(1 for r in csv_file.rows if r.value != "TRUE"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_76(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "answered_true": sum(1 for r in csv_file.rows if r.value == "XXTRUEXX"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_77(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "answered_true": sum(1 for r in csv_file.rows if r.value == "true"),
            "questions_answered": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_78(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "XXquestions_answeredXX": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_79(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "QUESTIONS_ANSWERED": sum(1 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_80(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "questions_answered": sum(None),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_81(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "questions_answered": sum(2 for q in csv_file.questions if q.answered),
            "maybe_required_blank": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_82(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "XXmaybe_required_blankXX": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_83(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "MAYBE_REQUIRED_BLANK": maybe_blank,
            "by_requirement": dict(requirements),
        },
    }


def x_validate__mutmut_84(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "XXby_requirementXX": dict(requirements),
        },
    }


def x_validate__mutmut_85(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "BY_REQUIREMENT": dict(requirements),
        },
    }


def x_validate__mutmut_86(csv_file: DataSafetyCsv) -> dict[str, Any]:
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
            "by_requirement": dict(None),
        },
    }

mutants_x_validate__mutmut['_mutmut_orig'] = x_validate__mutmut_orig # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_1'] = x_validate__mutmut_1 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_2'] = x_validate__mutmut_2 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_3'] = x_validate__mutmut_3 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_4'] = x_validate__mutmut_4 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_5'] = x_validate__mutmut_5 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_6'] = x_validate__mutmut_6 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_7'] = x_validate__mutmut_7 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_8'] = x_validate__mutmut_8 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_9'] = x_validate__mutmut_9 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_10'] = x_validate__mutmut_10 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_11'] = x_validate__mutmut_11 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_12'] = x_validate__mutmut_12 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_13'] = x_validate__mutmut_13 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_14'] = x_validate__mutmut_14 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_15'] = x_validate__mutmut_15 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_16'] = x_validate__mutmut_16 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_17'] = x_validate__mutmut_17 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_18'] = x_validate__mutmut_18 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_19'] = x_validate__mutmut_19 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_20'] = x_validate__mutmut_20 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_21'] = x_validate__mutmut_21 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_22'] = x_validate__mutmut_22 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_23'] = x_validate__mutmut_23 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_24'] = x_validate__mutmut_24 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_25'] = x_validate__mutmut_25 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_26'] = x_validate__mutmut_26 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_27'] = x_validate__mutmut_27 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_28'] = x_validate__mutmut_28 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_29'] = x_validate__mutmut_29 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_30'] = x_validate__mutmut_30 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_31'] = x_validate__mutmut_31 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_32'] = x_validate__mutmut_32 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_33'] = x_validate__mutmut_33 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_34'] = x_validate__mutmut_34 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_35'] = x_validate__mutmut_35 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_36'] = x_validate__mutmut_36 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_37'] = x_validate__mutmut_37 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_38'] = x_validate__mutmut_38 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_39'] = x_validate__mutmut_39 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_40'] = x_validate__mutmut_40 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_41'] = x_validate__mutmut_41 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_42'] = x_validate__mutmut_42 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_43'] = x_validate__mutmut_43 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_44'] = x_validate__mutmut_44 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_45'] = x_validate__mutmut_45 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_46'] = x_validate__mutmut_46 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_47'] = x_validate__mutmut_47 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_48'] = x_validate__mutmut_48 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_49'] = x_validate__mutmut_49 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_50'] = x_validate__mutmut_50 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_51'] = x_validate__mutmut_51 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_52'] = x_validate__mutmut_52 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_53'] = x_validate__mutmut_53 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_54'] = x_validate__mutmut_54 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_55'] = x_validate__mutmut_55 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_56'] = x_validate__mutmut_56 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_57'] = x_validate__mutmut_57 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_58'] = x_validate__mutmut_58 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_59'] = x_validate__mutmut_59 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_60'] = x_validate__mutmut_60 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_61'] = x_validate__mutmut_61 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_62'] = x_validate__mutmut_62 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_63'] = x_validate__mutmut_63 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_64'] = x_validate__mutmut_64 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_65'] = x_validate__mutmut_65 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_66'] = x_validate__mutmut_66 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_67'] = x_validate__mutmut_67 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_68'] = x_validate__mutmut_68 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_69'] = x_validate__mutmut_69 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_70'] = x_validate__mutmut_70 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_71'] = x_validate__mutmut_71 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_72'] = x_validate__mutmut_72 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_73'] = x_validate__mutmut_73 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_74'] = x_validate__mutmut_74 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_75'] = x_validate__mutmut_75 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_76'] = x_validate__mutmut_76 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_77'] = x_validate__mutmut_77 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_78'] = x_validate__mutmut_78 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_79'] = x_validate__mutmut_79 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_80'] = x_validate__mutmut_80 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_81'] = x_validate__mutmut_81 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_82'] = x_validate__mutmut_82 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_83'] = x_validate__mutmut_83 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_84'] = x_validate__mutmut_84 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_85'] = x_validate__mutmut_85 # type: ignore # mutmut generated
mutants_x_validate__mutmut['x_validate__mutmut_86'] = x_validate__mutmut_86 # type: ignore # mutmut generated
mutants_x_declared__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x_declared__mutmut)
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


def x_declared__mutmut_orig(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
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


def x_declared__mutmut_1(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
    """Only the questions answered YES — the declaration a human needs to read.

    A full export runs to a few hundred questions, nearly all of them answered
    no or left blank. Returning all of them buries the handful that actually say
    something about the app.
    """
    return [
        {
            "XXquestion_idXX": q.question_id,
            "label": q.label,
            "requirement": q.requirement,
            "selected": list(q.selected),
        }
        for q in csv_file.questions
        if q.selected
    ]


def x_declared__mutmut_2(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
    """Only the questions answered YES — the declaration a human needs to read.

    A full export runs to a few hundred questions, nearly all of them answered
    no or left blank. Returning all of them buries the handful that actually say
    something about the app.
    """
    return [
        {
            "QUESTION_ID": q.question_id,
            "label": q.label,
            "requirement": q.requirement,
            "selected": list(q.selected),
        }
        for q in csv_file.questions
        if q.selected
    ]


def x_declared__mutmut_3(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
    """Only the questions answered YES — the declaration a human needs to read.

    A full export runs to a few hundred questions, nearly all of them answered
    no or left blank. Returning all of them buries the handful that actually say
    something about the app.
    """
    return [
        {
            "question_id": q.question_id,
            "XXlabelXX": q.label,
            "requirement": q.requirement,
            "selected": list(q.selected),
        }
        for q in csv_file.questions
        if q.selected
    ]


def x_declared__mutmut_4(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
    """Only the questions answered YES — the declaration a human needs to read.

    A full export runs to a few hundred questions, nearly all of them answered
    no or left blank. Returning all of them buries the handful that actually say
    something about the app.
    """
    return [
        {
            "question_id": q.question_id,
            "LABEL": q.label,
            "requirement": q.requirement,
            "selected": list(q.selected),
        }
        for q in csv_file.questions
        if q.selected
    ]


def x_declared__mutmut_5(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
    """Only the questions answered YES — the declaration a human needs to read.

    A full export runs to a few hundred questions, nearly all of them answered
    no or left blank. Returning all of them buries the handful that actually say
    something about the app.
    """
    return [
        {
            "question_id": q.question_id,
            "label": q.label,
            "XXrequirementXX": q.requirement,
            "selected": list(q.selected),
        }
        for q in csv_file.questions
        if q.selected
    ]


def x_declared__mutmut_6(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
    """Only the questions answered YES — the declaration a human needs to read.

    A full export runs to a few hundred questions, nearly all of them answered
    no or left blank. Returning all of them buries the handful that actually say
    something about the app.
    """
    return [
        {
            "question_id": q.question_id,
            "label": q.label,
            "REQUIREMENT": q.requirement,
            "selected": list(q.selected),
        }
        for q in csv_file.questions
        if q.selected
    ]


def x_declared__mutmut_7(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
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
            "XXselectedXX": list(q.selected),
        }
        for q in csv_file.questions
        if q.selected
    ]


def x_declared__mutmut_8(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
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
            "SELECTED": list(q.selected),
        }
        for q in csv_file.questions
        if q.selected
    ]


def x_declared__mutmut_9(csv_file: DataSafetyCsv) -> list[dict[str, Any]]:
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
            "selected": list(None),
        }
        for q in csv_file.questions
        if q.selected
    ]

mutants_x_declared__mutmut['_mutmut_orig'] = x_declared__mutmut_orig # type: ignore # mutmut generated
mutants_x_declared__mutmut['x_declared__mutmut_1'] = x_declared__mutmut_1 # type: ignore # mutmut generated
mutants_x_declared__mutmut['x_declared__mutmut_2'] = x_declared__mutmut_2 # type: ignore # mutmut generated
mutants_x_declared__mutmut['x_declared__mutmut_3'] = x_declared__mutmut_3 # type: ignore # mutmut generated
mutants_x_declared__mutmut['x_declared__mutmut_4'] = x_declared__mutmut_4 # type: ignore # mutmut generated
mutants_x_declared__mutmut['x_declared__mutmut_5'] = x_declared__mutmut_5 # type: ignore # mutmut generated
mutants_x_declared__mutmut['x_declared__mutmut_6'] = x_declared__mutmut_6 # type: ignore # mutmut generated
mutants_x_declared__mutmut['x_declared__mutmut_7'] = x_declared__mutmut_7 # type: ignore # mutmut generated
mutants_x_declared__mutmut['x_declared__mutmut_8'] = x_declared__mutmut_8 # type: ignore # mutmut generated
mutants_x_declared__mutmut['x_declared__mutmut_9'] = x_declared__mutmut_9 # type: ignore # mutmut generated
mutants_x_build_request__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x_build_request__mutmut)
def build_request(csv_file: DataSafetyCsv) -> dict[str, str]:
    """The ``SafetyLabelsUpdateRequest`` body for this export."""
    return {"safetyLabels": csv_file.text}


def x_build_request__mutmut_orig(csv_file: DataSafetyCsv) -> dict[str, str]:
    """The ``SafetyLabelsUpdateRequest`` body for this export."""
    return {"safetyLabels": csv_file.text}


def x_build_request__mutmut_1(csv_file: DataSafetyCsv) -> dict[str, str]:
    """The ``SafetyLabelsUpdateRequest`` body for this export."""
    return {"XXsafetyLabelsXX": csv_file.text}


def x_build_request__mutmut_2(csv_file: DataSafetyCsv) -> dict[str, str]:
    """The ``SafetyLabelsUpdateRequest`` body for this export."""
    return {"safetylabels": csv_file.text}


def x_build_request__mutmut_3(csv_file: DataSafetyCsv) -> dict[str, str]:
    """The ``SafetyLabelsUpdateRequest`` body for this export."""
    return {"SAFETYLABELS": csv_file.text}

mutants_x_build_request__mutmut['_mutmut_orig'] = x_build_request__mutmut_orig # type: ignore # mutmut generated
mutants_x_build_request__mutmut['x_build_request__mutmut_1'] = x_build_request__mutmut_1 # type: ignore # mutmut generated
mutants_x_build_request__mutmut['x_build_request__mutmut_2'] = x_build_request__mutmut_2 # type: ignore # mutmut generated
mutants_x_build_request__mutmut['x_build_request__mutmut_3'] = x_build_request__mutmut_3 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut: MutantDict = {}  # type: ignore


@_mutmut_mutated(mutants_x_preflight_csv__mutmut)
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


def x_preflight_csv__mutmut_orig(path: str | Path) -> dict[str, Any]:
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


def x_preflight_csv__mutmut_1(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = None
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


def x_preflight_csv__mutmut_2(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(None)
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


def x_preflight_csv__mutmut_3(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = None
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


def x_preflight_csv__mutmut_4(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(None)
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


def x_preflight_csv__mutmut_5(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = None
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


def x_preflight_csv__mutmut_6(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(None)
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


def x_preflight_csv__mutmut_7(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "XXvalidXX": report["valid"],
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


def x_preflight_csv__mutmut_8(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "VALID": report["valid"],
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


def x_preflight_csv__mutmut_9(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["XXvalidXX"],
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


def x_preflight_csv__mutmut_10(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["VALID"],
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


def x_preflight_csv__mutmut_11(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["valid"],
        "XXpathXX": csv_file.source_path,
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


def x_preflight_csv__mutmut_12(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["valid"],
        "PATH": csv_file.source_path,
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


def x_preflight_csv__mutmut_13(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["valid"],
        "path": csv_file.source_path,
        "XXdetectedXX": {
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


def x_preflight_csv__mutmut_14(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["valid"],
        "path": csv_file.source_path,
        "DETECTED": {
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


def x_preflight_csv__mutmut_15(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["valid"],
        "path": csv_file.source_path,
        "detected": {
            "XXencodingXX": csv_file.encoding,
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


def x_preflight_csv__mutmut_16(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["valid"],
        "path": csv_file.source_path,
        "detected": {
            "ENCODING": csv_file.encoding,
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


def x_preflight_csv__mutmut_17(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["valid"],
        "path": csv_file.source_path,
        "detected": {
            "encoding": csv_file.encoding,
            "XXline_endingXX": csv_file.line_ending,
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


def x_preflight_csv__mutmut_18(path: str | Path) -> dict[str, Any]:
    """Check a Data Safety CSV without sending it anywhere."""
    csv_file = load_csv(path)
    report = validate(csv_file)
    declaration = declared(csv_file)
    return {
        "valid": report["valid"],
        "path": csv_file.source_path,
        "detected": {
            "encoding": csv_file.encoding,
            "LINE_ENDING": csv_file.line_ending,
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


def x_preflight_csv__mutmut_19(path: str | Path) -> dict[str, Any]:
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
            "XXbyte_lengthXX": csv_file.byte_length,
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


def x_preflight_csv__mutmut_20(path: str | Path) -> dict[str, Any]:
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
            "BYTE_LENGTH": csv_file.byte_length,
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


def x_preflight_csv__mutmut_21(path: str | Path) -> dict[str, Any]:
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
            "XXsha256XX": csv_file.sha256,
            **report["stats"],
        },
        "errors": report["errors"],
        "warnings": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_22(path: str | Path) -> dict[str, Any]:
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
            "SHA256": csv_file.sha256,
            **report["stats"],
        },
        "errors": report["errors"],
        "warnings": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_23(path: str | Path) -> dict[str, Any]:
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
            **report["XXstatsXX"],
        },
        "errors": report["errors"],
        "warnings": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_24(path: str | Path) -> dict[str, Any]:
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
            **report["STATS"],
        },
        "errors": report["errors"],
        "warnings": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_25(path: str | Path) -> dict[str, Any]:
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
        "XXerrorsXX": report["errors"],
        "warnings": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_26(path: str | Path) -> dict[str, Any]:
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
        "ERRORS": report["errors"],
        "warnings": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_27(path: str | Path) -> dict[str, Any]:
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
        "errors": report["XXerrorsXX"],
        "warnings": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_28(path: str | Path) -> dict[str, Any]:
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
        "errors": report["ERRORS"],
        "warnings": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_29(path: str | Path) -> dict[str, Any]:
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
        "XXwarningsXX": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_30(path: str | Path) -> dict[str, Any]:
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
        "WARNINGS": report["warnings"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_31(path: str | Path) -> dict[str, Any]:
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
        "warnings": report["XXwarningsXX"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_32(path: str | Path) -> dict[str, Any]:
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
        "warnings": report["WARNINGS"],
        "unknown_question_ids": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_33(path: str | Path) -> dict[str, Any]:
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
        "XXunknown_question_idsXX": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_34(path: str | Path) -> dict[str, Any]:
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
        "UNKNOWN_QUESTION_IDS": report["unknown_question_ids"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_35(path: str | Path) -> dict[str, Any]:
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
        "unknown_question_ids": report["XXunknown_question_idsXX"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_36(path: str | Path) -> dict[str, Any]:
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
        "unknown_question_ids": report["UNKNOWN_QUESTION_IDS"],
        "snapshot": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_37(path: str | Path) -> dict[str, Any]:
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
        "XXsnapshotXX": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_38(path: str | Path) -> dict[str, Any]:
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
        "SNAPSHOT": report["snapshot"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_39(path: str | Path) -> dict[str, Any]:
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
        "snapshot": report["XXsnapshotXX"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_40(path: str | Path) -> dict[str, Any]:
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
        "snapshot": report["SNAPSHOT"],
        "declared": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_41(path: str | Path) -> dict[str, Any]:
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
        "XXdeclaredXX": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_42(path: str | Path) -> dict[str, Any]:
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
        "DECLARED": declaration,
        "questions_not_declared": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_43(path: str | Path) -> dict[str, Any]:
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
        "XXquestions_not_declaredXX": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_44(path: str | Path) -> dict[str, Any]:
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
        "QUESTIONS_NOT_DECLARED": len(csv_file.questions) - len(declaration),
    }


def x_preflight_csv__mutmut_45(path: str | Path) -> dict[str, Any]:
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
        "questions_not_declared": len(csv_file.questions) + len(declaration),
    }

mutants_x_preflight_csv__mutmut['_mutmut_orig'] = x_preflight_csv__mutmut_orig # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_1'] = x_preflight_csv__mutmut_1 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_2'] = x_preflight_csv__mutmut_2 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_3'] = x_preflight_csv__mutmut_3 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_4'] = x_preflight_csv__mutmut_4 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_5'] = x_preflight_csv__mutmut_5 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_6'] = x_preflight_csv__mutmut_6 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_7'] = x_preflight_csv__mutmut_7 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_8'] = x_preflight_csv__mutmut_8 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_9'] = x_preflight_csv__mutmut_9 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_10'] = x_preflight_csv__mutmut_10 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_11'] = x_preflight_csv__mutmut_11 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_12'] = x_preflight_csv__mutmut_12 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_13'] = x_preflight_csv__mutmut_13 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_14'] = x_preflight_csv__mutmut_14 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_15'] = x_preflight_csv__mutmut_15 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_16'] = x_preflight_csv__mutmut_16 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_17'] = x_preflight_csv__mutmut_17 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_18'] = x_preflight_csv__mutmut_18 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_19'] = x_preflight_csv__mutmut_19 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_20'] = x_preflight_csv__mutmut_20 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_21'] = x_preflight_csv__mutmut_21 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_22'] = x_preflight_csv__mutmut_22 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_23'] = x_preflight_csv__mutmut_23 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_24'] = x_preflight_csv__mutmut_24 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_25'] = x_preflight_csv__mutmut_25 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_26'] = x_preflight_csv__mutmut_26 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_27'] = x_preflight_csv__mutmut_27 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_28'] = x_preflight_csv__mutmut_28 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_29'] = x_preflight_csv__mutmut_29 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_30'] = x_preflight_csv__mutmut_30 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_31'] = x_preflight_csv__mutmut_31 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_32'] = x_preflight_csv__mutmut_32 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_33'] = x_preflight_csv__mutmut_33 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_34'] = x_preflight_csv__mutmut_34 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_35'] = x_preflight_csv__mutmut_35 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_36'] = x_preflight_csv__mutmut_36 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_37'] = x_preflight_csv__mutmut_37 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_38'] = x_preflight_csv__mutmut_38 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_39'] = x_preflight_csv__mutmut_39 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_40'] = x_preflight_csv__mutmut_40 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_41'] = x_preflight_csv__mutmut_41 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_42'] = x_preflight_csv__mutmut_42 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_43'] = x_preflight_csv__mutmut_43 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_44'] = x_preflight_csv__mutmut_44 # type: ignore # mutmut generated
mutants_x_preflight_csv__mutmut['x_preflight_csv__mutmut_45'] = x_preflight_csv__mutmut_45 # type: ignore # mutmut generated
