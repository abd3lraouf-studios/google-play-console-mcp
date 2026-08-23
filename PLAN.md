# Google Play Console MCP — architecture and sourcing plan

One MCP server covering everything needed to manage an app's existence on Google
Play. Built on `lusky3/play-store-mcp` (MIT), which is the only server in this
space with real test coverage, and extended with the four data planes it lacks.

Baseline fork point is tagged `upstream-baseline` (lusky3 v0.5.1 @ `ebdd9fd`).
`git fetch upstream && git merge upstream/stable` still works; the rename was
committed in isolation so git rename detection follows it.

---

## Why this base

Surveyed 19 Google Play MCP servers on GitHub. Measured tool counts by parsing
registrations, ran the test suites, typechecked the TypeScript ones.

| | lusky3 (base) | devinwang | AppVisionOS | antoniolg |
|---|---:|---:|---:|---:|
| Tools | 117 | 150 | 104 | 3 |
| Commits | **184** | 10 | 4 | 29 |
| Merged PRs | **77** | 2 | 1 | 0 |
| Tests | **729, 100% branch cov** | 0 | 0 | 0 |
| CI workflows | **14** | 2 | 0 | 0 |
| Active in 2026 | **every month Jan–Aug** | Apr + Aug only | May only | dead |
| License | MIT | MIT | **none** | MIT |
| Stars | 19 | 3 | 1 | **155** |

Stars are a trap here: the 155-star repo ships three tools and has been dead
since October 2025. lusky3 is the only project that is actually maintained.

What made it the base beyond the numbers:

- **Idempotency-aware retry.** Whitelists `GET/HEAD/OPTIONS/PUT/DELETE` for 5xx
  retry; restricts POST to 429-only, because the server may have already applied
  a non-idempotent request before erroring. Exponential backoff with jitter.
- **Thread-safety.** Explicit lock around the non-thread-safe `httplib2`
  transport shared across concurrent tool workers.
- **Path-traversal-confined downloads** (`GOOGLE_PLAY_MCP_DOWNLOAD_DIR`).
- **Typed returns** — 583 lines of Pydantic models, not raw dicts.
- Dependency hygiene, design docs under `docs/superpowers/plans/`.

---

## The five data planes

The gaps are not one API. They are five independent sources with different auth.

| # | Plane | Scope / auth | Status | Source |
|---|---|---|---|---|
| 1 | Android Publisher v3 | `androidpublisher` | ✅ inherited, 117 tools | — |
| 2 | Play Developer Reporting v1beta1 | `playdeveloperreporting` | ❌ | `devinwang/google-play-developer-mcp` |
| 3 | GCS bulk reports `gs://pubsite_prod_rev_*` | `devstorage.read_only` | ❌ | `sonlenef/storepilot-mcp` + `desunit/google-play-analytics-mcp` |
| 4 | Public store scraping / ASO | none | ❌ | `RankoR/aso-mcp` |
| 5 | Store-image preflight | none | ✅ | own — generation is `appscreens`, out of scope here |

Plus one cross-cutting concern:

| Concern | Status | Source |
|---|---|---|
| Write-safety gate | binary `GOOGLE_PLAY_MCP_READ_ONLY` only | `sonlenef/storepilot-mcp` `core/guards.py` |

---

## Plane 2 — Play Developer Reporting (vitals)

**Consequence of the gap:** the server can ship a release but cannot tell you
whether it is crashing. No halt-on-bad-vitals loop is expressible.

Source: `devinwang/google-play-developer-mcp` `src/tools/reports.ts` — 271 lines,
self-contained, imports only zod + a local `defineTool` + a `reporting()` client.
MIT. 21 tools:

- `crash_rate`, `anr_rate` — get + query
- `slow_start_rate`, `slow_rendering_rate` — get + query
- `excessive_wakeup_rate`, `stuck_background_wakelock_rate`, `lmk_rate` — get + query
- `errors_counts` get/query, `errors_issues_search`, `errors_reports_search`
- `anomalies_list`
- `apps_search`, `fetch_release_filter_options`

Plus one tool nobody else has, from `brunosemfio/mcp-google-play-console`
(Apache-2.0): **`get_metric_set_freshness`** — tells you whether the vitals data
is stale before you act on it.

## Plane 3 — GCS bulk reports

**Consequence of the gap:** installs, ratings, earnings, store conversion and
search terms have no REST API at all. They exist only as CSVs in a private
Cloud Storage bucket. Without this plane, "how much did this app earn?" and
"what did users search to find us?" are unanswerable.

Primary source: `sonlenef/storepilot-mcp` `core/csv_reports.py` (MIT). Its module
docstring states the two properties that drive the whole design:

> The CSVs are UTF-16 encoded with a BOM, not UTF-8. Decoding naively yields
> either mojibake or a UnicodeDecodeError. Google adds, renames and reorders
> columns without notice (the earnings report columns changed in July 2026).
> Nothing here may depend on column position or column count — every lookup is
> by header name, whitespace/case tolerant, and a miss raises a ValidationError
> that lists the headers actually present.

It also never sums across currencies, and performs no network I/O so it is
offline-testable. `desunit/google-play-analytics-mcp` handles none of this — take
only its bucket-prefix map:

```
stats/store_performance/          -> acquisitions by traffic source / country
acquisition/retained_installers/  -> listing visitors, conversion rate, retention
acquisition/buyers_7d/            -> buyers within 7d of install (channel/country)
stats/installs/                   -> installs / uninstalls by dimension
stats/ratings/, ratings_v2/       -> ratings by dimension
stats/crashes/                    -> crashes / ANRs by dimension
financial-stats/subscriptions/    -> subscription activity by country
reviews/                          -> individual reviews
```

File naming: `<fileprefix><package>_<YYYYMM>[_<product>]_<dimension>.csv`,
daily rows keyed by a `Date` column, one calendar month per file.

## Plane 4 — Scraping / ASO

Source: `RankoR/aso-mcp` (MIT, Python, `play-store-scraper-ng`). Chosen over the
more popular `appreply-co/mcp-appstore` (67★, TypeScript) because it matches this
codebase's language and ships **proxy rotation** — without which portfolio-scale
scraping gets blocked.

12 tools: `google_play_search / app / list / reviews / suggest`, the category,
collection, country, language, age and rating filter enums, and metadata length
validators for title / short description / full description.

This plane also fixes **review history**: the Reviews API returns only ~1 week.
Scraping backfills it. (`jaydeetay/googleplayreviewsmcp` has the caching idea but
ships **no license** — reimplement the approach, do not copy the code.)

## Plane 5 — Store-image preflight

**Generation is out of scope.** `appscreens`
(github.com/abd3lraouf-studios/app-screenshot-generator) already composites and
exports store screenshots and ships a 45-tool MCP Worker for it. Porting
`mtbun/storeshots-mcp` would duplicate that badly.

Google Play Console MCP owns the handoff instead: verify a rendered file against Play's
per-image rules locally, then upload. The rule that earns it: screenshots and
the feature graphic must have NO alpha channel while the icon MUST have one, and
any exporter defaulting to "PNG" emits RGBA. Plus the set rule — min 2, max 8 per
type per locale — which is invisible when checking files one at a time.

No runtime coupling: the tools take a local path. The two servers compose in the
MCP client.

## Cross-cutting — the write gate

Source: `sonlenef/storepilot-mcp` `core/guards.py` (MIT). HMAC two-step
confirmation built against an explicit LLM threat model, quoting its docstring:

> The caller is not a hostile human, it is a language model that hallucinates
> confirmation tokens it never received, re-uses a token from an operation it ran
> five minutes ago, "helpfully" adjusts a parameter between the preview and the
> confirmation, and will happily answer "yes, the user approved" when nobody did.

The token is an HMAC over a canonical fingerprint of the operation, keyed with a
per-install secret the model never sees, so it cannot be computed, guessed or
transplanted. Everything attempted lands in an append-only audit log carrying no
credentials. This is a strict upgrade over the inherited binary read-only flag,
and it must land **before** the money and rollout-halting planes.

---

## Publisher v3 endpoints still missing from the base

Verified absent by fixed-string grep against `client.py` and `server.py`.
All available from `devinwang` (MIT) unless noted.

| Endpoint | Why it matters |
|---|---|
| `edits.validate` | no dry-run before commit |
| `edits.countryavailability.get` | cannot read per-country distribution |
| `edits.details.get/patch/update` | cannot set default language, contact email/phone/website |
| `edits.tracks.create` / `.patch` | **cannot create custom closed-testing tracks** |
| `edits.listings.delete` / `.deleteall` | cannot remove a locale's listing |
| `edits.apks.addexternallyhosted` | no externally-hosted APK (enterprise) |
| `edits.expansionfiles.patch` / `.update` | has get+upload only, cannot re-point an OBB |
| `purchases.subscriptions.acknowledge` | has product acknowledge but **not** subscription — real billing gap |
| `monetization.subscriptions.archive` | only delete exists |
| `monetization.convertRegionPrices` | no price conversion helper |
| reviews `delete_reply` | can reply, cannot retract (idea from jaydeetay, unlicensed — reimplement) |
| ~~managed publishing~~ | **Not implementable.** `androidpublisher v3` has no `managedPublishing` resource; `AgiMaulana`'s tool raises at runtime. Sending held changes live is Play Console UI only. |

## Architectural gaps

1. **The edit lifecycle is internal-only.** `edits().insert/commit/delete` appear
   only inside `client.py` methods. There is no `create_edit` / `commit_edit`
   tool, so an agent cannot compose a custom multi-step edit — every mutation is
   a pre-baked one-shot. Expose the lifecycle *and* keep a `with_edit()` helper
   with rollback (pattern from devinwang `utils/edit-session.ts`).
2. **No MCP resources, no prompts.** 117 tools and nothing else. `w3wide/play-console-mcp`
   is the only repo shipping `resources.ts` + `prompts.ts`.
3. **No named multi-account store.** Per-request credentials work, but there is
   no `accounts_list` / `accounts_switch` (devinwang `auth/account-store.ts`, 111 lines).
4. **No generic escape hatch.** `TylerThompson` exposes `android_publisher_invoke`
   — call any endpoint by name. Cheap insurance for unwrapped surfaces.

---

## Explicitly excluded

| Rejected | Reason |
|---|---|
| `trysonar/mcp` (47 ASO tools) | thin client for a hosted SaaS — `SONAR_API_KEY`, `baseUrl: https://trysonar.app`. Nothing to clone. |
| `dperkov/asomobile-mcp` | requires an ASOMobile subscription |
| `hemati` experiments (~10 tools) | calls `svc.edits().experiments().create(...)`, which is **not in the public Android Publisher v3 discovery document**. The rest is a local JSON store faking experiments. Its *localization* tools are real and are in scope. |
| `AppVisionOS/google-play-console-mcp` (104 tools) | **no LICENSE file** — legally unvendorable |
| `naufaldiath/android-vitals-mcp` | **no LICENSE file** |
| `gannonh/firebase-mcp` (248★), `tjdam007/mcp-crashlytics-server` | generic Firebase / needs a BigQuery export pipeline. Run as a *separate* MCP server if needed. |
| fastlane wrappers (`silviosotelo` 57 tools, `pampas93`, `lyderdev`) | shell out to fastlane rather than calling APIs — different execution model, do not mix in |

## Open decisions

- **`fastmcp==4.0.0b3`** is inherited and is a beta pin. Decide whether to hold
  it or move to stable before Plane 3 lands.

## License ledger

Everything vendored is MIT or Apache-2.0 and must carry attribution in
`NOTICE.md`.

| Repo | License | Vendoring |
|---|---|---|
| lusky3/play-store-mcp | MIT | base |
| devinwang/google-play-developer-mcp | MIT | ok |
| sonlenef/storepilot-mcp | MIT | ok |
| desunit/google-play-analytics-mcp | MIT | ok |
| RankoR/aso-mcp | MIT | ok |
| mtbun/storeshots-mcp | MIT | ok |
| brunosemfio/mcp-google-play-console | Apache-2.0 | ok, attribution required |
| hemati/google-play-store-mcp | MIT | ok (localization only) |
| w3wide/play-console-mcp | MIT | ok |
| AgiMaulana/GooglePlayConsoleMcp | MIT | ok |
| jaydeetay/googleplayreviewsmcp | **none** | ideas only, no code |
| AppVisionOS/google-play-console-mcp | **none** | excluded |
| naufaldiath/android-vitals-mcp | **none** | excluded |
