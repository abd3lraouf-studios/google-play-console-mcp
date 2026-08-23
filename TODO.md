# Google Play Console MCP — build checklist

Architecture and sourcing rationale: [PLAN.md](PLAN.md).

Rules for every phase:
- `pytest` stays green and coverage stays at 100% branch. The base shipped 729
  tests at 100%; that is the bar, not a nice-to-have.
- `ruff check src` and `mypy src` stay clean.
- Vendored code gets an attribution entry in `NOTICE.md` in the same commit.
- One phase per branch, squash-merged to `main`.

Tool count is tracked so drift is visible: **117 → target ~204**.

---

## Phase 0 — Fork hygiene ✅

- [x] Clone `lusky3/play-store-mcp` full history into `~/Developer/google-play-console-mcp`
- [x] `origin` → `upstream`; no push remote yet (GitHub later)
- [x] Tag `upstream-baseline` at lusky3 v0.5.1 `ebdd9fd`
- [x] Baseline verified: 729 passed, ruff clean, mypy clean
- [x] Rename `play_store_mcp` → `google_play_console_mcp`, `PLAY_STORE_MCP_*` → `GOOGLE_PLAY_MCP_*`,
      distribution → `google-play-console-mcp` (isolated commit, 70 files, still 729 green)
- [x] `PLAN.md` + `TODO.md`
- [x] `NOTICE.md` with the license ledger
- [x] Update `pyproject.toml` description + version for the new scope (0.6.0.dev0)
- [x] Prune inherited CI that points at upstream's SonarCloud/Snyk/codecov projects

## Phase 1 — Plane 2: Play Developer Reporting (vitals) → 126 tools ✅

Port `devinwang/google-play-developer-mcp` `src/tools/reports.ts` (271 lines, MIT).

- [x] Add `https://www.googleapis.com/auth/playdeveloperreporting` to `SCOPES`
- [x] Second `build("playdeveloperreporting", "v1beta1", ...)` in `client.py`,
      reusing the existing retry/backoff and `_http_lock`
- [x] Pydantic models for metric sets, timeline specs, dimensions, freshness
- [x] `crash_rate` get + query
- [x] `anr_rate` get + query
- [x] `slow_start_rate` get + query
- [x] `slow_rendering_rate` get + query
- [x] `excessive_wakeup_rate` get + query
- [x] `stuck_background_wakelock_rate` get + query
- [x] `lmk_rate` get + query
- [x] `errors_counts` get + query
- [x] `errors_issues_search`, `errors_reports_search`
- [x] `anomalies_list`
- [x] `apps_search`, `fetch_release_filter_options`
- [x] `get_metric_set_freshness` (from `brunosemfio`, Apache-2.0 — attribute)
- [x] Tests to 100% branch on the new module
- [x] `docs/tools/vitals.md`

## Phase 2 — Publisher v3 leftovers → 140 tools ✅

All from `devinwang` (MIT) unless noted. Every endpoint verified against the
live androidpublisher v3 discovery document before implementing.

- [x] `validate_edit`
- [x] `get_country_availability`
- [x] `update_app_details` (details get + update; only supplied fields change)
- [x] `create_track`, `update_track` (custom closed-testing tracks)
- [x] `delete_listing`, `delete_all_listings`
- [x] `add_externally_hosted_apk`
- [x] `patch_expansion_file`, `update_expansion_file`
- [x] `acknowledge_subscription_purchase` (v1 — real billing gap)
- [x] `archive_subscription`
- [x] `convert_region_prices`
- [x] `delete_review_reply` (idea from jaydeetay — unlicensed, reimplemented)
- [x] ~~`publish_managed_release`~~ **dropped — the endpoint does not exist.**
      `androidpublisher v3` has no `managedPublishing` resource, so
      `AgiMaulana`'s implementation raises at runtime. `edits.commit`'s
      `changesNotSentForReview` governs re-review after a rejection, not the
      managed-publishing gate. Sending held changes live is Play Console UI
      only. See `docs/tools/publisher-extras.md`.
- [x] Tests + docs

## Phase 3 — Write-safety gate ✅

Vendored `sonlenef/storepilot-mcp` `core/guards.py` (MIT), narrowed to Play.

- [x] `guards.py`: HMAC-over-canonical-fingerprint confirmation tokens,
      per-install secret the model never sees
- [x] `errors.py`: errors that carry a remedy (`PlayConsoleError`, `ValidationError`,
      `GuardError`, `redact_path`, `render_error`)
- [x] Preview renderer — everything a human needs to say no, rendered into chat
- [x] Append-only audit log at `~/.google-play-mcp/audit.jsonl`, 0600, no credentials
- [x] Replay ledger (each token authorises exactly one execution)
- [x] Untrusted-store-text flattening (every line boundary, ANSI, controls)
- [x] Staged-rollout policy: production capped at 20% first step, undisableable
- [x] Wired 15 high-blast-radius tools through the gate
- [x] `halt_release` deliberately **unguarded** — audited `immediate`, never gated
- [x] Keep `GOOGLE_PLAY_MCP_READ_ONLY` as the blunt override
- [x] Tests: token forgery, replay, expiry, parameter drift, degraded filesystem
- [x] `docs/write-safety.md`
- [x] **Fixed a deadlock inherited from the source**: `_consume_nonce` called
      `_note_warning` inside `with _lock:`, and `threading.Lock` is not
      reentrant — the server would hang the first time the replay ledger became
      unreadable. Warning is now deferred until after the lock is released.

## Phase 4 — Plane 3: Cloud Storage bulk reports ✅ → 151 tools

Vendored `storepilot` `core/csv_reports.py` (MIT) + `desunit`'s family map (MIT).

- [x] `devstorage.read_only` scope, `GOOGLE_PLAY_MCP_REPORTS_BUCKET` config
- [x] UTF-16 BOM-aware decoding (plus UTF-8, UTF-8-BOM, BOM-less UTF-16)
- [x] **Column lookup by header name only**, never by position; a miss raises
      listing the headers actually present
- [x] Whole-token subset matching survives Google renames; never substrings
- [x] **Column exclusion** so a field cannot claim a column another already took
      (`Merchant Currency` must not resolve to `Amount (Merchant Currency)`)
- [x] Never sum across currencies
- [x] Segment-boundary package matching (`com.acme.free` ≠ `com.acme.freemium`)
- [x] Freshness on every result: a zero is distinguishable from an absence
- [x] Disk cache: settled months forever, recent months 6h, failure ignored
- [x] `check_reports_access`, `list_report_families`, `list_report_objects`
- [x] `get_installs_report`, `get_ratings_report`, `get_crashes_report`
- [x] `get_store_performance_report`, `get_store_conversion_report`
- [x] `get_search_terms` ← pure ASO data, exists in no API
- [x] `get_subscriptions_report`
- [x] `get_earnings_report` (per currency, per app)
- [x] `retained_installers` / `buyers_7d` marked **discontinued after 2021-06**
      rather than shipped as tools that silently return nothing
- [x] Offline fixture tests (the parser does no network I/O by design)
- [x] `docs/tools/reports.md`

## Phase 5 — Plane 4: scraping / ASO ✅ → 158 tools

Ported `RankoR/aso-mcp` (MIT).

- [x] `play-store-scraper-ng` dependency
- [x] Proxy rotation: load, random pick, park-on-failure, reset-when-exhausted,
      credentials masked in logs
- [x] Retry loop factored into ONE helper (the source repeats it per tool)
- [x] No pool configured → fail immediately rather than re-asking the store
- [x] Throttled to 3 req/s
- [x] `search_store`, `get_store_listing`, `browse_store_chart`,
      `get_keyword_suggestions`, `scrape_reviews`, `list_store_enums`
- [x] **All scraped text flattened through `guards.untrusted`** and tagged with
      `UNTRUSTED_CONTENT_NOTE` — store text is an injection channel into the
      agent, and the source project does not guard it
- [x] `check_aso_metadata`: warns between the **policy** title limit (30) and the
      **API** limit (50). A 40-char title uploads fine and then fails review;
      neither source project distinguishes the two
- [x] Tests with mocked clients — no live scraping in CI
- [x] `docs/tools/scraping.md`
- [ ] **Not done:** durable review-history archive across runs. `scrape_reviews`
      reads as far back as the store paginates but persists nothing.

## Phase 6 — Store-image preflight ✅ → 161 tools + 2 prompts

**Scope changed.** The original plan was to port `mtbun/storeshots-mcp` for
screenshot generation. That is redundant: `appscreens`
(github.com/abd3lraouf-studios/app-screenshot-generator) already generates and
exports screenshots and ships its own 45-tool MCP Worker. Duplicating any of it
here would be strictly worse.

Google Play Console MCP instead owns the half appscreens does not: does this rendered file
actually satisfy Play, checked before an upload that would 400 after the bake.

- [x] `store_images.py`: Play's per-image spec table
- [x] PNG + JPEG header parsing, no imaging dependency — reads the bytes, not
      the extension
- [x] **Alpha both ways**: forbidden for screenshots / feature graphic / TV
      banner, REQUIRED for the icon. A plain "PNG" export is RGBA.
- [x] Palette + `tRNS` counts as transparent (colour type alone would miss it)
- [x] Dimension, aspect (≤2:1), and file-size checks
- [x] Set rules: min 2 / max 8 per type per locale — invisible file-by-file
- [x] Reports every broken rule at once, not the first
- [x] `list_store_image_specs`, `preflight_store_image`, `preflight_screenshot_set`
- [x] **MCP prompts** (pulled forward from Phase 7): `publish_screenshots` names
      the appscreens handoff and the alpha trap; `release_health_check` puts
      vitals freshness before any health claim
- [x] No runtime coupling — the tools take a local path and know nothing about
      any generator. The two servers compose in the MCP client.
- [x] Tests built on real PNG/JPEG bytes, not mocks
- [x] `docs/tools/store-images.md`

## Phase 7 — Architecture gaps ✅ → 169 tools, 2 prompts, 4 resources

- [x] Edit lifecycle exposed: `create_edit`, `describe_edit`, `validate_open_edit`,
      `commit_edit`, `delete_edit` — a multi-step change is now ONE transaction
      and one review submission
- [x] **`commit_edit`'s preview is read back from Play**, not built from
      arguments: the content was staged by earlier calls, so reading the edit is
      the only honest account of what committing would publish. Warns on a staged
      production release and on an edit that stages nothing.
- [x] `_gate` accepts a lazy preview so that read is paid for once, on the
      preview leg only
- [x] `delete_edit` deliberately **not** gated — discarding a draft is the safe
      direction, and gating it makes agents leak edits
- [x] `android_publisher_invoke` escape hatch: reads run immediately, writes are
      gated, decided from the API's own HTTP verb. Root allowlist rather than
      free getattr traversal. Rejects a path naming a resource, not a method.
- [x] `list_locale_coverage` (fixes the source's edit leak), `clone_listing_to_locale`
      (text only; refuses to overwrite translated copy unless told)
- [x] MCP **resources**: store-image specs, report families, vitals metric sets,
      and a config readiness report that never reveals a configured value
- [x] MCP **prompts** — landed in Phase 6
- [x] `docs/tools/edits.md`
- [ ] **Deferred: named multi-account store.** Per-request credentials already
      cover multi-tenant use (`X-Google-Credentials` headers), so this is
      ergonomics rather than capability. `devinwang/auth/account-store.ts` is the
      pattern if it becomes worth doing.
- [x] ~~hemati `validate_metadata_policy`~~ — superseded by `check_aso_metadata`
      (Phase 5), which additionally distinguishes policy from API limits
- [x] ~~hemati `asset_spec_check`~~ — superseded by `preflight_store_image`
      (Phase 6), which reads real image headers

## Phase 8 — Release

- [ ] README rewrite: five planes, what each needs, honest status per plane
- [ ] Decide the `fastmcp==4.0.0b3` pin (hold vs move to stable)
- [ ] Docker image
- [ ] Publish to GitHub
- [ ] PyPI via trusted publishing
- [ ] MCP registry entry

---

## Guardrails

- Never claim a plane works before it has read real data. `storepilot` is
  scrupulous about this in its README and it is the right posture — the money
  and vitals paths especially.
- No AI attribution in commits, tags, or PR bodies.
- Do not vendor from `AppVisionOS`, `naufaldiath`, or `jaydeetay` — no license.
