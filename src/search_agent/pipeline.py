import asyncio
import json
import logging
import re
import time
import zoneinfo
from datetime import datetime

from search_agent import cache
from search_agent.agents.analyze_synthesizer import analyze_synthesizer
from search_agent.agents.query_planner import query_planner
from search_agent.config import settings
from search_agent.deps import PipelineDeps, get_http_client, get_model
from search_agent.fetch import fetch_pages
from search_agent.models import RawSearchResult, SearchResult
from search_agent.providers import get_provider
from search_agent.providers.base import search_multiple

logger = logging.getLogger(__name__)

# Patterns that suggest a multi-faceted query needing decomposition
_COMPLEX_QUERY_PATTERNS = re.compile(
    # eng:
    r"\b(compare|vs\.?|versus|difference between|pros and cons|advantages and disadvantages"
    # da:
    r"|sammenlign|modsat|omvendt|forskellen mellem|fordele og ulemper|pros og cons"
    # eng:
    r"|on one hand|similarities and differences"
    # da:
    r"|på den ene side|ligheder og forskelle)\b",
    re.IGNORECASE,
)


def _is_simple_query(query: str) -> bool:
    """Heuristic: return True if the query is simple enough to skip the planner."""
    words = query.split()
    if len(words) > settings.search_simple_query_max_words:
        return False
    if _COMPLEX_QUERY_PATTERNS.search(query):
        return False
    if query.count("?") > settings.search_simple_query_max_questions:
        return False
    return True


async def _run_plan_and_search(query: str, context: str = "") -> list[RawSearchResult]:
    """Steps 1+2: plan queries and execute them against the search provider."""
    model = get_model()
    http_client = get_http_client()
    deps = PipelineDeps(http_client=http_client, model=model)

    # Step 1: Plan search queries (or skip for simple queries)
    tz = zoneinfo.ZoneInfo(settings.datetime_timezone)
    now = datetime.now(tz=tz).strftime(settings.datetime_format)

    if settings.search_skip_planner_for_simple_queries and _is_simple_query(query):
        search_queries = [query]
        logger.info("Skipped query planner (simple query): %s", search_queries)
    else:
        # Date bucket in the cache key (not just TTL) because the planner prompt
        # contains `now`; a TTL crossing midnight would otherwise return a plan
        # pinned to yesterday's date.
        today_bucket = datetime.now(tz=tz).strftime("%Y-%m-%d")
        planner_key = cache.make_key("planner", query.strip().lower(), context, today_bucket)
        cached_plan = await cache.get_json(planner_key)
        # Defend against a corrupted / poisoned cache entry: only trust a
        # list-of-strings shape. Anything else would silently feed garbage
        # (or attacker-chosen) values into SearXNG.
        if cached_plan is not None and not (
            isinstance(cached_plan, list) and all(isinstance(q, str) for q in cached_plan)
        ):
            logger.warning("Discarded malformed planner cache entry: %r", cached_plan)
            cached_plan = None
        if cached_plan is not None:
            search_queries = cached_plan[: settings.search_max_queries]
            logger.info(
                "Query planner cache hit (%d queries): %s",
                len(search_queries),
                search_queries,
            )
        else:
            planner_start = time.monotonic()
            prompt = f"Current date and time: {now}\nQuestion: {query}"
            if context:
                prompt += f"\nConversation context: {context}"
            logger.debug("Query planner prompt: %s", prompt)
            plan_result = await query_planner.run(prompt, deps=deps, model=model)
            logger.debug("Query planner raw output: %r", plan_result.output)
            search_queries = plan_result.output[: settings.search_max_queries]
            planner_time = time.monotonic() - planner_start
            logger.info(
                "Query planner produced %d queries in %.1fs: %s",
                len(search_queries),
                planner_time,
                search_queries,
            )
            if search_queries:
                await cache.set_json(planner_key, search_queries, ttl=settings.cache_planner_ttl)

    # Step 2: Execute searches (no LLM, just HTTP → search backend)
    provider = get_provider()
    search_start = time.monotonic()
    raw_results = await search_multiple(provider, http_client, search_queries)
    search_time = time.monotonic() - search_start
    logger.info(
        "Search (%s) returned %d results in %.1fs", provider.name, len(raw_results), search_time
    )

    return raw_results


async def run_search_pipeline(query: str, context: str = "") -> SearchResult:
    """Run the full search pipeline: plan → search → analyze+synthesize."""
    try:
        async with asyncio.timeout(settings.search_pipeline_timeout):
            return await _run_search_pipeline(query, context)
    except TimeoutError:
        logger.warning(
            "Search pipeline timed out after %ds for query: %s",
            settings.search_pipeline_timeout,
            query,
        )
        return SearchResult(summary="Search timed out. Please try again.", sources=[])


async def _run_search_pipeline(query: str, context: str = "") -> SearchResult:
    """Inner pipeline logic wrapped by the timeout in run_search_pipeline."""
    pipeline_start = time.monotonic()

    raw_results = await _run_plan_and_search(query, context)

    if not raw_results:
        return SearchResult(summary="No search results found.", sources=[])

    # Limit results to avoid overwhelming the LLM
    raw_results = raw_results[: settings.search_max_results]

    # Step 3: Analyze and synthesize in one pass
    model = get_model()
    http_client = get_http_client()
    deps = PipelineDeps(http_client=http_client, model=model)

    if settings.search_fetch_page_content:
        fetch_start = time.monotonic()
        raw_results = await fetch_pages(
            http_client,
            raw_results,
            max_pages=settings.search_fetch_max_pages,
            timeout=settings.search_fetch_timeout,
            max_chars=settings.search_fetch_max_chars,
            max_bytes=settings.search_fetch_max_bytes,
        )
        logger.info("Page fetch step took %.1fs", time.monotonic() - fetch_start)

    analyze_synth_start = time.monotonic()
    raw_json = json.dumps([r.model_dump(exclude_none=True) for r in raw_results])
    synth_prompt = (
        f"Question: {query}\n"
        f"--- BEGIN EXTERNAL SEARCH RESULTS ---\n"
        f"{raw_json}\n"
        f"--- END EXTERNAL SEARCH RESULTS ---"
    )
    logger.debug("Analyze+synthesize prompt: %s", synth_prompt)
    result = await analyze_synthesizer.run(synth_prompt, deps=deps, model=model)
    logger.debug("Analyze+synthesize output: %r", result.output)
    analyze_synth_time = time.monotonic() - analyze_synth_start

    # Drop sources the LLM invented: the model can be coaxed by adversarial
    # snippet/content text into emitting attacker-chosen URLs that the UI
    # would render as clickable citations. Keep only URLs that came from the
    # raw result set.
    allowed_urls = {r.url for r in raw_results}
    dropped = [s.url for s in result.output.sources if s.url not in allowed_urls]
    if dropped:
        logger.warning("Dropped %d fabricated source URL(s): %s", len(dropped), dropped)
    result.output.sources = [s for s in result.output.sources if s.url in allowed_urls]

    total_time = time.monotonic() - pipeline_start
    logger.info(
        "Search pipeline: analyze_synthesize=%.1fs total=%.1fs",
        analyze_synth_time,
        total_time,
    )

    return result.output


async def run_search_pipeline_raw(query: str, context: str = "") -> list[RawSearchResult]:
    """Run steps 1+2 only (plan + search). Returns raw results with timeout."""
    try:
        async with asyncio.timeout(settings.search_pipeline_timeout):
            results = await _run_plan_and_search(query, context)
            return results[: settings.search_max_results]
    except TimeoutError:
        logger.warning(
            "Search pipeline (raw) timed out after %ds for query: %s",
            settings.search_pipeline_timeout,
            query,
        )
        return []
