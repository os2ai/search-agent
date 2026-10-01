import asyncio
import json
import logging
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from search_agent.models import RawSearchResult

logger = logging.getLogger(__name__)


class SearchProvider(Protocol):
    """A pluggable search backend.

    ``name`` doubles as the cache namespace and the label used in health
    checks and logs, so it must be stable and unique per provider.
    """

    # Read-only properties so a plain class attribute or a @property both satisfy them.
    @property
    def name(self) -> str: ...

    # Max results allowed to carry ``content`` into the synthesizer prompt, or
    # None for no cap. Enforced globally by ``search_multiple`` *after* the
    # per-query results are merged and deduplicated — a per-query cap would let
    # N queries stack up to N times the intended content, overflowing the LLM
    # context window. Providers that never populate content from search (e.g.
    # SearXNG) leave this None.
    @property
    def content_result_cap(self) -> int | None: ...

    async def search(self, client: httpx.AsyncClient, query: str) -> list[RawSearchResult]: ...

    async def health(self, client: httpx.AsyncClient) -> bool: ...


async def read_capped_json(response: httpx.Response, max_bytes: int) -> dict[str, Any] | None:
    """Stream a JSON response body, aborting if it exceeds ``max_bytes``.

    Why: ``response.json()`` reads the full body unbounded. Search backends
    are trusted peers today, but a misbehaving or compromised peer could
    otherwise OOM the worker (and poison the cache slot for the query).
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            logger.warning("Search response exceeded %d bytes; aborting read", max_bytes)
            return None
        chunks.append(chunk)
    try:
        loaded = json.loads(b"".join(chunks))
    except json.JSONDecodeError:
        logger.warning("Search backend returned non-JSON body")
        return None
    if not isinstance(loaded, dict):
        # A JSON list/scalar would make ``data.get(...)`` in the callers raise
        # AttributeError, which their ``except httpx.HTTPError`` wouldn't catch.
        logger.warning("Search backend returned non-object JSON body")
        return None
    return loaded


def is_valid_url(url: str) -> bool:
    """Accept only http/https URLs with a non-empty netloc."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def normalize_query(query: str) -> str:
    return query.strip().lower()


async def search_multiple(
    provider: SearchProvider, client: httpx.AsyncClient, queries: list[str]
) -> list[RawSearchResult]:
    """Execute multiple queries concurrently and deduplicate results by URL.

    Content-bearing results are then capped globally (see
    ``SearchProvider.content_result_cap``) so multiple queries can't stack up
    more enriched content than the synthesizer's context window can hold.
    """
    results_lists = await asyncio.gather(
        *(provider.search(client, q) for q in queries), return_exceptions=True
    )

    all_results: list[RawSearchResult] = []
    seen_urls: set[str] = set()
    for results in results_lists:
        # ``return_exceptions=True`` so one query raising (e.g. a provider bug
        # on a single malformed response) doesn't discard the other queries'
        # results — degrade to what succeeded instead of failing the search.
        if isinstance(results, BaseException):
            logger.warning("A search query failed and was skipped", exc_info=results)
            continue
        for r in results:
            if r.url not in seen_urls:
                seen_urls.add(r.url)
                all_results.append(r)

    # Enforce the content cap across all queries. Results keep their search
    # order (each query's backend-reranked results, first query first), so the
    # earliest — most relevant — results retain content; the rest fall back to
    # snippet-only.
    cap = provider.content_result_cap
    if cap is not None:
        for r in all_results[cap:]:
            r.content = None

    return all_results
