# Google Play Console MCP

**An MCP server for Google Play Console — 169 tools across publishing, Android
vitals, revenue, ASO and store assets, with a confirmation gate on every
destructive write.**

[![CI](https://github.com/abd3lraouf-studios/google-play-console-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/abd3lraouf-studios/google-play-console-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/google-play-console-mcp)](https://pypi.org/project/google-play-console-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/google-play-console-mcp)](https://pypi.org/project/google-play-console-mcp/)
[![Coverage](https://img.shields.io/badge/coverage-100%25%20branch-brightgreen)](#tested)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Connect Claude, Cursor, VS Code, Codex, Gemini CLI, Windsurf, or any
[Model Context Protocol](https://modelcontextprotocol.io) client to your Google
Play developer account.

Ship a release. Ask whether it is crashing. Find out what it earned. See what
users searched to find it. Reply to a review. All of it from the same session,
and none of the destructive parts without you saying yes first.

```
"Is the 4.2.1 rollout healthy?"
  -> vitals freshness checked, crash rate by version code, worst issue's stack
     trace, current rollout fraction — and a recommendation

"How much did we make in July, and where did the installs come from?"
  -> earnings per currency, acquisitions by traffic source, top search terms

"Ship 4.2.2 to production at 10%."
  -> a preview you have to approve before anything moves
```

---

## Why this one

There are a dozen Google Play MCP servers. This is the only one that covers the
whole surface, and the only one with a real test suite.

|  | this | typical |
|---|---:|---:|
| Tools | **169** | 3–150 |
| Google APIs covered | **3** + scraping + preflight | 1 |
| Tests | **1,198** | 0 |
| Branch coverage | **100%** | none |
| Confirmation gate on destructive writes | **yes** | no |
| Audit log | **yes** | no |

Most servers wrap the Android Publisher API and stop. That API cannot tell you
your crash rate, cannot tell you what you earned, and cannot tell you what
people typed to find your app — those live in two other places entirely.

## The five planes

| Plane | Source | What it answers |
|---|---|---|
| **Publishing** | Android Publisher v3 | releases, tracks, listings, IAP, subscriptions, testers, users |
| **Android vitals** | Play Developer Reporting v1beta1 | crash rate, ANR rate, slow start, wakelocks, error clusters, anomalies |
| **Revenue & acquisition** | Cloud Storage bulk reports | earnings, installs, ratings, store conversion, **search terms** |
| **Public store** | scraping | competitor listings, chart positions, keyword suggestions, review history |
| **Store assets** | local | image preflight against Play's real rules, before upload |

Each needs different credentials, and the server tells you exactly which are
missing and how to fix it.

## Install

```bash
uvx google-play-console-mcp          # no install
pipx install google-play-console-mcp # or install it
pip install google-play-console-mcp
```

## Configure

Add to your MCP client (`.mcp.json`, `claude_desktop_config.json`, …):

```json
{
  "mcpServers": {
    "google-play": {
      "command": "uvx",
      "args": ["google-play-console-mcp"],
      "env": {
        "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/service-account.json",
        "GOOGLE_PLAY_MCP_REPORTS_BUCKET": "pubsite_prod_rev_0123456789"
      }
    }
  }
}
```

### Credentials

1. **Service account** — Google Cloud Console → enable the Google Play Android
   Developer API → create a service account → download the JSON key.
2. **Invite it** in Play Console → Users and permissions.
3. **Grant these**, which are separate and independently forgettable:

| For | Grant |
|---|---|
| Publishing | the app permissions you want the agent to have |
| Android vitals | the `playdeveloperreporting` scope |
| Revenue & acquisition | the **account-level** permission "View app information and download bulk reports" |

> The bulk-reports permission is **account-level**. An app-level grant does not
> reach the storage bucket, and this is the most common reason those tools 403.

Find the bucket in Play Console → Download reports → Statistics →
**Copy Cloud Storage URI**. There is no API that discovers it.

Then ask the agent to run `check_reports_access`, or read the
`gplay://config` resource — both report what is configured and what each missing
piece needs.

## Safety

This server can ship to production, delete every localized listing, and refund
real money. So:

**18 destructive tools are behind a two-step confirmation gate.** The first call
returns a preview — what changes, what it affects, how to undo it — and does
nothing. Only a second call carrying that preview's token executes.

The token is an HMAC over a canonical fingerprint of the operation, keyed with a
per-install secret the model never sees. It catches an invented token, a token
from a different operation, a **parameter changed between preview and
confirmation**, a reused token, and an expired one.

The threat model is not a hostile user. It is a language model that hallucinates
confirmation tokens, replays old ones, adjusts a parameter between the preview
and the confirmation, and will cheerfully report that you approved something you
never saw.

**`halt_release` is deliberately not gated.** Stopping a bad rollout is the one
operation that reduces blast radius, and demanding a second round-trip during an
incident is itself the failure mode.

**Production cannot go to 100% in one step.** The staged-rollout policy caps the
first step at 20%; reaching everyone requires the separately named widening
tool. The ceiling can be tightened but not disabled.

**Everything is audited** — previewed, confirmed, rejected, executed, failed —
to `~/.google-play-mcp/audit.jsonl`, with secrets scrubbed.

**`GOOGLE_PLAY_MCP_READ_ONLY=1`** refuses every write outright, for exploratory
sessions.

See [Write safety](docs/write-safety.md).

## What it knows that you would otherwise learn the hard way

- **Screenshots must have no alpha channel. The icon must have one.** A plain
  "PNG" export is RGBA — right for the icon, rejected for everything else.
  `preflight_store_image` catches it before a sixty-image bake fails at upload.
- **Play requires at least two screenshots per type per locale.** A set of one is
  valid file by file and still blocks the release.
- **A subscription purchase left unacknowledged for three days is automatically
  refunded and revoked** by Google. Silent revenue loss.
- **The API accepts a 50-character title; Play policy caps it at 30.** It uploads
  fine and is rejected at review.
- **Vitals data lags by hours to days.** A seven-day query that returns five days
  reads as an improvement rather than a gap, so freshness is a first-class tool.
- **Earnings for a month are not published until around the 5th of the next.**
  Reporting "$0" before that reads as a collapse rather than an absence.
- **Earnings are never summed across currencies** — 1000 JPY plus 10 EUR is a
  wrong answer that looks like a right one.
- **`com.acme.free` must not collect `com.acme.freemium`'s revenue.** Attribution
  stops at a package-segment boundary.

## Tools

169 tools. Full reference in [the docs](https://abd3lraouf-studios.github.io/google-play-console-mcp/).

| Area | Examples |
|---|---|
| Releases | `deploy_app` `promote_release` `update_rollout` `halt_release` `create_track` |
| Edits | `create_edit` `describe_edit` `validate_open_edit` `commit_edit` `delete_edit` |
| Listings | `update_listing` `list_locale_coverage` `clone_listing_to_locale` `upload_image` |
| Vitals | `query_vitals` `get_vitals_freshness` `search_error_issues` `list_vitals_anomalies` |
| Revenue | `get_earnings_report` `get_installs_report` `get_store_conversion_report` `get_search_terms` |
| Monetization | `create_subscription` `subscription_offers_*` `acknowledge_subscription_purchase` `refund_order` |
| Reviews | `get_reviews` `reply_to_review` `delete_review_reply` `scrape_reviews` |
| ASO | `search_store` `get_keyword_suggestions` `browse_store_chart` `check_aso_metadata` |
| Assets | `preflight_store_image` `preflight_screenshot_set` `list_store_image_specs` |
| Escape hatch | `android_publisher_invoke` |

Plus 2 prompts (`publish_screenshots`, `release_health_check`) and 4 resources.

## Tested

```
1,198 tests · 100% line and branch coverage · 15 modules
ruff clean · mypy clean · every release gated at --cov-fail-under=100
```

No live API call in the suite. Report parsing is tested against real UTF-16
bytes, image preflight against real PNG and JPEG headers.

## Out of scope

Screenshot *generation* — use
[appscreens](https://github.com/abd3lraouf-studios/app-screenshot-generator) or
any other tool, then preflight and upload here.

## Licence

[Apache-2.0](LICENSE).

---

<sub>Keywords: google play console mcp, google play mcp server, android publisher
api mcp, play developer api, model context protocol google play, android app
release automation, android vitals mcp, play console api claude, aso mcp,
google play earnings api, play store screenshots api, android release agent</sub>
