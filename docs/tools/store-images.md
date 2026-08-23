# Store images: preflight and upload

**Google Play Console MCP does not generate screenshots.** Compositing device mockups is a real
design problem with real tools; [appscreens](https://github.com/abd3lraouf-studios/app-screenshot-generator)
is one, and it ships its own MCP server for authoring and exporting.

Google Play Console MCP owns the other half: checking that a rendered file actually meets
Google Play's rules **before** an upload that would otherwise 400 after a long
render, and then uploading it.

## The rule everyone gets backwards

| Type | Alpha channel |
|---|---|
| Screenshots (all device classes) | **forbidden** — 24-bit PNG or JPEG |
| Feature graphic | **forbidden** |
| TV banner | **forbidden** |
| **Icon** | **required** — 32-bit PNG |

An exporter set to plain "PNG" almost always emits RGBA. That is correct for the
icon and rejected for everything else. Discovering it after baking sixty images
is the failure this exists to prevent.

## Tools

| Tool | Purpose |
|---|---|
| `list_store_image_specs` | Every type's format, alpha, dimension and size rules |
| `preflight_store_image` | Check one file against one type's rules |
| `preflight_screenshot_set` | Check a whole set — every file **and** the count |
| `upload_image` | Upload (Publisher API — this already existed) |

`preflight_*` reads the file's own header, not its extension: a `.png` that is
really a JPEG is something export pipelines produce, and Play reads the bytes
too. It reports **every** broken rule at once rather than the first, because an
agent fixing one problem per round trip is worse than one told all four.

## The specs

| Type | Dimensions | Formats | Alpha | Max size |
|---|---|---|---|---|
| `phoneScreenshots` etc. | 320–3840px/side, ≤2:1 | PNG, JPEG | forbidden | 8 MB |
| `icon` | exactly 512×512 | PNG | **required** | 1 MB |
| `featureGraphic` | exactly 1024×500 | PNG, JPEG | forbidden | 15 MB |
| `tvBanner` | exactly 1280×720 | PNG, JPEG | forbidden | 15 MB |

Screenshot types: `phoneScreenshots`, `sevenInchScreenshots`,
`tenInchScreenshots`, `tvScreenshots`, `wearScreenshots`.

## The count rule is invisible file-by-file

Play requires **at least two** screenshots per type per locale, and caps at
eight. A set of one is valid image by image and still blocks the release, which
is why `preflight_screenshot_set` checks the set rather than only its members.

## Transparency detection

Alpha is not just the PNG colour type. A **palette** image carrying a `tRNS`
chunk is transparent too, and Play rejects it exactly as it rejects RGBA.
Checking colour type alone would pass that file and fail at upload, so the
`tRNS` chunk is checked as well.

JPEG has no alpha channel at all, which is why it is always a safe screenshot
format.

## The workflow

There is an MCP prompt for this — `publish_screenshots` — so an agent does not
have to infer the order:

```
1. list_store_image_specs        read the rules BEFORE rendering, while the
                                 export settings can still be changed
2. <render>                      appscreens export_images(render=true), or
                                 any other tool, or files you already have
3. preflight_screenshot_set      every file + the 2-8 count rule
4. <fix and re-render>           do not upload a set that failed
5. upload_image                  once preflight is clean
6. list_images                   confirm what is attached to the listing
```

Step 1 before step 2 is the point. Preflight after rendering tells you the
render was wrong; the specs before rendering stop it being wrong.

## No coupling

These tools take a **local file path**. They know nothing about appscreens or
any other generator, and Google Play Console MCP makes no network call to one. The two servers
compose in the MCP client, which is what MCP clients are for — configure both in
`.mcp.json` and the agent moves between them.

## Deleting

`delete_all_images` removes every image of a type for a locale in one commit and
the API has no undo, so it is behind the [write gate](../write-safety.md).
