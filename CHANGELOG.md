<!-- markdownlint-disable-file MD024 -->
<!-- Keep a Changelog repeats "### Added"/"### Changed"/etc. across versions;
     MD024 (no-duplicate-headings) is disabled for this file by design. -->

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-08-23

### Added

- **One-line installer** — `uvx google-play-console-mcp install` registers the
  server with your coding agents. With no `--agent` it detects the agents on the
  machine and offers them for selection; `-a/--agent` names them directly,
  `--all` takes every one. Flags cover scope (`--project`, default user-level),
  `--read-only`, `--credentials`, `--name`, `--uvx` and `--dry-run`.
  Claude Code, Claude Desktop, Cursor, VS Code, Codex, Gemini CLI, Windsurf,
  Zed, Cline, Goose, OpenCode and 10 more are supported, each with its own
  config path, root key (`mcpServers` / `servers` / `context_servers`) and
  format (JSON, TOML, YAML).

  The wiring is delegated to [add-mcp](https://github.com/neon-solutions/add-mcp)
  (Apache-2.0), invoked through `npx`. It is not vendored or redistributed — it
  is fetched at run time only when you run `install`. With no Node.js present
  the command prints the exact config block to paste and exits non-zero rather
  than failing silently.

  The registered launch command is chosen deliberately: an absolute path to a
  durably installed console script, because GUI agents such as Claude Desktop
  and Zed start from a login environment whose `PATH` usually lacks
  `~/.local/bin`. When `install` is itself running from a uv cache — the
  `uvx ... install` case — it registers the `uvx google-play-console-mcp` runner
  form instead, since the cache path is pruned and its hash changes.

### Changed

- The bare invocation is unchanged: MCP clients launch the executable with no
  arguments and speak stdio to it, so the server never sits behind a required
  subcommand.

## [0.1.0] - 2026-08-23

Initial release. 169 tools across five data planes, 2 MCP prompts and 4 MCP
resources.

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
- **Full Publisher v3 coverage**, including
  `acknowledge_subscription_purchase` — an unacknowledged subscription purchase
  is automatically refunded and revoked by Google after three days.
- **`android_publisher_invoke`** — a generic escape hatch with a resource
  allowlist and verb-based gating.
- **Write-safety gate** — 18 destructive tools behind two-step confirmation with
  HMAC tokens bound to a canonical fingerprint of the operation, a staged-rollout
  policy that cannot be disabled, and an append-only audit log.
- APK/AAB downloads are always confined to a directory: `GOOGLE_PLAY_MCP_DOWNLOAD_DIR`
  when set, otherwise the server's working directory. A `destination_path` that
  resolves outside it is rejected.
- **Experimental code-mode (opt-in):** set `CODE_MODE=1` to expose tools through
  FastMCP's code-mode transform (`search`/`get_schema`/`execute` meta-tools with a
  sandboxed executor) instead of the full tool list, reducing per-request tool-list
  token overhead. Off by default; requires the `google-play-console-mcp[code-mode]`
  extra (Monty sandbox) for the `execute` tool.
- Publishing via PyPI Trusted Publishing (OIDC); no API token exists.

### Notes

- **Write endpoints are beta.** The write/mutating tools are covered by unit
  tests (mocked), but only read-only paths have been exercised against the live
  Play API. Treat create/update/patch/delete/upload/purchase-action/migrate
  tools as beta and [open an issue](https://github.com/abd3lraouf-studios/google-play-console-mcp/issues)
  for any problems. Run with `--read-only` / `GOOGLE_PLAY_MCP_READ_ONLY=1` to
  disable all write operations.
- **Tool count.** 169 tools is a large surface for a single MCP server: it
  increases per-request token usage and some clients cap/truncate large tool
  lists. A follow-up release will reduce this.
- There is no `managedPublishing` resource in the Android Publisher v3 discovery
  document, so no `publish_managed_release` tool is provided. Sending held
  changes live remains a Play Console UI action.
- Android Vitals is served by the separate Play Developer Reporting API tools
  above; there is no `get_vitals_overview` / `get_vitals_metrics`.

### Planned
- Consolidate and reduce the MCP tool surface (now 169 tools) by grouping
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
  suite (1,199 tests), lint, mypy, both transports (`stdio` and
  `streamable-http`) boot and complete a legacy `initialize` →
  `notifications/initialized` → `tools/list` round trip, and the live
  read-only integration suite against a real Play Console app all pass
  unmodified. Pinned to the exact beta version (not an open range) since
  FastMCP 4 isn't GA yet; `stable` is unaffected (still capped `<4.0`).

[Unreleased]: https://github.com/abd3lraouf-studios/google-play-console-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/abd3lraouf-studios/google-play-console-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/abd3lraouf-studios/google-play-console-mcp/releases/tag/v0.1.0
