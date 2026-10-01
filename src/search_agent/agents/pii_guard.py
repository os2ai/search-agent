from pydantic_ai import Agent

from search_agent.config import settings
from search_agent.deps import PipelineDeps, create_model
from search_agent.models import PiiCheck

pii_guard = Agent(
    create_model(),
    output_type=PiiCheck,
    instructions=settings.search_pii_check_prompt,
    deps_type=PipelineDeps,
)
