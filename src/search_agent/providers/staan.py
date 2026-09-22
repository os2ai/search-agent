import logging

import httpx
from pydantic import ValidationError

from search_agent import cache
from search_agent.config import settings
from search_agent.models import RawSearchResult
from search_agent.providers.base import is_valid_url, normalize_query, read_capped_json

logger = logging.getLogger(__name__)


def _build_params(query: str) -> dict:
    params: dict = {"q": query, "market": settings.staan_market}
    if settings.staan_enrichment == "full_content":
        # Markdown only — HTML would need extraction before it's useful to the LLM.
        params["full_content"] = "markdown"
    elif settings.staan_enrichment == "extra_snippets":
        params["extra_snippets"] = "true"
        params["max_snippets"] = settings.staan_max_snippets
        params["min_score"] = settings.staan_min_score
    return params


def _extract_content(item: dict) -> str | None:
    """Pull enrichment text from a result item, capped at staan_content_max_chars.

    The cap bounds the synthesizer prompt: results are JSON-dumped into a
    single LLM call, and uncapped ``full_content`` is entire page bodies.
    """
    full = item.get("full_content")
    if isinstance(full, dict) and isinstance(full.get("text"), str) and full["text"]:
        return full["text"][: settings.staan_content_max_chars]
    chunks = item.get("extra_snippets")
    if isinstance(chunks, list):
        texts = [c["chunk"] for c in chunks if isinstance(c, dict) and c.get("chunk")]
        if texts:
            return "\n\n".join(texts)[: settings.staan_content_max_chars]
    return None


def _to_result(item: dict) -> RawSearchResult | None:
    # Coerce with ``or ""`` (not ``get(..., "")``) so an explicit JSON null —
    # ``get`` only falls back on a *missing* key — becomes an empty string
    # rather than None, which the required ``str`` fields would reject.
    title = item.get("title") or ""
    url = item.get("url") or ""
    if not (title and url and is_valid_url(url)):
        return None
    # Advisory only; keep it only if the peer actually sent a string (some
    # APIs return an epoch int, which the ``str | None`` field would reject).
    published = item.get("published_date")
    if not isinstance(published, str):
        published = None
    try:
        return RawSearchResult(
            title=title,
            url=url,
            snippet=item.get("snippet") or "",
            engine="staan",
            content=_extract_content(item),
            published_date=published,
        )
    except ValidationError:
        # A single malformed item must not sink the whole query's results.
        logger.warning("Skipping malformed Staan result for url=%r", url)
        return None


class StaanProvider:
    """Search backend backed by the Staan "Web for AI" API.

    https://docs.staan.ai/docs/web-for-ai — can return full page content
    (``full_content=markdown``) or semantically scored chunks
    (``extra_snippets``) per result, mapped into ``RawSearchResult.content``.
    """

    name = "staan"

    def __init__(self, *args, **kwargs):
        if not settings.staan_api_key:
            raise RuntimeError(
                "SEARCH_AGENT_STAAN_API_KEY is required when SEARCH_AGENT_SEARCH_PROVIDER=staan"
            )

    @property
    def content_result_cap(self) -> int:
        # Enforced globally in search_multiple across all queries, not per query.
        return settings.staan_content_max_results

    async def search(self, client: httpx.AsyncClient, query: str) -> list[RawSearchResult]:
        """Execute a single search query against Staan and return structured results."""
        # Every knob that changes the cached payload is part of the key — except
        # the API key (a secret that doesn't affect the shape) and
        # staan_content_max_results (applied post-cache in search_multiple, so a
        # single query caches content for all results regardless of it).
        cache_key = cache.make_key(
            self.name,
            normalize_query(query),
            settings.staan_url,
            settings.staan_market,
            settings.staan_enrichment,
            settings.staan_max_snippets,
            settings.staan_min_score,
            settings.staan_content_max_chars,
        )
        cached = await cache.get_json(cache_key)
        if cached is not None:
            logger.debug("staan cache hit query=%r", query)
            return [RawSearchResult.model_validate(item) for item in cached]

        try:
            async with client.stream(
                "GET",
                f"{settings.staan_url}/v2/search/web",
                params=_build_params(query),
                headers={"Authorization": f"Bearer {settings.staan_api_key}"},
                timeout=settings.staan_timeout,
            ) as response:
                response.raise_for_status()
                data = await read_capped_json(response, settings.staan_max_response_bytes)
        except httpx.HTTPError:
            # Don't log params or headers here — the Authorization header
            # carries the API key.
            logger.exception("Staan request failed for query: %s", query)
            return []
        if data is None:
            return []

        raw_results = (data.get("web") or {}).get("results", [])
        logger.debug("Staan response for query=%r: %d results", query, len(raw_results))

        results = [r for item in raw_results if (r := _to_result(item)) is not None]

        # NB: content is capped globally in search_multiple (across all queries),
        # not here — so the cache stores content for every result and the cap can
        # be retuned without invalidating cached entries.
        if results:
            await cache.set_json(
                cache_key,
                [r.model_dump(mode="json") for r in results],
                ttl=settings.cache_staan_ttl,
            )
        return results

    async def health(self, client: httpx.AsyncClient) -> bool:
        """Probe the Staan API with a minimal search.

        There is no documented health endpoint, so this issues a real (billed,
        rate-limited) request. Acceptable: the deep health check is operator
        invoked only — the compose healthcheck hits the shallow ``/health``.
        A 401/403 correctly reports unhealthy (bad or missing API key).
        """
        try:
            response = await client.get(
                f"{settings.staan_url}/v2/search/web",
                params={"q": "ping", "market": settings.staan_market},
                headers={"Authorization": f"Bearer {settings.staan_api_key}"},
                timeout=5.0,
            )
            return response.is_success
        except httpx.HTTPError:
            logger.exception("Staan health check failed")
            return False
