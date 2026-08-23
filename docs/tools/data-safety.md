# Data Safety

The Data Safety form is a declaration to users and to Google about what an app
collects and shares. An inaccurate one is a policy violation, and Play can
remove the app over it.

## The one thing to know first

`applications.dataSafety` is **write-only**. There is no API — here or in Play —
that reads the current declaration back.

Three consequences follow, and they shape every tool on this page:

1. The CSV can only come from **Play Console > App content > Data safety >
   Export to CSV**. Nothing can generate it for you.
2. `set_data_safety` replaces the **whole** declaration, not the fields you
   changed.
3. The exported file is therefore the only rollback that will ever exist.
   Export one before you change anything. Without it, the write is not
   reversible.

The `declare_data_safety` prompt walks this path in order.

## The CSV format

Five columns, in this order:

| Column | Contents |
|---|---|
| `Question ID (machine readable)` | e.g. `PSL_DATA_TYPES_PERSONAL`, or a composite `PSL_DATA_USAGE_RESPONSES:PSL_NAME:DATA_USAGE_COLLECTION_PURPOSE` |
| `Response ID (machine readable)` | The choice within that question; blank for free-text questions |
| `Response value` | `true`, `false`, or blank — **either case** |
| `Answer requirement` | `REQUIRED`, `MAYBE_REQUIRED`, `OPTIONAL`, `SINGLE_CHOICE`, `MULTIPLE_CHOICE` |
| `Human-friendly question label` | The question as the Console shows it |

One row per response option, so a question spans as many rows as it has
choices, and rows are grouped by the question ID. A real export runs to roughly
780 rows across 217 questions.

Google's Help Center spells the second column `Response (machine readable)`;
real exports say `Response ID (machine readable)`. Both are accepted.

Google's Help Center says to "enter TRUE or FALSE". Real exports disagree: of
six sampled from published apps between 2022 and 2026, five write lowercase
`true`/`false` and one writes upper. Both are accepted; only a value that is not
a boolean at all (`Yes`, `1`) is an error.

**Avoid opening the file in a spreadsheet.** A round trip through Excel or
Sheets can re-encode it as UTF-16, which is rejected — silently rewriting the
bytes of a legal declaration is exactly what you do not want.

## preflight_data_safety_csv

Check an export locally. **Read-only.** No network.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `csv_path` | string | Yes | Path to a CSV exported from Play Console |

```python
preflight_data_safety_csv(csv_path="~/Downloads/data-safety.csv")
```

Returns `valid`, `errors`, `warnings`, the detected encoding/line endings/size/
SHA-256, `declared` (the questions this file answers YES), and
`unknown_question_ids`.

### What blocks

Only faults that are wrong however the rest of the form is filled in:

- headers that are not Play's five columns
- a row with no Question ID
- a `Response value` that is not a boolean or blank (`Yes`, `1`)
- the same `(question, response)` pair twice
- a `SINGLE_CHOICE` question with more than one `TRUE`
- an empty file, a header with no rows, UTF-16, or bytes that are not UTF-8

### What only warns

Everything conditional, because a warning that fires on correct files teaches
people to ignore warnings:

- a question ID outside the bundled snapshot (see below)
- an `Answer requirement` this server does not recognise — its choice rules are
  then not applied
- an answered question with no `TRUE` — for `MULTIPLE_CHOICE`, `SINGLE_CHOICE`
  or `REQUIRED`
- a UTF-8 BOM (stripped from what is sent) or mixed line endings

A question whose rows are **entirely blank** is not flagged at all. Most of a
real export is conditional — the purpose questions under
`PSL_DATA_USAGE_RESPONSES:PSL_NAME:…` only apply if the app declares it
collects a name — so an app that collects nothing legitimately leaves hundreds
of groups blank.

### The question-ID snapshot

Google adds and renames Data Safety questions without notice and does not
publish the catalogue, so `data_safety_ids.py` carries a dated, deliberately
partial list, built from the union of six real Play Console exports and a blank
template rather than from documentation. It can only ever say "this one is new, look at it" — it never
blocks a write, and an empty snapshot produces silence rather than noise.
Composite IDs are checked segment by segment, so a new data type under a
familiar family is still surfaced.

To refresh it: export a fresh CSV, run `preflight_data_safety_csv`, add
whatever `unknown_question_ids` reports to `KNOWN_ID_TOKENS`, and bump
`SNAPSHOT_DATE` in the same commit.

## set_data_safety

Write the declaration. **Write.** Disabled in
[read-only mode](../configuration.md#read-only-mode), and behind the
[two-step confirmation gate](../write-safety.md).

| Parameter | Type | Required | Description |
|---|---|---|---|
| `package_name` | string | Yes | App package name |
| `csv_path` | string | One of | Path to a CSV exported from Play Console |
| `safety_labels` | object | One of | `SafetyLabelsUpdateRequest` body: `{"safetyLabels": "<CSV contents>"}` |
| `confirm` | boolean | No | Set True only after the user approved the preview |
| `confirmation_token` | string | No | The token from that preview; never invent one |

Supply exactly one of `csv_path` and `safety_labels`.

```python
set_data_safety(
    package_name="com.example.myapp",
    csv_path="~/Downloads/data-safety.csv",
)
```

### Which input to use

**`csv_path` is the one to use.** The file is read and checked before anything
is sent, and read again on the confirm leg — so the approval is bound to the
answers, not to the path. Edit the file between the two calls and the token is
refused; leave it alone and it confirms.

It also keeps the preview readable. A real CSV is ~190 KB; passing it inline
would mean echoing all of it back verbatim to confirm.

**`safety_labels` still works, and is deliberately not enforced.** Structure is
reported into the preview as warnings, but never refused. The validator encodes
an unpublished format that Google changes without notice, so there has to be a
way past it — and this one costs a deliberate act of reading the file yourself,
which a `skip_validation` flag would not.

### The preview

Shows the questions the file answers YES, the row and question counts, and the
CSV's SHA-256 — not the CSV. It also states plainly that there is no API to read
what is being replaced.

### After the write

The API returns an empty body, so success means Play accepted the file, not
that the declaration says what you intended. Confirm it in Play Console.
