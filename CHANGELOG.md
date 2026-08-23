<!-- markdownlint-disable-file MD024 -->
<!-- Keep a Changelog repeats "### Added"/"### Changed"/etc. across versions;
     MD024 (no-duplicate-headings) is disabled for this file by design. -->

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] - 2026-08-23

First release under the name `google-play-console-mcp`, relicensed to
Apache-2.0. Derived from [lusky3/play-store-mcp](https://github.com/lusky3/play-store-mcp)
(MIT) at v0.5.1 and extended from 117 to 169 tools across five data planes.
See [NOTICE](NOTICE) for full attribution.

### Added

- **Play Developer Reporting API** (9 tools) — crash rate, ANR rate, slow start,
  slow rendering, excessive wake-ups, stuck wakelocks, LMK rate, error counts,
  error issues, error reports and anomalies. Freshness is a first-class tool
  because Play vitals lag by hours to days.
- **Cloud Storage bulk reports** (11 tools) — earnings, installs, ratings,
  crashes, store performance, store conversion, subscriptions and **search
  terms**, none of which exist in any Play REST API. Totals are reported per
  currency and never summed across them.
- **Public store scraping** (7 tools) — competitor listings, chart positions,
  keyword suggestions, ASO metadata checks, and review history past the Reviews
  API's one-week window. All scraped text is flattened and tagged as untrusted.
- **Store-image preflight** (3 tools) — Play's real per-image rules checked
  locally before upload, including the alpha-channel asymmetry (screenshots must
  have none, the icon must have one) and the two-to-eight set rule.
- **Edit lifecycle** (5 tools) — `create_edit`, `describe_edit`,
  `validate_open_edit`, `commit_edit`, `delete_edit`, so a multi-step change is
  one transaction and one review submission.
- **14 Publisher v3 endpoints** the base did not cover, including
  `acknowledge_subscription_purchase` — an unacknowledged subscription purchase
  is automatically refunded and revoked by Google after three days.
- **`android_publisher_invoke`** — a generic escape hatch with a resource
  allowlist and verb-based gating.
- **Write-safety gate** — 18 destructive tools behind two-step confirmation with
  HMAC tokens bound to a canonical fingerprint of the operation, a staged-rollout
  policy that cannot be disabled, and an append-only audit log.
- 2 MCP prompts and 4 MCP resources.

### Changed

- Package renamed `play_store_mcp` → `google_play_console_mcp`; environment
  variables `PLAY_STORE_MCP_*` → `GOOGLE_PLAY_MCP_*`.
- Relicensed to Apache-2.0. Every upstream licence is retained verbatim in
  `LICENSES/` and the release build fails if the wheel omits them.
- Publishing moved to PyPI Trusted Publishing (OIDC); no API token exists.

### Fixed

- A reentrant-lock deadlock inherited from the vendored guard code: the replay
  ledger raised its degradation warning while holding a non-reentrant lock, so
  the server hung the first time the ledger became unreadable.
- `list_locale_coverage` opened an edit to read and never deleted it, leaking one
  per call.
- Column resolution could bind a field to a column another field had already
  claimed, reporting an earnings amount as its currency code.

### Removed

- `publish_managed_release`. There is no `managedPublishing` resource in the
  Android Publisher v3 discovery document, so implementations of this tool fail
  at runtime. Sending held changes live remains a Play Console UI action.


### Planned
- Consolidate and reduce the MCP tool surface (now 117 tools) by grouping
  related operations, to lower per-request tool-list overhead — with no planned
  loss of functionality.
- Promote the FastMCP 4 / MCP 2026-07-28 upgrade (below) to `stable` once
  FastMCP 4 reaches a stable release. `stable` remains pinned to `fastmcp<4.0`
  until then; this is nightly-only for now.

### Dependencies
- **Nightly-only:** bumped `fastmcp` to the `4.0.0b3` beta, which migrates to
  MCP Python SDK v2 and the new MCP 2026-07-28 spec (stateless protocol core;
  no more mandatory `initialize`/`Mcp-Session-Id`). FastMCP 4 negotiates the
  best mutual protocol era per client by default, so existing clients still on
  the older handshake keep working unchanged — verified locally: full unit
  suite (728 tests), lint, mypy, both transports (`stdio` and
  `streamable-http`) boot and complete a legacy `initialize` →
  `notifications/initialized` → `tools/list` round trip, and the live
  read-only integration suite against a real Play Console app all pass
  unmodified. Pinned to the exact beta version (not an open range) since
  FastMCP 4 isn't GA yet; `stable` is unaffected (still capped `<4.0`).

## [0.5.0] - 2026-08-14

Adds opt-in **code-mode**, migrates the server onto the standalone **`fastmcp`**
package, hardens shared-client concurrency, and removes the non-functional Vitals
tools.

> **Breaking — Vitals tools removed.** `get_vitals_overview` and
> `get_vitals_metrics` no longer exist (see Removed); they returned placeholder
> data and never called an API.

### Added
- **Experimental code-mode (opt-in):** set `CODE_MODE=1` to expose tools through
  FastMCP's code-mode transform (`search`/`get_schema`/`execute` meta-tools with a
  sandboxed executor) instead of the full tool list, reducing per-request tool-list
  token overhead. Off by default; requires the `google-play-console-mcp[code-mode]` extra
  (Monty sandbox) for the `execute` tool. This is the first step of the tool-surface
  reduction noted under Planned.

### Changed
- Migrated the server framework from the official MCP SDK's `FastMCP`
  (`mcp.server.fastmcp`) to the standalone `fastmcp` package (v3). Behavior is
  unchanged — all 117 tools, the `/health` and `/credentials` routes,
  per-request header credentials, admin-token auth, read-only mode, and
  DNS-rebinding protection (`GOOGLE_PLAY_MCP_DISABLE_DNS_REBINDING`) are
  preserved. This unblocks the upcoming code-mode capability, which lives only
  in `fastmcp`.
- **Breaking:** APK/AAB downloads are now **always confined to a directory** —
  there is no "write anywhere" mode. The base directory is
  `GOOGLE_PLAY_MCP_DOWNLOAD_DIR` when set, otherwise the server's current working
  directory; a `destination_path` that resolves outside it is rejected. On
  network transports (`--transport sse` / `streamable-http`), setting
  `GOOGLE_PLAY_MCP_DOWNLOAD_DIR` is **recommended** but not required — the server
  logs a warning (rather than refusing to start) when it is unset and falls back
  to the working directory. Point it at a writable directory on cloud/hosted
  deployments (e.g. `/tmp/play-store-downloads` on Render), where the working
  directory may be read-only.

### Removed
- **Breaking:** removed the non-functional `get_vitals_overview` and
  `get_vitals_metrics` tools. They never called an API and returned hardcoded
  placeholder data; Android Vitals requires the separate Play Developer
  Reporting API, which is out of scope for this server.

### Fixed
- `get_order` / `batch_get_orders` now read the real v3 `Order` resource:
  product IDs from `lineItems[].productId` (exposed as `product_ids` /
  `line_items`) and status from the `state` string enum. Previously they read
  non-existent top-level `productId` / `purchaseState` fields, so those values
  were always null against the live API and order state was lost.
- `list_in_app_products` now follows `tokenPagination.nextPageToken` instead of
  returning only the first page (apps with many SKUs were silently truncated).
- `get_reviews` and `list_voided_purchases` now paginate to `max_results`
  across pages via `tokenPagination`, rather than returning a single page.
- `delete_subscription_offer` now returns the parent `product_id` instead of
  mislabeling the deleted `offer_id` as `product_id`.
- Media downloads (`download_generated_apk` / `download_system_apk_variant`) now
  acquire the client's transport lock per chunk, closing a gap in the shared-client
  thread-safety fix: a download concurrent with another call on the shared client
  no longer races on the non-thread-safe `httplib2` transport (which could corrupt
  the downloaded file or raise `ResponseNotReady`).
- The shared (env / `/credentials`) client now serializes its HTTP transport with
  a per-client lock, so concurrent tool calls under network transports no longer
  race on the non-thread-safe `httplib2` connection (which could interleave
  requests or deliver a response to the wrong caller). Per-request header-auth
  clients each get their own client and stay fully concurrent.

### Security
- APK/AAB downloads (`download_generated_apk`, `download_system_apk_variant`)
  now write to a temporary file and atomically rename on success, so a failed
  or unauthorized download can no longer truncate an existing file or leave a
  partial one at the destination.
- Download-destination confinement lives in `PlayStoreClient` and applies to both
  the temporary `.part` file and the final file: every destination is canonicalized
  and verified to stay within the (always-present) base directory before anything
  is written, closing the path-traversal / arbitrary-file-overwrite vector
  (SonarCloud `S2083`). Downloads are always confined — `GOOGLE_PLAY_MCP_DOWNLOAD_DIR`
  when set, otherwise the working directory.
- Documented that the server-side credential fallback
  (`GOOGLE_PLAY_STORE_CREDENTIALS` / `/credentials`) is a process-global client
  shared by every request that omits a credential header; multi-tenant
  deployments should leave it unset so a missing header fails closed rather than
  running under a shared identity.
- Recommend pairing code-mode with `--read-only` / `GOOGLE_PLAY_MCP_READ_ONLY=1`
  unless writes are needed: one `execute` can invoke up to 50 tool calls
  (including mutations) behind a single approval. Read-only enforcement still
  applies inside the sandbox.

### Dependencies
- Bumped `pyasn1` 0.6.3 → 0.6.4 (CVE-2026-59885, CVE-2026-59886) and
  `cryptography` 49.0.0 → 50.0.0 (PYSEC-2026-3552) — HIGH-severity advisories in
  transitive dependencies (via `google-auth` / `pyjwt[crypto]`). `pip-audit` clean.

## [0.4.0] - 2026-07-02

Major feature expansion: grows from ~24 to **119 MCP tools**, adding broad
coverage of the Google Play Developer API, plus reliability/security hardening
and a full dependency refresh.

> **Note — write endpoints are beta.** The new write/mutating tools in this
> release are covered by unit tests (mocked), but only read-only paths have been
> exercised against the live Play API. Treat create/update/patch/delete/upload/
> purchase-action/migrate tools as beta and
> [open an issue](https://github.com/lusky3/play-store-mcp/issues) for any
> problems. Run with `--read-only` / `GOOGLE_PLAY_MCP_READ_ONLY=1` to disable all
> write operations.

> **Note — tool count.** 119 tools is a large surface for a single MCP server:
> it increases per-request token usage and some clients cap/truncate large tool
> lists. A follow-up release will reduce this.

### Added
- **Purchases & orders:** in-app product purchases (`get`/`acknowledge`/`consume`);
  purchase management (`refund_order`, `cancel`/`defer`/`revoke_subscription_purchase`,
  `get_product_purchase_v2`); `get_review`, `batch_get_orders`.
- **Monetization catalog:** in-app products, subscriptions, subscription base
  plans (incl. price migration), subscription offers, one-time products, and
  one-time product purchase options & offers.
- **Artifacts & uploads:** edit upload pipeline (APKs, app bundles, deobfuscation
  and expansion files); store-listing images; generated APK list + download;
  system APK variants; internal app sharing uploads.
- **Account & configuration:** external transactions (alternative billing);
  device tier configs; app data safety labels; app recovery actions; Play Console
  account access (users & grants).
- **Read-only mode:** `--read-only` / `GOOGLE_PLAY_MCP_READ_ONLY` disables all write
  operations.

### Changed
- Transient errors (429/500/503) are retried with exponential backoff on real API
  calls, and the retry is idempotency-aware — non-idempotent (POST) mutations are
  not retried on an ambiguous 5xx, to avoid duplicate side effects.
- `/credentials` endpoint hardened: optional `GOOGLE_PLAY_MCP_ADMIN_TOKEN`
  (constant-time bearer check) for deployments behind a reverse proxy; blocking
  credential validation moved off the event loop.
- Consistent error contract: read methods raise `PlayStoreClientError` instead of
  leaking raw `HttpError`, and edit transactions are always cleaned up on failure.
- List endpoints now follow `nextPageToken` — fixes silent truncation (including
  the account-access user list).
- CI: PyPI publish gated on tests/lint/type-check; least-privilege Docker workflow
  permissions; pinned `uv`.

### Fixed
- `list_app_recoveries` now sends the API-required `versionCode` (previously
  rejected).
- `__version__` is single-sourced from package metadata (was a stale `0.2.0`).
- Case-insensitive `.aab` detection.
- Subscription `start_time` / `expiry_time` populated from the v2 response.

### Security
- `pyjwt[crypto]>=2.12.0` is now a declared dependency so the CVE-2026-32597 fix
  reaches installs, not just the lockfile.
- Credential-update error responses no longer leak exception text.

### Dependencies
- Refreshed all dependencies to latest, including the majors **mypy 2.x** and
  **protobuf 7.x**. Validated: 697 tests / 100% branch coverage, ruff/mypy clean,
  pip-audit clean, and a live read-only API smoke.

## [0.3.0] - 2026-06-19

Security hardening, dependency upgrades, and CI improvements.

### Added
- Configurable DNS-rebinding protection via the
  `GOOGLE_PLAY_MCP_DISABLE_DNS_REBINDING` environment variable (for cloud /
  reverse-proxy deployments).

### Changed
- Upgraded `mcp` 1.26.0 → 1.28.0 and `cryptography` 46.0.7 → 49.0.0.
- Hardened CI workflows, suppressed scanner false positives, and addressed
  code-review findings.

## [0.2.0] and earlier

See the [GitHub Releases](https://github.com/lusky3/play-store-mcp/releases) page.

[Unreleased]: https://github.com/lusky3/play-store-mcp/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/lusky3/play-store-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/lusky3/play-store-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/lusky3/play-store-mcp/compare/v0.2.0...v0.3.0
