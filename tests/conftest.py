import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Set test env vars before any imports that read settings
os.environ.setdefault("SEARCH_AGENT_LLM_BASE_URL", "http://localhost:11434/v1")
os.environ.setdefault("SEARCH_AGENT_LLM_API_KEY", "test-key")
os.environ.setdefault("SEARCH_AGENT_LLM_MODEL", "test-model")
os.environ.setdefault("SEARCH_AGENT_SEARXNG_URL", "http://localhost:8080")

# Pin settings the tests depend on so local .env overrides don't leak in
os.environ["SEARCH_AGENT_SEARCH_MAX_RESULTS"] = "15"
os.environ["SEARCH_AGENT_SEARCH_FETCH_PAGE_CONTENT"] = "false"
os.environ["SEARCH_AGENT_SEARCH_PROVIDER"] = "searxng"
# Existing tests assume no caching; cache-specific tests opt in per-test.
os.environ["SEARCH_AGENT_CACHE_BACKEND"] = "disabled"


@pytest.fixture(autouse=True)
def _reset_provider():
    """Drop any provider a test resolved or injected, so tests stay isolated."""
    yield
    from search_agent.providers import set_provider_for_testing

    set_provider_for_testing(None)


def make_stream_mock(json_body: dict[str, Any]) -> MagicMock:
    """Build a mock async context manager mimicking ``client.stream(...)``.

    SearXNG fetch uses streaming reads to enforce a size cap, so tests need
    something that supports ``async with`` and ``aiter_bytes``.
    """
    body_bytes = json.dumps(json_body).encode("utf-8")

    async def aiter_bytes():
        yield body_bytes

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.aiter_bytes = aiter_bytes

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm
