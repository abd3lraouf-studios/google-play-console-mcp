<!-- markdownlint-disable-file MD024 -->
<!-- Keep a Changelog repeats "### Added"/"### Changed"/etc. across versions;
     MD024 (no-duplicate-headings) is disabled for this file by design. -->

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- The release workflow installs the built wheel into a clean environment,
  imports it and runs its console script before anything is published. Every
  other check reads the distribution from the outside — its file list and its
  metadata — and none of them notices a module that failed to be packaged: a
  wheel missing `data_safety_ids.py` passes the licence check *and*
  `twine check --strict`, then raises `ModuleNotFoundError` on a user's first
  import. This has to run before the upload, because a PyPI version number can
  never be reused.

- Removed the TestPyPI rehearsal job, its `workflow_dispatch` target input, the
  `testpypi` environment, and the section of `docs/releasing.md` that
  prescribed it. No trusted publisher was ever registered on test.pypi.org, so
  the step failed with `invalid-publisher` on every release for five releases
  while real publishing succeeded. A documented step that always fails is worse
  than no step. What it was protecting against is covered directly by the
  install check above.

- The `pypi` deployment environment now requires a reviewer, so a tag push
  pauses for human approval instead of publishing straight through.

## [0.4.0] - 2026-08-24

### Added

- **Data Safety CSV support.** `set_data_safety` takes a `csv_path` and reads,
  checks and sends the file itself; `safety_labels` still works unchanged. New
  read-only `preflight_data_safety_csv` reports everything wrong with an export
  before it is declared, and a `declare_data_safety` prompt walks the
  export → preflight → declare path. The declaration has no read API, so an
  exported CSV is the only rollback that can exist — the prompt and the preview
  both say so.

  Structural faults block: wrong headers, a `Response value` that is not a
  boolean or blank, a repeated response, a single-choice question with two
  answers, UTF-16. Booleans are accepted in either case — Google's Help Center
  says `TRUE`/`FALSE`, but five of six exports sampled from published apps
  (2022-2026) write lowercase. Conditional faults only warn — most of a real export is contingent on
  answers elsewhere in it, and an unrecognised `PSL_*` question ID means "look
  at this", never "stop". See `docs/tools/data-safety.md`.

- **Contract tests against the real API description.** Every `service.…` call in
  the source is checked against the Android Publisher discovery document
  `google-api-python-client` ships and `client.py` already builds from, along
  with the `METRIC_SETS` accessor paths — which reach the API through `getattr`,
  where neither static analysis nor a mock could see them — and
  `INVOKABLE_ROOTS`. A network-marked drift test compares the bundled document
  against the live one; it is deselected by default.

### Changed

- **Request bodies are checked before they are sent.** Thirty-eight tools take a
  raw API body; a misspelled field used to reach Google and come back as a 400
  that did not name it. Unknown fields (with a "did you mean") and wrong JSON
  types are now refused locally; enum values, documented-required fields and
  read-only fields warn. `GOOGLE_PLAY_MCP_BODY_VALIDATION=warn` turns the layer
  off if Google adds a field before the pinned client learns about it. See
  `docs/configuration.md`.

- The test suite's service double now resolves every attribute against the
  discovery document before delegating to the mock, so a typo'd method or
  parameter fails instead of passing silently. Existing assertions are
  unchanged. This immediately caught one under-specified test, which called
  `edits.tracks.list` without the required `editId`.

- Property-based tests for the CSV parser, via `hypothesis` (new dev
  dependency). They assert the invariants the example tests cannot: that the
  uploaded text is always the input verbatim, that a report is internally
  consistent, that warnings never decide validity, and that an unreadable file
  always surfaces as a `ValidationError` with a remedy rather than a crash.

### Fixed

- **Data Safety CSVs are accepted with lowercase booleans.** Google's Help
  Center says to enter `TRUE`/`FALSE`, and the first implementation enforced
  that. Real exports disagree: of six sampled from published apps between 2022
  and 2026, five write lowercase `true`/`false`. The strict reading rejected
  five of those six. Both cases are now accepted; only a value that is not a
  boolean at all is an error.

- An oversized CSV field no longer escapes as a bare `_csv.Error`. An unclosed
  quote swallows the rest of the file into one cell and trips csv's 128 KB
  field limit; that now returns a `ValidationError` naming the likely cause.

- The inline `safety_labels` path had no size ceiling — only `load_csv` checked
  the file on disk. It is now bounded the same way.

- `starlette` and `uvicorn` are declared directly. `server.py` imports both for
  the HTTP transport, the `/health` and `/credentials` routes and the
  TrustedHost middleware, but they arrived only transitively via `fastmcp`, so
  a change to what fastmcp pins would have broken this server at import. Same
  reasoning already applied to `pyjwt` and `keyring`. Found with `deptry`.

- `pytest`'s `addopts` is a list rather than a string. A quoted `-m` expression
  inside one string survives pytest's own parsing but breaks tools that re-split
  it.

- A long non-string argument is no longer rendered in full into the confirmation
  block. `_call_value` only length-capped strings, so a multi-kilobyte CSV
  arriving as a one-key dict was echoed in its entirety — and a preview nobody
  reads to the end is not a safety mechanism. Such values are now replaced with
  a digest the human can check the confirm call against.

- Corrected the gated-tool count, which had drifted three different ways: the
  docs said fifteen and listed fifteen, `gplay://config` said sixteen, the
  README said eighteen. Eighteen is right. The resource now counts them instead
  of asserting a number, and `docs/write-safety.md` lists all eighteen —
  `android_publisher_invoke`, `clone_listing_to_locale` and `commit_edit` were
  missing.

- `set_data_safety` is documented in `docs/tools/data-safety.md` rather than
  inside the subscriptions reference, and is linked from the tool index.

## [0.3.1] - 2026-08-23

### Changed

- Dropped the GitHub Pages documentation site. The docs live in `docs/` in this
  repository and are read there; the `Documentation` URL in the package
  metadata now points at that directory instead of a published site. Removes
  `.github/workflows/docs.yml` and `mkdocs.yml`.

## [0.3.0] - 2026-08-23

### Added

- **Credentials in the OS keychain.** `google-play-console-mcp credentials set
  key.json` stores a service-account key in the macOS Keychain, Windows
  Credential Locker, or a Freedesktop Secret Service on Linux, and the server
  reads it from there. `credentials show` reports what is configured without
  printing any of it, `credentials delete` removes it, and `credentials adc`
  explains user OAuth. The key never appears in a terminal, a log line, or a
  shell history entry: `set` reads from a file or stdin.
- **`install --keychain`** files the key in the keychain instead of writing a
  path into the agent's config file — that file is plain text and often lives in
  a dotfiles repository.
- **User OAuth, via Application Default Credentials.** Setting
  `GOOGLE_PLAY_MCP_USE_ADC=1` (or passing `--use-adc`) lets the server
  authenticate with the credentials from `gcloud auth application-default
  login`, and with the metadata server on GCE, Cloud Run and GKE. No OAuth code
  ships here — no client secret, no redirect listener, no token file — because
  `google.auth.default()` already does all of it.

  **Off by default, deliberately.** Every other source is specific to this
  server: an argument passed to it, or a keychain entry under its own service
  name. ADC is ambient machine state shared with every Google tool, and on a
  developer laptop it is usually a personal account left behind by an unrelated
  `gcloud` login. Falling back to it silently would let this server publish a
  release as whoever logged in last, with nothing in the invocation to say so.

  User credentials are scoped at login, not at use, so the login has to name the
  scopes — `credentials adc` prints the exact command.

### Changed

- Credential resolution order is now: `--credentials` argument, then
  `GOOGLE_APPLICATION_CREDENTIALS` / `GOOGLE_PLAY_STORE_CREDENTIALS`, then the
  keychain, then ADC if enabled. Explicit arguments still win, so nothing that
  worked before changes.
- The "no valid credentials" errors now name every source, including the two
  new ones.

### Dependencies

- Declared `keyring>=25.7.0` directly. It was already installed for every user
  as a transitive dependency of `fastmcp`, so the dependency tree is unchanged.

## [0.2.1] - 2026-08-23

### Fixed

- **The Docker image now builds.** It never had: `pyproject.toml` force-includes
  `LICENSE` and `LICENSES/` into the wheel, but the Dockerfile copied neither
  into its build stage and `.dockerignore` excluded `LICENSE` outright, so
  `uv sync` failed with `Forced include not found: /build/LICENSES` on every
  commit since the first one. Both are copied now — which is also the correct
  outcome on its own terms, since a published image redistributes this code and
  the third-party code under `LICENSES/`, and both licences require their texts
  to travel with it.

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

[Unreleased]: https://github.com/abd3lraouf-studios/google-play-console-mcp/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/abd3lraouf-studios/google-play-console-mcp/releases/tag/v0.4.0
[0.3.1]: https://github.com/abd3lraouf-studios/google-play-console-mcp/releases/tag/v0.3.1
[0.3.0]: https://github.com/abd3lraouf-studios/google-play-console-mcp/releases/tag/v0.3.0
[0.2.1]: https://github.com/abd3lraouf-studios/google-play-console-mcp/releases/tag/v0.2.1
[0.2.0]: https://github.com/abd3lraouf-studios/google-play-console-mcp/releases/tag/v0.2.0
[0.1.0]: https://github.com/abd3lraouf-studios/google-play-console-mcp/releases/tag/v0.1.0
