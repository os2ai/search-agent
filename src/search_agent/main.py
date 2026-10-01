import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from search_agent import cache
from search_agent.cache import close_cache, init_cache
from search_agent.config import settings
from search_agent.deps import close_shared_clients, get_http_client, init_shared_clients
from search_agent.mcp_server import mcp, streamable_http_app
from search_agent.models import SearchRequest, SearchResult
from search_agent.pipeline import run_search_pipeline
from search_agent.providers import get_provider, init_provider

_log_level = logging.DEBUG if settings.debug else logging.INFO
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
if settings.debug:
    logging.getLogger("search_agent").setLevel(logging.DEBUG)
    logging.getLogger("pydantic_ai").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_shared_clients()
    init_cache()
    init_provider()
    async with mcp.session_manager.run():
        yield
    await close_cache()
    await close_shared_clients()


class MaxBodySizeMiddleware:
    """Reject HTTP requests advertising a Content-Length over ``max_bytes``.

    Why: FastAPI/uvicorn don't cap request body size by default, so an
    oversized POST forces the worker to buffer arbitrary bytes before
    Pydantic's field validators ever run. Chunked-transfer clients without
    a Content-Length header bypass this check — acceptable here because
    JSON clients always send Content-Length, and the cap is also enforced
    upstream by the reverse proxy in real deployments.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for name, value in scope.get("headers") or []:
                if name == b"content-length":
                    try:
                        declared = int(value)
                    except ValueError:
                        break
                    if declared > self._max_bytes:
                        response = PlainTextResponse("Request body too large", status_code=413)
                        await response(scope, receive, send)
                        return
                    break
        await self._app(scope, receive, send)


app = FastAPI(title="Search Agent", lifespan=lifespan)
app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_request_body_bytes)


@app.get("/health")
async def health(deep: bool = False):
    if not deep:
        return {"status": "ok"}

    provider = get_provider()
    try:
        healthy = await provider.health(get_http_client())
    except Exception:
        # Never let the health endpoint 500 — an uninitialized client
        # (RuntimeError) or a provider that leaks an unexpected error should
        # both report "degraded", not crash the probe.
        logger.exception("Deep health check failed")
        healthy = False
    if healthy:
        return {"status": "ok", "provider": provider.name, "search_backend": "reachable"}
    return JSONResponse(
        status_code=503,
        content={"status": "degraded", "provider": provider.name, "search_backend": "unreachable"},
    )


@app.post("/api/v1/search", response_model=SearchResult)
async def search(request: SearchRequest) -> SearchResult | JSONResponse:
    """Run the search pipeline and return a sourced summary."""
    # Don't log `context` at INFO — it routinely carries conversation
    # history / PII. Truncate `query` for the same reason. Full text is
    # available at DEBUG (opt-in via SEARCH_AGENT_DEBUG=true).
    logger.info(
        "Search request: query=%r context_len=%d no_cache=%s",
        request.query[:80],
        len(request.context),
        request.no_cache,
    )
    logger.debug(
        "Search request (full): query=%r context=%r",
        request.query,
        request.context,
    )
    try:
        if request.no_cache:
            with cache.bypass():
                result = await run_search_pipeline(query=request.query, context=request.context)
        else:
            result = await run_search_pipeline(query=request.query, context=request.context)
        return result
    except Exception:
        logger.exception("Search pipeline failed")
        return JSONResponse(
            status_code=500,
            content={"summary": "Search failed due to an internal error.", "sources": []},
        )


app.mount("/", streamable_http_app())
