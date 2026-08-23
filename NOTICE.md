# Third-party attribution

Google Play Console MCP is a fork of, and vendors code from, the projects below. Each entry
records what was taken and under which license. Update this file in the same
commit that vendors the code.

## Base

**lusky3/play-store-mcp** — MIT
<https://github.com/lusky3/play-store-mcp>
Forked at v0.5.1 (`ebdd9fd`, tagged `upstream-baseline`). The Android Publisher
v3 client, the Pydantic model layer, the MCP server, the test suite, and the CI
configuration all originate here.

## Vendored

| Project | License | What was taken | Landed in |
|---|---|---|---|
| [devinwang/google-play-developer-mcp](https://github.com/devinwang/google-play-developer-mcp) | MIT | `src/tools/reports.ts` → Play Developer Reporting tools; Publisher v3 endpoints missing from the base; `auth/account-store.ts` multi-account pattern; `utils/edit-session.ts` rollback pattern | Phases 1, 2, 7 |
| [sonlenef/storepilot-mcp](https://github.com/sonlenef/storepilot-mcp) | MIT | `core/csv_reports.py` UTF-16 / column-drift-tolerant report parser; `core/guards.py` HMAC write-confirmation gate, rollout policy, audit log and untrusted-text flattening (ported to `google_play_console_mcp/guards.py`, with a reentrant-lock deadlock in `_consume_nonce` fixed) | Phases 3, 4 |
| [desunit/google-play-analytics-mcp](https://github.com/desunit/google-play-analytics-mcp) | MIT | Cloud Storage bulk-report family map: bucket prefixes, file prefixes, dimensions, and which families Google discontinued | Phase 4 |
| [RankoR/aso-mcp](https://github.com/RankoR/aso-mcp) | MIT | Public-store scraping tools, the proxy pool and rotation strategy, and the metadata length validators (ported to `google_play_console_mcp/scraping.py`, with the per-tool retry loop factored into one helper, all scraped text flattened as untrusted, and the policy-vs-API title limit distinguished) | Phase 5 |
| [mtbun/storeshots-mcp](https://github.com/mtbun/storeshots-mcp) | MIT | **Not used.** Screenshot generation is covered by `appscreens`, which the author already runs; Google Play Console MCP only validates and uploads. No code taken. |
| [brunosemfio/mcp-google-play-console](https://github.com/brunosemfio/mcp-google-play-console) | Apache-2.0 | `get_metric_set_freshness` | Phase 1 |
| [hemati/google-play-store-mcp](https://github.com/hemati/google-play-store-mcp) | MIT | `list_locale_coverage` and `clone_listing_to_locale` (ported with the source's per-call edit leak fixed). Its metadata-policy and asset-spec checks were superseded by `check_aso_metadata` and `preflight_store_image`. | Phase 7 |
| [w3wide/play-console-mcp](https://github.com/w3wide/play-console-mcp) | MIT | MCP resource and prompt registration patterns | Phase 7 |

## Ideas only — no code copied

| Project | License | Why no code was taken |
|---|---|---|
| [jaydeetay/googleplayreviewsmcp](https://github.com/jaydeetay/googleplayreviewsmcp) | **none** | No license file, so nothing may be vendored. Approaches reimplemented from behaviour: caching reviews locally to backfill history past the Reviews API's ~1-week window; retracting a developer reply |
| [AgiMaulana/GooglePlayConsoleMcp](https://github.com/AgiMaulana/GooglePlayConsoleMcp) | MIT | Licensing was fine; the code was not. Managed publishing was **evaluated and rejected** — its `service.managedPublishing()` call targets a resource absent from the androidpublisher v3 discovery document. No code taken. |
| [TylerThompson/google-play-console-mcp](https://github.com/TylerThompson/google-play-console-mcp) | MIT | Only the concept of a generic `android_publisher_invoke` escape hatch. This implementation adds a resource allowlist and verb-based gating. |
