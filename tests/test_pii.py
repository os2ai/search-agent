"""Tests for the PII gate.

conftest.py pins SEARCH_AGENT_SEARCH_PII_CHECK_ENABLED=false for the rest of
the suite; these tests re-enable it (or patch `check_pii` directly) to cover
the gate's own behaviour.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from search_agent.config import settings
from search_agent.models import PiiCheck
from search_agent.pii import PII_REFUSAL_MESSAGE, PiiBlockedError, check_pii


def _guard_output(contains_pii: bool, reason: str = "") -> MagicMock:
    result = MagicMock()
    result.output = PiiCheck(contains_pii=contains_pii, reason=reason)
    return result


class TestCheckPii:
    async def test_disabled_allows_without_llm_call(self):
        with (
            patch.object(settings, "search_pii_check_enabled", False),
            patch("search_agent.pii.pii_guard") as guard,
        ):
            verdict = await check_pii("any query")
        assert verdict.allowed is True
        guard.run.assert_not_called()

    async def test_clean_text_allowed(self):
        with (
            patch.object(settings, "search_pii_check_enabled", True),
            patch("search_agent.pii.get_model", return_value=MagicMock()),
            patch("search_agent.pii.get_http_client", return_value=MagicMock()),
            patch("search_agent.pii.pii_guard") as guard,
        ):
            guard.run = AsyncMock(return_value=_guard_output(False))
            verdict = await check_pii("capital of Denmark")

        assert verdict.allowed is True
        guard.run.assert_called_once()

    async def test_pii_blocks_with_generic_message(self):
        with (
            patch.object(settings, "search_pii_check_enabled", True),
            patch("search_agent.pii.get_model", return_value=MagicMock()),
            patch("search_agent.pii.get_http_client", return_value=MagicMock()),
            patch("search_agent.pii.pii_guard") as guard,
        ):
            guard.run = AsyncMock(return_value=_guard_output(True, reason="phone number: 12345678"))
            verdict = await check_pii("find the phone number of Jane Doe 12345678")

        assert verdict.allowed is False
        # The refusal shown to callers must not echo the detected data.
        assert verdict.message == PII_REFUSAL_MESSAGE
        assert "12345678" not in verdict.message

    async def test_context_is_included_in_classification(self):
        with (
            patch.object(settings, "search_pii_check_enabled", True),
            patch("search_agent.pii.get_model", return_value=MagicMock()),
            patch("search_agent.pii.get_http_client", return_value=MagicMock()),
            patch("search_agent.pii.pii_guard") as guard,
        ):
            guard.run = AsyncMock(return_value=_guard_output(False))
            await check_pii("who is this?", context="email: jane@example.com")

        sent_text = guard.run.await_args.args[0]
        assert "Conversation context: email: jane@example.com" in sent_text

    async def test_fails_closed_on_llm_error(self):
        with (
            patch.object(settings, "search_pii_check_enabled", True),
            patch("search_agent.pii.get_model", return_value=MagicMock()),
            patch("search_agent.pii.get_http_client", return_value=MagicMock()),
            patch("search_agent.pii.pii_guard") as guard,
        ):
            guard.run = AsyncMock(side_effect=RuntimeError("LLM down"))
            verdict = await check_pii("any query")

        assert verdict.allowed is False
        assert verdict.message == PII_REFUSAL_MESSAGE

    async def test_fails_closed_on_timeout(self):
        async def slow_run(*args, **kwargs):
            await asyncio.sleep(5)

        with (
            patch.object(settings, "search_pii_check_enabled", True),
            patch.object(settings, "search_pii_check_timeout", 0),
            patch("search_agent.pii.get_model", return_value=MagicMock()),
            patch("search_agent.pii.get_http_client", return_value=MagicMock()),
            patch("search_agent.pii.pii_guard") as guard,
        ):
            guard.run = slow_run
            verdict = await check_pii("any query")

        assert verdict.allowed is False


class TestPipelineIntegration:
    @patch("search_agent.pipeline.analyze_synthesizer")
    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.check_pii")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_pipeline_refuses_before_any_search(
        self, mock_get_model, mock_get_http, mock_check, mock_planner, mock_search, mock_synth
    ):
        from search_agent.pipeline import run_search_pipeline

        mock_get_model.return_value = MagicMock()
        mock_get_http.return_value = MagicMock()
        mock_check.return_value = MagicMock(allowed=False, message=PII_REFUSAL_MESSAGE)

        result = await run_search_pipeline("find jane doe's home address")

        assert result.summary == PII_REFUSAL_MESSAGE
        assert result.sources == []
        # Nothing downstream may run — the PII must never reach planner or backend.
        mock_planner.run.assert_not_called()
        mock_search.assert_not_called()
        mock_synth.run.assert_not_called()

    @patch("search_agent.pipeline.analyze_synthesizer")
    @patch("search_agent.pipeline.search_multiple")
    @patch("search_agent.pipeline.query_planner")
    @patch("search_agent.pipeline.check_pii")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_pipeline_proceeds_when_gate_allows(
        self, mock_get_model, mock_get_http, mock_check, mock_planner, mock_search, mock_synth
    ):
        from search_agent.models import RawSearchResult, SearchResult, Source
        from search_agent.pipeline import run_search_pipeline

        mock_get_model.return_value = MagicMock()
        mock_get_http.return_value = MagicMock()
        mock_check.return_value = MagicMock(allowed=True, message="")

        planner_result = MagicMock()
        planner_result.output = ["capital of denmark"]
        mock_planner.run = AsyncMock(return_value=planner_result)
        mock_search.return_value = [
            RawSearchResult(title="R", url="https://example.com", snippet="S", engine="google")
        ]
        synth_result = MagicMock()
        synth_result.output = SearchResult(
            summary="Copenhagen [1].", sources=[Source(title="R", url="https://example.com")]
        )
        mock_synth.run = AsyncMock(return_value=synth_result)

        result = await run_search_pipeline("capital of denmark")

        assert result.summary == "Copenhagen [1]."
        mock_check.assert_called_once()

    @patch("search_agent.pipeline.check_pii")
    @patch("search_agent.pipeline.get_http_client")
    @patch("search_agent.pipeline.get_model")
    async def test_raw_pipeline_raises_on_block(self, mock_get_model, mock_get_http, mock_check):
        from search_agent.pipeline import run_search_pipeline_raw

        mock_get_model.return_value = MagicMock()
        mock_get_http.return_value = MagicMock()
        mock_check.return_value = MagicMock(allowed=False, message=PII_REFUSAL_MESSAGE)

        with pytest.raises(PiiBlockedError):
            await run_search_pipeline_raw("find jane doe's CPR number")


class TestMcpTool:
    @patch("search_agent.mcp_server.run_search_pipeline_raw", new_callable=AsyncMock)
    async def test_search_web_returns_error_on_pii_block(self, mock_raw):
        from search_agent.mcp_server import search_web

        mock_raw.side_effect = PiiBlockedError(PII_REFUSAL_MESSAGE)

        result = await search_web("find jane doe's home address")
        parsed = json.loads(result)

        assert parsed == {"error": PII_REFUSAL_MESSAGE}
