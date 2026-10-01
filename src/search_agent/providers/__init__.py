"""Pluggable search backends.

The active provider is selected by ``settings.search_provider`` and resolved
once — eagerly via :func:`init_provider` during app lifespan startup (so
misconfiguration fails the boot), or lazily on first use in contexts that
don't run the lifespan (tests).
"""

from search_agent.config import settings
from search_agent.providers.base import SearchProvider
from search_agent.providers.searxng import SearxngProvider
from search_agent.providers.staan import StaanProvider

_provider: SearchProvider | None = None


def _create_provider(name: str) -> SearchProvider:
    if name == "staan":
        return StaanProvider()
    return SearxngProvider()


def init_provider() -> None:
    """Resolve the provider from settings. Call during lifespan startup.

    Raises RuntimeError on misconfiguration so uvicorn refuses to start
    instead of failing on the first search request.
    """
    global _provider
    _provider = _create_provider(settings.search_provider)


def get_provider() -> SearchProvider:
    """Return the active search provider, resolving lazily if needed."""
    global _provider
    if _provider is None:
        _provider = _create_provider(settings.search_provider)
    return _provider


def set_provider_for_testing(provider: SearchProvider | None) -> None:
    """Swap the active provider. Only for tests."""
    global _provider
    _provider = provider
