"""Opt-in evaluation of the PII gate against a synthetic case set.

Skipped unless ``RUN_PII_EVAL=1`` — unlike the rest of the suite this hits
the REAL configured LLM (``SEARCH_AGENT_LLM_*``), so it is a prompt/model
regression harness, not a unit test. The dataset is fully synthetic: all
names, numbers and addresses are invented (see the notes in the JSON).

Usage (inside the container, with a reachable LLM):

    RUN_PII_EVAL=1 docker compose run --rm --no-deps agent \\
        uv run pytest tests/test_pii_eval.py -v -s

Optional: ``PII_EVAL_CATEGORY=cpr_number,name_phone`` to run a subset.

Pass bar: every case must match its ``expected`` verdict. Tune the gate
prompt in ``config.py`` (or ``SEARCH_AGENT_SEARCH_PII_CHECK_PROMPT``) until
the whole set is green, then re-run the normal suite.
"""

import asyncio
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import pytest

from search_agent import deps
from search_agent.config import settings
from search_agent.pii import check_pii

DATA_FILE = Path(__file__).parent / "data" / "pii_eval_cases.json"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PII_EVAL") != "1",
    reason="set RUN_PII_EVAL=1 to run the PII gate evaluation (needs a real LLM)",
)

# Modest concurrency: enough to keep the run under a minute on a local
# Ollama, low enough not to trip rate limits on hosted endpoints.
_CONCURRENCY = 4


def _load_cases() -> list[dict]:
    cases = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    wanted = os.environ.get("PII_EVAL_CATEGORY")
    if wanted:
        cats = {c.strip() for c in wanted.split(",")}
        cases = [c for c in cases if c["category"] in cats]
    assert cases, "no eval cases selected — check PII_EVAL_CATEGORY"
    return cases


@pytest.fixture
async def _live_deps():
    """Initialize the shared model/clients exactly as the app lifespan does.

    Async so teardown runs on the test's own event loop — closing httpx
    clients from a different loop raises.
    """
    deps.init_shared_clients()
    yield
    await deps.close_shared_clients()


async def test_pii_gate_eval(_live_deps):
    cases = _load_cases()
    results: dict[str, bool] = {}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def run_case(case: dict) -> None:
        async with sem:
            started = time.monotonic()
            verdict = await check_pii(case["query"], case.get("context", ""))
            actual = "block" if not verdict.allowed else "allow"
            results[case["id"]] = actual == case["expected"]
            mark = "ok " if results[case["id"]] else "FAIL"
            print(
                f"[{mark}] {case['id']:16s} expected={case['expected']:5s} "
                f"actual={actual:5s} ({time.monotonic() - started:.1f}s)"
            )

    with pytest.MonkeyPatch.context() as mp:
        # conftest pins the gate off for the unit suite; the eval needs it on.
        mp.setattr(settings, "search_pii_check_enabled", True)
        await asyncio.gather(*(run_case(c) for c in cases))

    by_cat: dict[str, list[bool]] = defaultdict(list)
    for case in cases:
        by_cat[case["category"]].append(results[case["id"]])

    print("\n=== PII gate eval ===")
    for cat in sorted(by_cat):
        ok = sum(by_cat[cat])
        total = len(by_cat[cat])
        flag = "" if ok == total else "  <-- MISMATCHES"
        print(f"{cat:24s} {ok}/{total}{flag}")

    failures = [c["id"] for c in cases if not results[c["id"]]]
    assert not failures, f"{len(failures)}/{len(cases)} cases misclassified: {failures}"
