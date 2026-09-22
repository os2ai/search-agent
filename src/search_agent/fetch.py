import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx
import trafilatura

from search_agent import cache
from search_agent.config import settings
from search_agent.models import RawSearchResult
from search_agent.providers.base import is_valid_url

logger = logging.getLogger(__name__)

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


async def _host_is_public(host: str) -> bool:
    """Return True only if every resolved address for host is globally routable.

    Why ``is_global`` and not a denylist: ``is_global`` is the inverse of
    every reserved range (private, loopback, link-local, multicast, reserved,
    unspecified, …) so we don't have to remember to extend the list when a
    new range is reserved. Notably this catches ``0.0.0.0`` / ``::`` (which
    fall back to loopback when connected to) and ``240.0.0.0/4``.
    """
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return bool(infos)


async def _fetch_one(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
    max_chars: int,
    max_bytes: int,
) -> str | None:
    """Fetch a URL and return extracted main text, or None on any failure.

    Results are cached: successful extracts for `cache_fetch_ttl`, failures
    (invalid URL, SSRF block, HTTP error, empty extract) for the shorter
    `cache_fetch_negative_ttl` so transient failures recover quickly.
    """
    cache_key = cache.make_key("fetch", url, max_chars)
    cached = await cache.get_json(cache_key)
    if cached is not None:
        logger.debug("fetch cache hit url=%s ok=%s", url, cached.get("ok"))
        return cached.get("text") if cached.get("ok") else None

    text = await _fetch_one_uncached(client, url, timeout, max_chars, max_bytes)
    if text:
        await cache.set_json(cache_key, {"ok": True, "text": text}, ttl=settings.cache_fetch_ttl)
    else:
        await cache.set_json(cache_key, {"ok": False}, ttl=settings.cache_fetch_negative_ttl)
    return text


async def _fetch_one_uncached(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
    max_chars: int,
    max_bytes: int,
) -> str | None:
    body = await _fetch_with_validated_redirects(client, url, timeout, max_bytes)
    if not body:
        return None

    text = await asyncio.to_thread(
        trafilatura.extract,
        body,
        include_comments=False,
        include_tables=False,
        output_format="markdown",
    )
    if not text:
        return None
    return text[:max_chars]


async def _fetch_with_validated_redirects(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
    max_bytes: int,
) -> str | None:
    """Fetch a URL, manually following redirects and re-validating each hop.

    Why: httpx's ``follow_redirects=True`` blindly chases 3xx responses, so a
    public URL that redirects to an internal address (cloud metadata, Redis,
    Traefik dashboard, etc.) bypasses a one-shot SSRF check. We disable auto
    redirect and re-run the public-host check on every hop.

    A narrow DNS rebinding window remains between each hop's resolve and
    httpx's connect — closing it fully requires connecting by pinned IP,
    which is incompatible with HTTPS certificate verification absent a
    custom transport.
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not is_valid_url(current):
            return None
        host = urlparse(current).hostname
        if not host or not await _host_is_public(host):
            logger.debug("Fetch blocked (non-public host): %s", current)
            return None
        try:
            async with client.stream(
                "GET", current, timeout=timeout, follow_redirects=False
            ) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    current = str(httpx.URL(current).join(location))
                    continue
                if response.status_code >= 400:
                    return None
                content_type = response.headers.get("content-type", "").lower()
                if not content_type.startswith("text/html"):
                    return None

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        logger.debug("Fetch cut short (size cap %d exceeded): %s", max_bytes, current)
                        return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                    chunks.append(chunk)
                return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        except (httpx.HTTPError, UnicodeDecodeError):
            logger.debug("Fetch failed: %s", current, exc_info=True)
            return None
    logger.debug("Fetch aborted (max redirects exceeded): %s", url)
    return None


async def fetch_pages(
    client: httpx.AsyncClient,
    results: list[RawSearchResult],
    max_pages: int,
    timeout: float,
    max_chars: int,
    max_bytes: int,
) -> list[RawSearchResult]:
    """Fetch and extract main content for up to max_pages results.

    Results that already carry content (a provider like Staan can return
    full page text with the search response) are left untouched; only the
    first max_pages results without content are fetched. Any fetch failure
    leaves content=None so the pipeline continues with just the snippet.
    """
    if max_pages <= 0 or not results:
        return results

    targets = [r for r in results if not r.content][:max_pages]
    if not targets:
        return results
    extracted = await asyncio.gather(
        *(_fetch_one(client, r.url, timeout, max_chars, max_bytes) for r in targets)
    )
    for result, text in zip(targets, extracted, strict=True):
        result.content = text

    fetched = sum(1 for t in extracted if t)
    logger.info("Fetched %d/%d pages with extractable content", fetched, len(targets))
    return results
