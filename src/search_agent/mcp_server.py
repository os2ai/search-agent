import json

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from search_agent.config import settings
from search_agent.models import RawSearchResult
from search_agent.pii import PiiBlockedError
from search_agent.pipeline import run_search_pipeline_raw

mcp = MCPServer("search-agent")


def streamable_http_app():
    """Build the MCP Streamable HTTP ASGI app (served at ``/mcp``).

    mcp 2.x moved ``transport_security`` off the server constructor onto the
    transport factory, so the DNS-rebinding host allowlist is applied here.
    """
    return mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            allowed_hosts=settings.mcp_allowed_hosts,
        ),
    )


@mcp.tool()
async def search_web(query: str, context: str = "") -> str:
    """Search the web for current information.

    Use this when the user asks about recent events, facts you're unsure about,
    or anything that benefits from up-to-date web data.

    Args:
        query: The search query to look up on the web.
        context: Optional conversation context to help refine the search.

    Returns:
        JSON array of search results, each with 'title', 'link', and 'snippet'
        keys. If the PII gate refuses the request, returns a
        JSON object with a single 'error' key explaining the refusal instead.
    """
    try:
        raw_results: list[RawSearchResult] = await run_search_pipeline_raw(
            query=query, context=context
        )
    except PiiBlockedError as exc:
        # Surface the refusal to the calling LLM rather than pretending the
        # web has nothing to say. str(exc) is the generic refusal message.
        return json.dumps({"error": str(exc)})
    formatted = [{"title": r.title, "link": r.url, "snippet": r.snippet} for r in raw_results]
    return json.dumps(formatted)
