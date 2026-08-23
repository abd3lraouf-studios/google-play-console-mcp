# Publisher v3 — endpoints beyond the inherited surface

Fourteen Android Publisher v3 tools the upstream base did not cover. No new
credentials or scopes: these use the same `androidpublisher` service as the rest
of the publishing tools.

Every endpoint here was verified against the live
[`androidpublisher v3` discovery document](https://androidpublisher.googleapis.com/$discovery/rest?version=v3)
before being written.

## Edits

| Tool | Notes |
|---|---|
| `validate_edit` | Opens an edit, validates it, then **discards it** — nothing is committed. An empty edit always validates, so this is most useful as a permissions probe: a failure means the service account cannot publish, which is better learned before a release than during one. |

## App configuration

| Tool | Notes |
|---|---|
| `get_country_availability` | Country codes a track is distributed to, plus the rest-of-world flag and whether the track mirrors production. |
| `update_app_details` | Default listing language and the contact email, phone and website shown on the store listing. **Only the fields you pass change** — the rest are read and written back, because the Play API replaces the whole resource on update. |

## Tracks

| Tool | Notes |
|---|---|
| `create_track` | Creates a custom closed-testing track. The four built-ins (internal, alpha, beta, production) always exist; this is how testing groups beyond them are set up. |
| `update_track` | Rewrites a track's release configuration. |

Both commit in a single edit and roll it back on failure.

## Store listings

| Tool | Notes |
|---|---|
| `delete_listing` | Removes one locale's listing. |
| `delete_all_listings` | Removes **every** locale's listing in one commit. Destructive, no API undo. |

## Binaries

| Tool | Notes |
|---|---|
| `add_externally_hosted_apk` | Registers an APK hosted outside Play. Enterprise (Play for Work private channel) only — a normal developer account gets a 403. |
| `patch_expansion_file` | Points a version's OBB at one already uploaded against another version code, using partial-update semantics. |
| `update_expansion_file` | Same, using full-replace semantics. |

Large apps reuse the same OBB across builds; these attach an existing expansion
file to a new version code instead of re-uploading it. `expansion_file_type`
must be `main` or `patch`.

## Billing

| Tool | Notes |
|---|---|
| `acknowledge_subscription_purchase` | **Google automatically refunds and revokes any subscription purchase not acknowledged within three days.** The inherited surface acknowledged one-time products but not subscriptions, so this closed a path to silent revenue loss. |
| `archive_subscription` | Play recommends archiving rather than deleting a subscription that has ever been purchased: deletion is rejected once purchases exist, and archiving preserves the record for existing subscribers. |
| `convert_region_prices` | Converts one source price into every Play region's price. Returns Play's own recommended prices — rounded to local conventions — not a naive FX conversion. Read-only, so it works under `GOOGLE_PLAY_MCP_READ_ONLY`. |

## Reviews

| Tool | Notes |
|---|---|
| `delete_review_reply` | Retracts a developer reply. The Play API has **no delete-reply endpoint**; the reply is cleared by replying again with empty text. The result carries `method: "empty_reply"` so this is not mistaken for erasure from history. |

## Not included: managed publishing

Several MCP servers in this space ship a `publish_managed_release` tool. It
cannot work, and this one deliberately does not have it.

The Android Publisher v3 discovery document has **no `managedPublishing`
resource** — its top-level resources are `applications`, `apprecovery`,
`appsigning`, `appstoreappsreview`, `appstorecatalog`, `edits`,
`externaltransactions`, `generatedapks`, `grants`, `inappproducts`,
`internalappsharingartifacts`, `monetization`, `orders`, `purchases`, `reviews`,
`systemapks` and `users`. A call to `service.managedPublishing()` raises at
runtime.

`edits.commit` does accept a `changesNotSentForReview` parameter, but its own
documentation scopes it to re-review after a rejection:

> When a rejection happens, the parameter will make sure that the changes in
> this edit won't be reviewed until they are explicitly sent for review from
> within the Google Play Console UI.

That is not the managed-publishing gate. Sending held changes live remains a
Play Console UI action with no API equivalent. A tool that claimed otherwise
would fail at runtime or silently do nothing.

## Read-only mode

`GOOGLE_PLAY_MCP_READ_ONLY` blocks all of these except `get_country_availability` and
`convert_region_prices`, which mutate nothing.

`validate_edit` is treated as a write even though it discards its edit: it
consumes an edit slot and touches the publishing API, so it belongs behind the
same gate.
