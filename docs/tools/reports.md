# Bulk reports (Cloud Storage)

Eleven read-only tools over the monthly CSVs Play Console writes to a private
Cloud Storage bucket: installs, ratings, crashes, store acquisitions, conversion
rate, **search terms**, subscriptions and **earnings**.

**None of these numbers exist in any Play REST API.** Without this plane,
"how much did this app earn last month?" and "what did users search to find us?"
are unanswerable.

## Setup

This is a third data source with its own configuration, separate from the
Publisher and Reporting APIs.

**1. Find the bucket.** In Play Console open **Download reports → Statistics**
(or **Financial reports**) and click **Copy Cloud Storage URI**. It looks like
`gs://pubsite_prod_rev_0123456789`. There is no API that discovers this.

```bash
export GOOGLE_PLAY_MCP_REPORTS_BUCKET=gs://pubsite_prod_rev_0123456789   # gs:// optional
```

**2. Grant the permission.** In Play Console → **Users and permissions**, give
the service account **View app information and download bulk reports**.

> This is an **account-level** permission. An app-level grant does not reach the
> bucket, and this is the single most common reason the tools return 403.

The scope `devstorage.read_only` is requested automatically from the same
service-account key; no extra credential file is needed.

**3. Check it.** `check_reports_access` reports what is wrong rather than
raising, and names the exact remedy.

## Read the freshness block

Every tool returns a `freshness` object alongside the numbers. It exists to
prevent the worst failure in this domain: **reporting a zero for a month whose
CSV has not been published yet, as if the app had collapsed.**

| Situation | What freshness says |
|---|---|
| Month has not started | `is_complete: false`, "no data exists for it… not a measurement" |
| Month still filling in | `is_complete: false`, Play stats land 3–7 days late |
| Earnings before the 5th of the next month | `is_complete: false`, "zero because the report does not exist, not because revenue was zero" |
| Closed month, rows present | `is_complete: true`, `as_of` the last day |
| Closed month, no rows | `is_complete: false`, "the report object is missing from the bucket" |

A zero must always be distinguishable from an absence.

## Tools

| Tool | Returns |
|---|---|
| `check_reports_access` | Bucket configured and readable? With the remedy if not. |
| `list_report_families` | Every family, its dimensions, and whether Google still publishes it. |
| `list_report_objects` | Raw object names under a prefix — use when a report comes back empty. |
| `get_installs_report` | Daily/active-device installs, uninstalls, upgrades. |
| `get_ratings_report` | Daily and total average rating. |
| `get_crashes_report` | Daily crash and ANR counts. |
| `get_store_performance_report` | Store acquisitions by traffic source or country. |
| `get_store_conversion_report` | Visitors, acquisitions and conversion rate. |
| `get_search_terms` | What users searched to find the app, ranked by acquisitions. |
| `get_subscriptions_report` | New / cancelled / active subscriptions by country. |
| `get_earnings_report` | Transaction-level earnings, per currency and per app. |

All are reads, so none is blocked by `GOOGLE_PLAY_MCP_READ_ONLY`.

Per-app tools take a `dimension`: `overview`, `country`, `device`, `language`,
`app_version`, `os_version`, `carrier` (varies by family — `list_report_families`
has the exact list). Rows are collapsed to per-dimension totals, largest first;
a month of country-sliced installs is a few thousand rows and an agent needs the
shape, not the file.

## Money rules

`get_earnings_report` reports **per currency and never sums across currencies**.
Adding 1000 JPY to 10 EUR gives a wrong answer that looks like a right one, and
this server has no exchange rates and no business inventing any.

Amounts come from the **merchant currency** column — what actually lands in the
payout — not the buyer's currency.

Attributing a transaction to an app is a prefix test that stops at a segment
boundary. The product id is the package for an app sale and `<package>.<sku>`
for an in-app product, so a bare `startswith` would give `com.acme.free` the
revenue of `com.acme.freemium`. Only `.` and `:` count as boundaries.

## Discontinued families

`list_report_families` marks these. They exist in older tooling and return
nothing for recent months — because Google stopped publishing them, not because
the app had no data:

| Family | Status |
|---|---|
| `retained_installers` | Discontinued after 2021-06. Use `store_conversion`. |
| `buyers_7d` | Discontinued after 2021-06. |

## Why column lookup is by name

Google adds, renames and reorders these columns without notice — the earnings
columns changed in July 2026. Nothing here depends on column position or count:

- Headers are folded to stable keys (`Amount (Merchant Currency)` →
  `amount_merchant_currency`).
- Exact match first, then a **whole-token subset** match so a rename from
  `Daily Device Installs` to `Daily Device Installs (new)` still resolves.
- Never substrings: that silently confuses `Install events` with `Uninstall
  events`, attributing real numbers to the wrong metric while looking plausible.
- Single-token candidates (`Country`, `Device`) must match exactly — too
  ambiguous to fuzzy-match.
- A column another field already claimed is excluded, so looking for
  `Merchant Currency` in a file that only has `Amount (Merchant Currency)`
  cannot resolve to the amount and report `4.99` as the currency code.
- A miss raises an error **listing the headers actually present**, so the fix is
  obvious instead of archaeological.

## Encoding

Play writes these files as **UTF-16 with a BOM**, not UTF-8. Decoding naively
yields mojibake or a `UnicodeDecodeError`. UTF-8, UTF-8-BOM and BOM-less UTF-16
(detected by NUL density) are all handled so a future format change does not
break every report tool at once.

## Caching

Objects are cached under `~/.google-play-mcp/reports-cache/`, `0600`. A month old
enough to be settled is cached indefinitely — its CSV is immutable. A recent
month expires after 6 hours, because it is still filling in. A cache that cannot
be written is ignored: it is an optimisation, never a requirement.

## Two families, one directory

`store_performance` and `store_conversion` both live under
`stats/store_performance/` and are told apart only by the file prefix:

| Family | File prefix | Contents |
|---|---|---|
| `store_performance` | `total_store_performance_` | acquisitions only |
| `store_conversion` | `store_performance_` | visitors, acquisitions, conversion rate, **search terms**, UTM |

The `store_conversion` prefix matches its `total_` sibling too, so that sibling
is filtered out explicitly when resolving objects.
