# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Search Agent — a FastAPI service that implements a 3-stage web search pipeline using Pydantic AI agents and SearXNG as the search backend. Also exposes search as an MCP tool.

## Commands

All Python commands run via docker compose (never directly on host). The Taskfile uses `docker compose exec` (requires running services), so start services first.

```bash
task up                                  # Start all services (required before other task commands)
task test                                # Run tests (uses exec)
task test -- tests/test_pipeline.py::TestSearchPipeline::test_pipeline_runs_all_stages  # Single test
task lint:check                          # Lint (src/ only)
task lint:format                         # Format (src/ only)
task lint                                # Lint + format check
task coding-standards:apply              # Lint fix + format
task build:image                         # Build and push prod image to ghcr.io/aarhusai/search-agent
task build:image TAG=v1.0.0              # With custom tag
```

Alternative: direct docker compose (does not require running services):

```bash
docker compose run --rm --no-deps agent uv run pytest                    # Run all tests
docker compose run --rm --no-deps agent uv run ruff check src tests      # Lint
docker compose run --rm --no-deps agent uv run ruff format src tests     # Format
```

## Architecture

### 3-Stage Search Pipeline (`pipeline.py`)

1. **Query Planner** — Decomposes complex questions into up to `search_max_queries` targeted search queries via LLM. Skipped for "simple" queries (word/`?` thresholds in settings, plus a hardcoded complexity regex) controlled by `_is_simple_query()`.
2. **Search Executor** — Calls the configured search provider (`search_provider`: `searxng` default, or `staan`) via HTTP, runs multiple queries concurrently, deduplicates by URL, caps at `search_max_results`. The Staan provider can return full page content / scored chunks per result directly into `RawSearchResult.content`.
3. **(Optional) Page Fetch** — If `search_fetch_page_content=true`, `fetch.py` fetches the top `search_fetch_max_pages` result URLs and extracts main text via `trafilatura`; the extracted text lands in `RawSearchResult.content` for the synthesizer. Guarded by content-type check, byte cap, and an SSRF filter that rejects private/loopback/link-local hosts. MCP path is snippet-only regardless.
4. **Analyze + Synthesize** — Single combined agent (`analyze_synthesizer`) that filters, ranks, extracts passages, and generates a cited summary with `[1]`, `[2]` style inline citations.

### Key Modules

- `main.py` — FastAPI app with `/health`, `/api/v1/search` endpoints. Mounts the MCP ASGI app at `/`, which serves the Streamable HTTP transport at `/mcp`. The mount must stay at module scope: `mcp.session_manager` (used by the lifespan) raises `RuntimeError` until `streamable_http_app()` has been called. Wraps the handler in `cache.bypass()` when `SearchRequest.no_cache=True`.
- `pipeline.py` — Orchestrates the 3 stages with timeout handling (default 90s). Planner output is cached per (normalized query, context, `YYYY-MM-DD`) — the date bucket is in the key because the prompt includes `datetime.now()`, so a TTL crossing midnight would otherwise leak a stale date.
- `config.py` — `Settings` class using pydantic-settings. All env vars use `SEARCH_AGENT_` prefix.
- `deps.py` — Shared httpx client and Pydantic AI model, initialized at app startup via lifespan.
- `cache.py` — Pluggable cache with a `CacheBackend` protocol and three implementations: `RedisBackend` (production, shared across pods), `InMemoryBackend` (tests/dev only — per-process, not safe for multi-pod prod), `DisabledBackend`. Fail-open on Redis errors/timeouts (300ms socket timeouts). Versioned namespace keys (`fetch:v1:…`). `bypass()` context manager + `no_cache` `ContextVar` avoid threading a flag through every call.
- `providers/` — Pluggable search backends behind a `SearchProvider` protocol (`base.py`, mirrors the `CacheBackend` pattern). `providers/__init__.py` is the registry: `init_provider()` (lifespan, fails fast on missing Staan key), `get_provider()` (lazy fallback for tests), `set_provider_for_testing()`. Shared helpers in `base.py`: `search_multiple` (concurrency + URL dedup, then enforces the provider's `content_result_cap` globally across all queries so multi-query content can't overflow the LLM context window), `is_valid_url`, `read_capped_json` (rejects non-object JSON bodies), `normalize_query`.
  - `providers/searxng.py` — `SearxngProvider` (default). Warns on `unresponsive_engines` (e.g. Brave rate-limited) and, at DEBUG, logs which engines contributed results. Results cached per `(normalize(query), searxng_url)`; empty result lists are not cached so transient outages can retry immediately.
  - `providers/staan.py` — `StaanProvider` for the Staan "Web for AI" API (Bearer auth, `GET /v2/search/web`, results under `web.results`). Enrichment via `staan_enrichment`: `full_content` (markdown page body) or `extra_snippets` (scored chunks) → `RawSearchResult.content`, capped per result (`staan_content_max_chars`). The count cap (`staan_content_max_results` top reranked results keep content) is applied globally in `search_multiple` across all queries — not inside `search()` — so the cached payload is cap-independent and the synthesizer prompt stays bounded regardless of query count. Response read is capped at `staan_max_response_bytes` (larger than SearXNG's since `full_content` returns whole page bodies). Never log its request headers — they carry the API key.
- `fetch.py` — Optional per-result page fetch with trafilatura extraction. Concurrent, with content-type / byte-size / SSRF guards. Skips results whose `content` is already populated (e.g. by the Staan provider) — only content-less results consume `search_fetch_max_pages` slots. `trafilatura.extract` is run via `asyncio.to_thread` because it's sync. `_fetch_one` wraps `_fetch_one_uncached` with a cache (positive TTL for extracted text, shorter negative TTL for `None`/failed fetches).
- `models.py` — `SearchRequest` (with optional `no_cache` flag), `RawSearchResult` (with optional `content` field populated by fetch), `SearchResult`, `Source`.
- `mcp_server.py` — MCP server (`MCPServer` from the mcp 2.x SDK) exposing `search_web` tool. Uses `run_search_pipeline_raw` (steps 1+2 only, no LLM synthesis) so callers get raw results for their own citation handling (e.g. Open WebUI). Does not expose `no_cache`; MCP callers always hit the shared cache. `streamable_http_app()` builds the ASGI app that `main.py` mounts — mcp 2.x takes `transport_security` (the `mcp_allowed_hosts` DNS-rebinding allowlist) on the transport factory rather than the server constructor, so it is applied there.
- `agents/` — Pydantic AI agent definitions. `analyze_synthesizer.py` is the combined analyze+synthesize agent.

### Data Flow

`SearchRequest(query, context, no_cache)` → query planner (Redis-cached) → `[str]` queries → search provider (Redis-cached; SearXNG or Staan) → `[RawSearchResult]` → fetch_pages (Redis-cached per URL, optional, skips results that already have content) → analyze_synthesizer → `SearchResult(summary, sources)`

## Configuration

All env vars use `SEARCH_AGENT_` prefix (via pydantic-settings). Key settings:

- `SEARCH_AGENT_DEBUG` (default: `false`) — sets `search_agent` and `pydantic_ai` loggers to DEBUG and logs full agent prompts + outputs, plus the SearXNG engine list per query. Does **not** enable `httpx`/`httpcore` DEBUG (those leak `Authorization` headers and full LLM response bodies); enable those manually if you need them.
- `SEARCH_AGENT_LLM_BASE_URL` (default: `http://localhost:11434/v1`) — OpenAI-compatible endpoint (Ollama, etc.)
- `SEARCH_AGENT_LLM_API_KEY`, `SEARCH_AGENT_LLM_MODEL` (default: `llama3`)
- `SEARCH_AGENT_LLM_STRICT_TOOLS` (default: `true`) — OpenAI strict tool definitions
- `SEARCH_AGENT_SEARCH_PROVIDER` (default: `searxng`) — search backend: `searxng` or `staan`. Deep health check (`/health?deep=true`) reports `provider` + `search_backend` fields.
- `SEARCH_AGENT_SEARXNG_URL` (default: `http://searxng:8080`)
- `SEARCH_AGENT_STAAN_API_KEY` — **required when provider is `staan`** (startup fails without it). Plus `SEARCH_AGENT_STAAN_URL` (`https://api.staan.ai`), `SEARCH_AGENT_STAAN_MARKET` (`en-us`), `SEARCH_AGENT_STAAN_TIMEOUT` (10s), `SEARCH_AGENT_STAAN_ENRICHMENT` (`full_content` | `extra_snippets` | `none`), `SEARCH_AGENT_STAAN_MAX_SNIPPETS` (3), `SEARCH_AGENT_STAAN_MIN_SCORE` (0.1), `SEARCH_AGENT_STAAN_CONTENT_MAX_CHARS` (5000), `SEARCH_AGENT_STAAN_CONTENT_MAX_RESULTS` (5, top reranked results that keep content, enforced globally across all planner queries — with the char cap this bounds the synthesizer prompt for 32k-context models), `SEARCH_AGENT_STAAN_MAX_RESPONSE_BYTES` (10_000_000)
- `SEARCH_AGENT_SEARXNG_TIMEOUT` (15s), `SEARCH_AGENT_SEARCH_PIPELINE_TIMEOUT` (90s), `SEARCH_AGENT_LLM_TIMEOUT` (60s)
- `SEARCH_AGENT_DATETIME_TIMEZONE` (default: `UTC`), `SEARCH_AGENT_DATETIME_FORMAT` — used in query planner prompts
- `SEARCH_AGENT_MCP_ALLOWED_HOSTS` (default: `["search-agent:8001","localhost:8001"]`) — Host header allowlist for the MCP transport; a mismatched `Host` gets a 421
- `SEARCH_AGENT_SEARCH_SKIP_PLANNER_FOR_SIMPLE_QUERIES` (default: `true`)
- `SEARCH_AGENT_SEARCH_MAX_QUERIES` (default: `3`) — cap planner output
- `SEARCH_AGENT_SEARCH_MAX_RESULTS` (default: `15`) — cap results reaching the synthesizer
- `SEARCH_AGENT_SEARCH_SIMPLE_QUERY_MAX_WORDS` (default: `15`), `SEARCH_AGENT_SEARCH_SIMPLE_QUERY_MAX_QUESTIONS` (default: `1`) — `_is_simple_query` thresholds
- `SEARCH_AGENT_SEARCH_FETCH_PAGE_CONTENT` (default: `false`) — fetch and extract main text from result pages via trafilatura; when enabled the synthesizer sees a `content` field per result in addition to the SearXNG `snippet`. MCP path (`search_web`) is snippet-only regardless.
- `SEARCH_AGENT_SEARCH_FETCH_MAX_PAGES` (5), `SEARCH_AGENT_SEARCH_FETCH_TIMEOUT` (10s), `SEARCH_AGENT_SEARCH_FETCH_MAX_CHARS` (5000), `SEARCH_AGENT_SEARCH_FETCH_MAX_BYTES` (2_000_000)
- `SEARCH_AGENT_CACHE_BACKEND` (default: `redis`) — `redis` for production (shared across pods), `memory` for tests/dev only (per-process), or `disabled`.
- `SEARCH_AGENT_CACHE_REDIS_URL` (default: `redis://redis:6379/0`) — used when backend is `redis`.
- `SEARCH_AGENT_CACHE_FETCH_TTL` (3600s), `SEARCH_AGENT_CACHE_FETCH_NEGATIVE_TTL` (300s), `SEARCH_AGENT_CACHE_SEARXNG_TTL` (300s), `SEARCH_AGENT_CACHE_STAAN_TTL` (300s), `SEARCH_AGENT_CACHE_PLANNER_TTL` (21600s) — TTLs per cache namespace. Planner cache key is date-bucketed so it rolls daily even inside TTL.
- Agent prompts overridable via `SEARCH_AGENT_SEARCH_QUERY_PLANNER_PROMPT`, `SEARCH_AGENT_SEARCH_ANALYZE_SYNTHESIZE_PROMPT`

## Testing

Tests mock all external services (LLM and SearXNG). `conftest.py` sets `SEARCH_AGENT_*` env vars before any module imports — this ordering matters because `config.py` reads env vars at module level via `settings = Settings()`. `conftest.py` also pins `SEARCH_AGENT_CACHE_BACKEND=disabled` so existing tests assert fresh behaviour; cache-specific tests opt in by swapping the module-level backend via `cache.set_backend_for_testing(InMemoryBackend())` (see the `in_memory_backend` fixture in `tests/test_cache.py`). `RedisBackend` is exercised with `fakeredis`.

pytest-asyncio is configured with `asyncio_mode = "auto"` so async tests don't need the `@pytest.mark.asyncio` decorator.

## Code Style

- Python 3.12+, ruff with rules: E, F, I, N, W, UP, B, RUF
- Line length: 100
- Async-first — all I/O uses async httpx and FastAPI async handlers

## Docker

Multi-stage Dockerfile with `dev` and `prod` targets. docker-compose defines three services: `agent` (container port 8001), `searxng` (container port 8080), and `redis` (container port 6379, `redis:7-alpine` with 256MB `maxmemory` + `allkeys-lru`, data persisted at `.docker/data/redis/`). All three have health checks — ports are not host-mapped (random host ports unless overridden). Services connect via a bridge `app` network; `agent` is also on an external `frontend` network. Source is volume-mounted for live reload in dev. Build target is controlled by `ENV` variable (defaults to `dev`). Note: `Taskfile.yml` currently sets `SERVICE: search-agent`, which no longer matches the compose service name `agent` — task commands that use `docker compose exec {{.SERVICE}}` will fail until that var is updated. Use `docker compose exec agent …` directly in the meantime.

## Important rules

- **Never read `.env` files** — they contain secrets. Use `docker-compose.yml` or `config.py` to understand env vars.
- **Always use docker compose** to run Python commands — never run `uv` or Python directly on the host.
