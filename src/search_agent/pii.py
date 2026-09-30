"""PII gate — refuse searches whose query or context contains personal data.

Why: under EU GDPR Art. 4(1) a name, phone number, address, e-mail, CPR number,
health detail, etc. counts as personal data even when it merely appears inside
a search query. Forwarding such a query to an external search backend (or an
LLM) would be uncontrolled processing of that data, so the pipeline checks the
incoming text with an LLM *before* any search is issued and refuses the
request when personal data is detected.

Design decisions (see the gate's three entry-point guarantees):

- Runs in ``_run_plan_and_search`` so both ``/api/v1/search``
  (``run_search_pipeline``) and the MCP ``search_web`` tool
  (``run_search_pipeline_raw``) are covered by a single choke point.
- Classifies query **and** conversation context together: the context feeds
  the query planner and can leak PII into the generated search queries.
- **Fails closed**: a timeout or error in the check is treated as "PII may be
  present" and the search is refused. Availability is deliberately traded for
  the stronger compliance guarantee.
- The refusal reason is a fixed generic string — never the detected data —
  and the model is instructed not to echo personal data into its output.
  Nothing is logged at INFO/DEBUG beyond what the rest of the pipeline
  already logs (query truncated, context length only).
"""

import asyncio
import logging
from dataclasses import dataclass

from search_agent.agents.pii_guard import pii_guard
from search_agent.config import settings
from search_agent.deps import PipelineDeps, get_http_client, get_model

logger = logging.getLogger(__name__)

# Shown to the caller (REST body / MCP tool text). Deliberately generic: it
# must not confirm *which* kind of personal data was found, and must never
# contain the data itself.
PII_REFUSAL_MESSAGE = (
    "Search refused: the request appears to contain personal data. "
    "To comply with GDPR, this service does not perform searches containing "
    "personal information. Please remove any personal details and try again."
)


@dataclass(frozen=True)
class PiiVerdict:
    """Outcome of the PII gate. ``allowed=False`` means: do not search."""

    allowed: bool
    message: str = ""


class PiiBlockedError(Exception):
    """Raised by the pipeline when the PII gate refuses a request.

    The message is always the fixed generic refusal text — never the query,
    which is itself the personal data we refuse to process.
    """


async def check_pii(query: str, context: str = "") -> PiiVerdict:
    """Classify query+context for personal data. Blocks on detection or error.

    Returns ``PiiVerdict(allowed=True)`` only when the gate is disabled or the
    model confidently reports no personal data.
    """
    if not settings.search_pii_check_enabled:
        return PiiVerdict(allowed=True)

    # The gate must see everything that flows downstream: the planner embeds
    # the full context in its prompt, and SearchRequest caps the fields at
    # 2000/10000 chars — so checking the full (validated) length keeps the
    # gate's view a superset of the planner's. Anything longer (e.g. an MCP
    # caller bypassing the REST model) is capped here to bound gate tokens.
    text = f"Query: {query[:2000]}"
    if context:
        text += f"\nConversation context: {context[:10000]}"

    try:
        async with asyncio.timeout(settings.search_pii_check_timeout):
            model = get_model()
            deps = PipelineDeps(http_client=get_http_client(), model=model)
            result = await pii_guard.run(text, deps=deps, model=model)
    except Exception as exc:
        # Fail closed: an unavailable/timeouting checker must not become a
        # bypass around the GDPR guarantee. Log only the exception *class* at
        # ERROR: output-validation errors can embed the model's raw output in
        # the message, and a misbehaving guard could echo personal data there.
        # Full traceback stays at DEBUG, where prompts are already logged.
        logger.error("PII check failed (%s); refusing search (fail closed)", type(exc).__name__)
        logger.debug("PII check failure details", exc_info=True)
        return PiiVerdict(allowed=False, message=PII_REFUSAL_MESSAGE)

    verdict = result.output
    if verdict.contains_pii:
        # Log only the model's generic category ("phone number"), never the
        # query text — the query itself is the PII we refuse to process.
        logger.warning("PII check blocked search (category: %s)", verdict.reason[:120] or "n/a")
        return PiiVerdict(allowed=False, message=PII_REFUSAL_MESSAGE)

    logger.debug("PII check passed")
    return PiiVerdict(allowed=True)
