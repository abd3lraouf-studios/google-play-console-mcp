# Edit lifecycle, escape hatch and locales

Every other mutating tool here is a **pre-baked one-shot**: it opens an edit,
does one thing, commits, and closes. That is safe, and it is why the rest of the
surface reads cleanly — but it means "patch the listing, upload a screenshot,
set the track, then commit" is four commits and four review submissions instead
of one.

These tools expose the lifecycle so that transaction is expressible.

## The lifecycle

| Tool | Gated | Notes |
|---|---|---|
| `create_edit` | no | Opens a draft. Nothing reaches users until commit. |
| `describe_edit` | read | Reads back everything staged. |
| `validate_open_edit` | read | Dry-runs the edit you are building. |
| `commit_edit` | **yes** | Publishes everything staged. |
| `delete_edit` | no | Discards the draft. |

```
create_edit                 -> edit_id
  <stage changes>
describe_edit               -> what a commit would publish
validate_open_edit          -> would it commit cleanly?
commit_edit                 -> preview, approve, publish
```

Edits expire after about seven days. An abandoned one is harmless, but
`delete_edit` is deliberately **not** gated — discarding a draft is the safe
direction, and an agent that has to ask permission to clean up leaves edits open
instead.

## `commit_edit`'s preview is read from Play

Everywhere else the write gate previews the arguments the caller passed. That
does not work here: the interesting content was staged by *earlier* calls, so
the arguments to `commit_edit` are just an edit ID.

So the preview is built by reading the edit back — its staged track releases,
listing locales, app details and binaries — and it warns when a **production**
release is staged, or when the edit stages nothing readable at all (usually the
wrong edit ID). The preview carries `Verified: read back from the open edit`.

The read happens only on the preview leg, not again on the confirm.

## `android_publisher_invoke`

A generic call into any Android Publisher v3 method by dotted path — insurance
for surfaces this server has not wrapped, and for endpoints Google ships after
this release.

```
android_publisher_invoke(method_path="edits.tracks.list",
                         params={"packageName": "com.example.app",
                                 "editId": "..."})
```

- **Reads run immediately.** Writes are two-step confirmed. The decision comes
  from the HTTP verb the API itself uses, so a read is never made to round-trip
  for approval just to find out it was a read.
- The first path segment must be an **allowlisted resource**. Free `getattr`
  traversal down a caller-supplied path on a live client is how "call any
  method" becomes a way to reach something that is not an API method at all.
- A path naming a *resource* rather than a method (`edits.tracks`) is rejected
  with the fix, instead of resolving to an object with no HTTP verb.

Prefer the dedicated tools where they exist: they validate input, normalise
output and carry the safety rules. The preview for a generic write says plainly
that Google Play Console MCP does not know what the call changes and has no undo for it.

## Locales

| Tool | Gated | Notes |
|---|---|---|
| `list_locale_coverage` | read | Which locales have a listing; missing vs a target set |
| `clone_listing_to_locale` | **yes** | Copies listing **text** between locales |

`clone_listing_to_locale` copies title, descriptions and video. **Images are not
cloned** — that means downloading and re-uploading every asset, a different
operation with different failure modes.

A locale that already has a listing is left alone unless `overwrite=True`, so
this cannot silently replace human translation with the source language. With
`overwrite` set, the preview says so explicitly.

> Reading listings requires an open edit. `list_locale_coverage` opens a
> throwaway one and **deletes it**, including when the read fails. The
> implementation this was ported from leaves it open, leaking an edit per call.

## Resources

Four MCP resources carry static reference data an agent can pull into context
without spending a tool call. None makes an API request or needs credentials:

| URI | Contents |
|---|---|
| `gplay://specs/store-images` | Play's per-image rules |
| `gplay://specs/report-families` | Bulk-report families, discontinued ones flagged |
| `gplay://specs/vitals-metric-sets` | Reporting API metric sets |
| `gplay://config` | Which planes are configured, and what each needs |

`gplay://config` reports **readiness, never values**. A resource is read
into the model's context, so a bucket id or key path does not belong in one.

## Prompts

| Prompt | Purpose |
|---|---|
| `publish_screenshots` | render → preflight → upload, including the alpha trap |
| `release_health_check` | vitals freshness before any health claim |
