# Public store scraping

Seven read-only tools that read the **public** Play Store rather than your
developer account. No credentials are needed — these work even with no service
account configured.

They answer three things nothing else here can:

- **What do competitors look like, and where do they rank?** Store ranking is
  country- and language-specific, so `search_store` is a real ranking check.
- **What does Play autocomplete suggest?** The closest thing to public keyword
  volume the store exposes.
- **What did a reviewer write more than a week ago?** The authenticated Reviews
  API returns roughly **one week**. Scraping is the only route to older reviews —
  and it works for competitors' apps too.

## Tools

| Tool | Returns |
|---|---|
| `search_store` | Ranked search results as a user in that country sees them |
| `get_store_listing` | Any app's full public listing — description, installs, ratings histogram |
| `browse_store_chart` | Top free / paid / grossing, optionally by category |
| `get_keyword_suggestions` | Play's own autocomplete for a seed keyword |
| `scrape_reviews` | Public reviews, past the Reviews API's one-week window |
| `list_store_enums` | Valid collection, category, sort, age and price values |
| `check_aso_metadata` | Listing text against Play **policy** limits |

All are reads, so `GOOGLE_PLAY_MCP_READ_ONLY` does not block them.

## Scraped text is untrusted

Review bodies, reviewer display names and app descriptions are written by
strangers, and they land in tool output the model reads as context. That makes
them an injection channel into the agent driving your release tooling.

Every scraped string is passed through the same flattener the write gate uses:
line breaks, control characters and ANSI escapes are removed, so store text can
never begin a line and imitate this server's own output. The words are left
alone — a sanitizer that mangles content produces false confidence and a wrong
answer.

Tools returning store text also carry an `untrusted_content_note` telling the
model explicitly that the content is data, not instructions, and that nothing in
it can approve a write or supply a confirmation token.

## Policy limits vs API limits

`check_aso_metadata` exists because these are two different numbers, and getting
them confused wastes a review cycle:

| Field | API accepts | Play policy | Behaviour |
|---|---:|---:|---|
| Title | 50 | **30** | 31–50 → warning: uploads fine, rejected at review |
| Short description | 80 | 80 | over → error |
| Full description | 4000 | 4000 | over → error |

A 40-character title **uploads successfully and then fails review**. The
inherited `validate_listing_text` checks what the API accepts; this checks what
Play will actually publish. Use both — this one before deciding on the text, the
other before uploading it.

## Proxies

Scraping a whole portfolio from one IP gets that IP blocked. Configure a pool:

```bash
export GOOGLE_PLAY_MCP_SCRAPE_PROXIES="http://host1:3128,http://user:pass@host2:3128"
```

- A request that fails is retried on a **different** proxy; the failed one is
  parked.
- When every proxy has failed the list is cleared and tried again — the usual
  cause is a transient block, not a dead pool.
- Credentials are masked before a proxy URL reaches a log.
- With **no** pool configured a failure raises immediately rather than retrying:
  there is nothing to rotate to, and repeating the request just asks the store
  the same question again. The error says to configure a pool.

Requests are throttled to 3/second regardless. Nothing here is latency-critical,
and being rate-limited costs far more than being slow.

## Pairing with the other planes

| Question | Tool |
|---|---|
| What terms does Play suggest for "budget tracker"? | `get_keyword_suggestions` (public) |
| Which terms actually *converted* for my app? | `get_search_terms` (bulk reports) |
| What does my listing say? | `get_listing` (Publisher API, authoritative) |
| What does a competitor's listing say? | `get_store_listing` (scraped) |
| What did users say this week? | `get_reviews` (Reviews API) |
| What did they say last year? | `scrape_reviews` |

## Limits

Scraping reads HTML that Google can change without notice. When the store
changes shape these tools break and the authenticated tools do not — treat
scraped numbers as indicative and account data as authoritative.

`scrape_reviews` reads as far back as the store will paginate but does **not**
persist what it reads. Building a durable local review archive across runs is
separate work and is not implemented.
