"""Public Play Store scraping — competitors, keywords and review history.

Everything else in this server reads *your own* account through an authenticated
API. This module reads the **public store**, which is the only way to answer
three questions:

- What do competitors' listings look like, and where do they rank?
- What does Play autocomplete suggest for a keyword?
- What did a reviewer write more than a week ago?

That last one matters more than it sounds. The Reviews API returns only about
**one week** of reviews; anything older is simply gone, so scraping is the only
route to history. :meth:`ScrapingMixin.scrape_reviews` reads as far back as the
store will paginate. It does not yet persist what it reads — building a durable
local archive across runs is a separate piece of work.

Ported from ``RankoR/aso-mcp`` (MIT) — the proxy pool, the retry loop and the
metadata validators are that project's design. See NOTICE.md.

Two things are deliberately different here:

**Scraped text is untrusted.** Review bodies, reviewer display names and app
descriptions are written by strangers and land in tool output the model reads as
context, which makes them an injection channel into the agent. Every scraped
string goes through :func:`google_play_console_mcp.guards.untrusted`, which flattens line
breaks so store text can never imitate this server's own output.

**The retry loop lives in one place.** The source repeats it in each tool, which
is how a fix reaches four of five call sites.
"""

from __future__ import annotations

import os
import random
from typing import TYPE_CHECKING, Any

import structlog
from google_play_scraper import (  # type: ignore[import-untyped]
    Age,
    Category,
    Collection,
    GooglePlayClient,
    Sort,
)

from google_play_console_mcp.errors import ValidationError
from google_play_console_mcp.guards import untrusted
from google_play_console_mcp.models import (
    ScrapedApp,
    ScrapedAppDetails,
    ScrapedReview,
    ScrapedReviewPage,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = structlog.get_logger(__name__)

#: Comma-separated proxy URLs. Scraping a portfolio from one IP gets it blocked.
PROXY_ENV_VAR = "GOOGLE_PLAY_MCP_SCRAPE_PROXIES"

#: Requests per second per client. Play does not document a limit; this is
#: politeness, and the value is low on purpose — nothing here is latency
#: critical, and being rate-limited costs far more than being slow.
DEFAULT_THROTTLE_RPS = 3

#: Play policy limits, which are stricter than what the API accepts. A 40-char
#: title uploads fine and is then rejected at review, so the ASO check warns at
#: the policy limit and errors only at the hard cap.
POLICY_TITLE_LENGTH = 30
API_TITLE_LENGTH = 50
SHORT_DESCRIPTION_LENGTH = 80
FULL_DESCRIPTION_LENGTH = 4000

PRICE_FILTERS = ("all", "free", "paid")


class ScrapingError(ValidationError):
    """A public-store request could not be completed."""

    kind = "scraping_error"


class ProxyPool:
    """A rotating pool of proxies, with failed ones parked.

    Loaded from ``GOOGLE_PLAY_MCP_SCRAPE_PROXIES`` as a comma-separated list, e.g.
    ``http://host1:3128,http://user:pass@host2:3128``. With no proxies
    configured every request goes out directly, which is fine for occasional
    lookups and not fine for scraping a portfolio.
    """

    def __init__(self, raw: str | None = None) -> None:
        source = raw if raw is not None else os.environ.get(PROXY_ENV_VAR, "")
        self._proxies: list[str] = [p.strip() for p in source.split(",") if p.strip()]
        self._failed: set[str] = set()
        if self._proxies:
            logger.info("Loaded scraping proxies", count=len(self._proxies))

    @property
    def has_proxies(self) -> bool:
        """Whether any proxy is configured."""
        return bool(self._proxies)

    @property
    def available(self) -> list[str]:
        """Proxies that have not failed since the last reset."""
        return [p for p in self._proxies if p not in self._failed]

    def get(self) -> dict[str, str] | None:
        """A random usable proxy, or None when none are configured."""
        if not self._proxies:
            return None
        candidates = self.available
        if not candidates:
            # Everything failed. Rather than give up, clear the marks and try
            # again: the usual cause is a transient block, not a dead pool.
            logger.warning("All scraping proxies failed; resetting the failed list")
            self._failed.clear()
            candidates = self._proxies
        return {"https": random.choice(candidates)}  # noqa: S311 - not cryptographic

    def mark_failed(self, proxy: dict[str, str] | None) -> None:
        """Park a proxy that just failed."""
        if proxy and (url := proxy.get("https")):
            self._failed.add(url)
            logger.debug("Marked scraping proxy failed", proxy=mask_proxy(url))

    def reset(self) -> None:
        """Un-park every proxy."""
        self._failed.clear()

    def attempts(self) -> int:
        """How many times a request is worth retrying across the pool."""
        return max(len(self.available), 1) if self.has_proxies else 1


def mask_proxy(url: str) -> str:
    """Hide credentials in a proxy URL before it reaches a log."""
    if "@" in url:
        return f"***@{url.rsplit('@', 1)[-1]}"
    return url


def list_collections() -> list[str]:
    """Play Store collection names (top free, top paid, grossing)."""
    return [c.name for c in Collection]


def list_categories() -> list[str]:
    """Play Store category names."""
    return [c.name for c in Category]


def list_sort_orders() -> list[str]:
    """Review sort orders."""
    return [s.name for s in Sort]


def list_age_filters() -> list[str]:
    """Age-range filters for browsing collections."""
    return [a.name for a in Age]


def _enum_or_error(enum: Any, value: str, label: str) -> Any:
    """Resolve a name into an enum member, or raise with the valid names."""
    try:
        return enum[value.upper()]
    except KeyError:
        raise ValidationError(
            f"Unknown {label} {value!r}.",
            remedy=f"Use one of: {', '.join(m.name for m in enum)}.",
        ) from None


def _clean(value: Any, *, limit: int | None = None) -> str | None:
    """Flatten a scraped string, or None when it is empty.

    Applied to everything the store returns. The text is written by strangers
    and is rendered into output the model reads as context.
    """
    if value is None:
        return None
    flattened = untrusted(value, limit=limit)
    return flattened or None


def _to_app(raw: Any) -> ScrapedApp:
    """Map an AppOverview into our model, flattening every string."""
    get = raw.get if isinstance(raw, dict) else lambda k, d=None: getattr(raw, k, d)
    return ScrapedApp(
        app_id=str(get("app_id", "") or ""),
        title=_clean(get("title")) or "",
        developer=_clean(get("developer")),
        developer_id=_clean(get("developer_id")),
        score=get("score"),
        price_text=_clean(get("price_text")),
        free=get("free"),
        summary=_clean(get("summary"), limit=400),
        icon=_clean(get("icon")),
    )


def _to_app_details(raw: Any) -> ScrapedAppDetails:
    """Map an AppDetails into our model."""
    get = raw.get if isinstance(raw, dict) else lambda k, d=None: getattr(raw, k, d)
    base = _to_app(raw)
    return ScrapedAppDetails(
        **base.model_dump(),
        description=_clean(get("description"), limit=4000),
        installs=_clean(get("installs")),
        min_installs=get("min_installs"),
        ratings=get("ratings"),
        reviews=get("reviews"),
        histogram=list(get("histogram") or []),
        currency=_clean(get("currency")),
        price=get("price"),
        offers_iap=get("offers_iap"),
        android_version=_clean(get("android_version")),
        developer_email=_clean(get("developer_email")),
        developer_website=_clean(get("developer_website")),
        privacy_policy=_clean(get("privacy_policy")),
        genre=_clean(get("genre")),
    )


def _isoformat(value: Any) -> str | None:
    """Render a scraped timestamp as ISO text, whatever shape it arrived in."""
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else _clean(value)


def _to_review(raw: Any) -> ScrapedReview:
    """Map a scraped Review into our model.

    Review text is the highest-risk string this server handles: it is written by
    anyone who installed the app and it reaches the model as context.
    """
    get = raw.get if isinstance(raw, dict) else lambda k, d=None: getattr(raw, k, d)
    return ScrapedReview(
        review_id=str(get("id", "") or ""),
        user_name=_clean(get("user_name"), limit=120),
        score=get("score"),
        text=_clean(get("text"), limit=5000),
        date=_isoformat(get("date")),
        reply_text=_clean(get("reply_text"), limit=5000),
        reply_date=_isoformat(get("reply_date")),
        version=_clean(get("version")),
        thumbs_up=get("thumbs_up"),
    )


class ScrapingMixin:
    """Public Play Store reads.

    Mixed into :class:`google_play_console_mcp.client.PlayStoreClient`. Needs no credentials —
    this is the public store, not the developer account — so it works even when
    the service-account key is missing.
    """

    _logger: Any
    _proxy_pool: ProxyPool | None = None

    @property
    def proxy_pool(self) -> ProxyPool:
        """The lazily-created proxy pool."""
        if self._proxy_pool is None:
            self._proxy_pool = ProxyPool()
        return self._proxy_pool

    def _scrape(self, action: Callable[[GooglePlayClient], Any], *, what: str) -> Any:
        """Run one scraping call, rotating proxies on failure.

        Retries only make sense when there is another proxy to try: without a
        pool, a failure is a real failure and repeating it just hits the store
        again for the same answer.
        """
        pool = self.proxy_pool
        attempts = pool.attempts()
        last_error: Exception | None = None

        for attempt in range(attempts):
            proxy = pool.get()
            try:
                client = GooglePlayClient(
                    proxies=proxy,
                    throttle_requests_per_second=DEFAULT_THROTTLE_RPS,
                )
                result = action(client)
            except Exception as exc:
                last_error = exc
                if proxy is None:
                    raise ScrapingError(
                        f"Could not {what}: {type(exc).__name__}: {exc}",
                        remedy=(
                            "The public Play Store rejected or dropped the request. Retry, "
                            f"or configure a proxy pool in {PROXY_ENV_VAR} — scraping a "
                            "whole portfolio from one IP gets that IP blocked."
                        ),
                    ) from exc
                pool.mark_failed(proxy)
                self._logger.warning(
                    "Scrape failed, rotating proxy",
                    what=what,
                    attempt=attempt + 1,
                    of=attempts,
                    error=str(exc),
                )
            else:
                if proxy is not None:
                    pool.reset()
                return result

        raise ScrapingError(
            f"Could not {what} after {attempts} proxy attempt(s): {last_error}",
            remedy=(
                "Every configured proxy failed. Check the proxies are reachable and not "
                "themselves blocked by Play."
            ),
        )

    # =========================================================================
    # Search and browse
    # =========================================================================

    def scrape_search(
        self,
        term: str,
        country: str = "us",
        language: str = "en",
        num: int = 20,
        price: str = "all",
    ) -> list[ScrapedApp]:
        """Search the public store, as a user in ``country`` would see it.

        Ranking is country- and language-specific, so those arguments are the
        whole point rather than incidental.
        """
        if price not in PRICE_FILTERS:
            raise ValidationError(
                f"Unknown price filter {price!r}.",
                remedy=f"Use one of: {', '.join(PRICE_FILTERS)}.",
            )
        results = self._scrape(
            lambda c: c.search(
                term=term, num=num, price=price, lang=language, country=country
            ),
            what=f"search the store for {term!r}",
        )
        return [_to_app(item) for item in results or []]

    def scrape_app(
        self, app_id: str, country: str = "us", language: str = "en"
    ) -> ScrapedAppDetails:
        """Read one app's public listing — any app, not just your own."""
        raw = self._scrape(
            lambda c: c.app(app_id=app_id, lang=language, country=country),
            what=f"read the listing for {app_id}",
        )
        return _to_app_details(raw)

    def scrape_collection(
        self,
        collection: str = "TOP_FREE",
        category: str | None = None,
        country: str = "us",
        language: str = "en",
        num: int = 50,
        age: str | None = None,
    ) -> list[ScrapedApp]:
        """Browse a store chart — the top-free/paid/grossing lists."""
        options: dict[str, Any] = {
            "collection": _enum_or_error(Collection, collection, "collection"),
            "num": num,
            "lang": language,
            "country": country,
        }
        if category:
            options["category"] = _enum_or_error(Category, category, "category")
        if age:
            options["age"] = _enum_or_error(Age, age, "age filter")

        results = self._scrape(
            lambda c: c.list(**options),
            what=f"browse the {collection} chart",
        )
        return [_to_app(item) for item in results or []]

    def scrape_suggestions(
        self, keyword: str, country: str = "us", language: str = "en"
    ) -> list[str]:
        """Play's own autocomplete for a keyword.

        These are the terms Play believes users actually type, which makes them
        the closest thing to keyword volume the store exposes publicly.
        """
        results = self._scrape(
            lambda c: c.suggest(term=keyword, lang=language, country=country),
            what=f"fetch suggestions for {keyword!r}",
        )
        return [untrusted(s) for s in results or []]

    def scrape_reviews(
        self,
        app_id: str,
        country: str = "us",
        language: str = "en",
        num: int = 100,
        sort: str = "NEWEST",
    ) -> ScrapedReviewPage:
        """Scrape public reviews, going back further than the Reviews API can.

        The authenticated Reviews API returns roughly one week. These go back as
        far as the store will paginate, which is the only route to history.
        """
        results = self._scrape(
            lambda c: c.reviews(
                app_id=app_id,
                lang=language,
                country=country,
                num=num,
                sort=_enum_or_error(Sort, sort, "sort order"),
            ),
            what=f"scrape reviews for {app_id}",
        )
        reviews = [_to_review(item) for item in results or []]
        return ScrapedReviewPage(
            app_id=app_id,
            country=country,
            language=language,
            sort=sort.upper(),
            reviews=reviews,
            count=len(reviews),
        )


# =============================================================================
# ASO metadata checks
# =============================================================================


def check_aso_metadata(
    title: str | None = None,
    short_description: str | None = None,
    full_description: str | None = None,
) -> dict[str, Any]:
    """Check listing text against Play's **policy** limits, not just the API's.

    The distinction matters and neither source project makes it: the Android
    Publisher API accepts a 50-character title, and Play then rejects the app at
    review because policy caps titles at 30. An upload that succeeds and a
    release that ships are different things, so a title between 31 and 50
    characters is reported as a warning rather than silently passing.
    """
    errors: list[str] = []
    warnings: list[str] = []
    lengths: dict[str, int] = {}

    if title is not None:
        lengths["title"] = len(title)
        if len(title) > API_TITLE_LENGTH:
            errors.append(
                f"Title is {len(title)} characters; the API rejects anything over "
                f"{API_TITLE_LENGTH}."
            )
        elif len(title) > POLICY_TITLE_LENGTH:
            warnings.append(
                f"Title is {len(title)} characters. The API accepts up to "
                f"{API_TITLE_LENGTH}, but Play policy caps titles at "
                f"{POLICY_TITLE_LENGTH} — this uploads fine and is then rejected at "
                f"review."
            )

    if short_description is not None:
        lengths["short_description"] = len(short_description)
        if len(short_description) > SHORT_DESCRIPTION_LENGTH:
            errors.append(
                f"Short description is {len(short_description)} characters; the limit "
                f"is {SHORT_DESCRIPTION_LENGTH}."
            )

    if full_description is not None:
        lengths["full_description"] = len(full_description)
        if len(full_description) > FULL_DESCRIPTION_LENGTH:
            errors.append(
                f"Full description is {len(full_description)} characters; the limit is "
                f"{FULL_DESCRIPTION_LENGTH}."
            )

    return {
        "valid": not errors,
        "lengths": lengths,
        "errors": errors,
        "warnings": warnings,
        "limits": {
            "title_policy": POLICY_TITLE_LENGTH,
            "title_api": API_TITLE_LENGTH,
            "short_description": SHORT_DESCRIPTION_LENGTH,
            "full_description": FULL_DESCRIPTION_LENGTH,
        },
    }
