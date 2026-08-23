# Write safety

Google Play Console MCP can ship a release to production, delete every localized store
listing, and refund real money. Fifteen tools are therefore behind a two-step
confirmation gate, and every attempt — previewed, confirmed, rejected, executed
or failed — is written to an append-only audit log.

Ported from `sonlenef/storepilot-mcp` `core/guards.py` (MIT). The threat model
and the token design are that project's work.

## Why a gate and not just read-only

`GOOGLE_PLAY_MCP_READ_ONLY=1` refuses **every** write. That is the right setting for
an exploratory session, but it is all-or-nothing: the moment you actually want
to ship, you turn it off and the agent regains every destructive tool at once.

The gate is the finer instrument. A human reads exactly what one specific write
will do, approves that one, and the approval is bound to those exact arguments.
Both mechanisms are active: read-only wins when set.

## The threat model is the model

The caller is not a hostile human. It is a language model that:

- hallucinates confirmation tokens it never received,
- re-uses a token from an operation it ran five minutes ago,
- "helpfully" adjusts a parameter between the preview and the confirmation,
- and will happily answer "yes, the user approved" when nobody did.

So:

1. **The preview is the safety mechanism.** It contains everything a human needs
   to say no, rendered into the chat where a person can actually see it.
2. **The token only prevents drift.** It is an HMAC over a canonical fingerprint
   of the operation, keyed with a per-install secret the model never sees, so it
   cannot be computed, guessed, or transplanted from another operation.
3. **Everything attempted is logged**, with no credentials in it.

## What a call looks like

Step one — preview. Nothing is changed:

```
deploy_app(package_name="com.example.app", track="production", file_path="app.aab")
```

```
====================================================================
CONFIRMATION REQUIRED — nothing has been changed yet.
====================================================================
Operation : deploy_app
Target    : google_play:com.example.app
Effect    : Upload app.aab and release it on track 'production'.

What will change:
  track: (none)  ->  production
  rollout: (none)  ->  100% of eligible users

Warnings:
  ! Track 'production' is PRODUCTION — this reaches real users of the live app.
  ! Rollout is 100%: every eligible user on this track receives it at once.

Undo      : halt_release stops the rollout; it cannot un-deliver installs already served.

--------------------------------------------------------------------
Show the block above to the user and wait for an explicit approval.
If they approve, call the tool again with EXACTLY these arguments plus:

deploy_app(
  package_name='com.example.app',
  track='production',
  file_path='app.aab',
  confirm=True,
  confirmation_token="pp1.1787484533.3e8c3458.6105652f4b8bf347e45d0e909957153a",
)

The token expires at 2026-08-23T11:28:53+00:00 (10 minutes) and is bound to
these exact arguments — changing any of them invalidates it. Do not construct a
token yourself.
--------------------------------------------------------------------
```

Step two — the human approves, and the tool is called again with `confirm=True`
and that token. Only now does anything happen.

## What the token catches

| Situation | Result |
|---|---|
| `confirm=True` with no token | refused — the two-step flow is explained |
| A token the model invented | refused as malformed |
| A token from a different operation | refused: "does not match these arguments" |
| **A parameter changed between preview and confirm** | same refusal — the token is bound to the fingerprint |
| The same token used twice | refused: each token authorises exactly one execution |
| A token older than 10 minutes | refused as expired |

The gate deliberately **cannot** distinguish "token from another operation" from
"same operation, changed parameter". Both are drift, and the fix for both is the
same: preview again, and confirm the new preview.

## Gated tools

| Tool | Why |
|---|---|
| `deploy_app`, `deploy_app_multilang`, `batch_deploy` | ships a binary to users |
| `promote_release` | can move a build onto production |
| `update_rollout` | changes who receives an update; widening is irreversible |
| `delete_listing`, `delete_all_listings` | destroys store listing text, no API undo |
| `delete_all_images` | destroys store screenshots |
| `set_data_safety` | a legal declaration; an inaccurate one can get the app removed |
| `refund_order`, `revoke_subscription_purchase` | moves real money, not reversible |
| `delete_user` | removes a person's Play Console access |
| `deploy_app_recovery` | pushes a recovery action to installed devices |
| `update_app_details` | changes public contact information |
| `delete_review_reply` | changes a public reply |

### `halt_release` is deliberately NOT gated

Halting a rollout is the one operation that makes things *safer*. Demanding a
second round-trip during an incident is itself the failure mode. It executes
immediately and is still written to the audit log, as `immediate`.

## Staged-rollout policy

`google_play_console_mcp.guards.PRODUCTION_POLICY` refuses to release to 100% of production in
one step. First step is capped at 20%; the default when no fraction is given is
10%. Reaching everyone requires the separately named widening tool — which is
the point.

`GOOGLE_PLAY_MCP_MAX_INITIAL_ROLLOUT` can tighten the ceiling freely but can only
loosen it to 0.5, and unparseable or `NaN` values fall back to the default. A
`NaN` ceiling would make every `fraction > ceiling` comparison false and
silently disable the policy, so that case is handled explicitly rather than
through `min`/`max`.

The policy also rejects track names that only *look* like production — `prod`,
`live`, `release`, `main` — because Play would otherwise silently create a
brand-new empty closed-testing track under that name. Pass `production`
explicitly.

## The audit log

`~/.google-play-mcp/audit.jsonl`, one JSON object per line, created `0600`:

```json
{"ts":"2026-08-23T11:18:53+00:00","tool":"deploy_app","target":"google_play:com.example.app",
 "outcome":"executed","fingerprint":"3e8c34580843af89","params":{"track":"production",
 "rollout_percentage":100.0},"detail":"","extra":{"version_code":42,"elapsed_s":3.1}}
```

Outcomes: `preview`, `confirmed`, `rejected`, `executed`, `failed`, `immediate`.
A **failed** write is the most important line in the log — it is the case where
you do not know whether anything landed.

Parameter values are scrubbed: anything whose key looks like a secret becomes
`<redacted>`, paths are shortened to their last two segments, and values over
240 characters are stored as a prefix plus a SHA-256 digest so the log stays
greppable while still proving which text was published.

Losing the audit log never breaks a write, but it is never silent either — a
`! Guard bookkeeping degraded:` banner is appended to tool output.

## State directory

`~/.google-play-mcp/`, or `GOOGLE_PLAY_MCP_STATE_DIR`. Created `0700`, and re-narrowed on
every write rather than only at creation, so a directory restored from a backup
gets fixed rather than trusted.

| File | Contents |
|---|---|
| `guard.key` | the per-install HMAC key, `0600` |
| `guard-nonces.json` | spent token nonces, for replay protection |
| `audit.jsonl` | the append-only audit log |

If `guard.key` is found readable by other local users it is narrowed to `0600`
and a warning is raised — a key another user can read is a key they can mint
tokens with. If the key cannot be persisted at all (read-only home, sandbox) a
process-lifetime key is used instead: confirmations still work within a session,
and every token from a previous process is rejected. That fails closed.

## Untrusted store text

Review bodies, reviewer display names and developer replies are written by
whoever installed the app, and they get rendered into tool output that the model
reads as context. That makes them an injection channel into the agent.

The forgeable unit is a **line**: this server's output is line-structured, so a
reviewer whose display name contains a newline could emit text that begins a
line and reads as Google Play Console MCP's own voice. `guards.untrusted()` flattens every
line boundary Python recognises — not just `\n`, but `\r`, `\v`, `\f`,
`\x1c`–`\x1e`, `\x85`, U+2028 and U+2029 — strips ANSI escapes, and collapses
whitespace. Every render site additionally indents, so untrusted text can never
start a line. Nothing rewrites the words themselves: a sanitizer that mangles
content produces false confidence and a wrong preview.

## A note on the port

One change from the upstream source: `_consume_nonce` called `_note_warning`
from inside `with _lock:`. `threading.Lock` is not reentrant, so the first time
the replay ledger became unreadable the server would deadlock — precisely when
the warning matters most. The warning is now deferred until after the lock is
released, matching what `_guard_secret` already did.
