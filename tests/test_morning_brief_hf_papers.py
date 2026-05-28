"""Phase G: HuggingFace daily papers — filter + fetch unit tests.

Tests are isolated from network and filesystem. The filter and fetch helpers
live in agents.morning_brief and are tested without touching the database or
the full proactive pipeline.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_paper(title: str, summary: str, arxiv_id: str = "2501.00001") -> dict:
    return {"title": title, "summary": summary, "url": f"https://arxiv.org/abs/{arxiv_id}"}


def _hf_api_item(title: str, summary: str, arxiv_id: str = "2501.00001") -> dict:
    """Shape that the HF daily_papers API returns."""
    return {"paper": {"title": title, "summary": summary, "id": arxiv_id}}


# ---------------------------------------------------------------------------
# _filter_papers_by_interests
# ---------------------------------------------------------------------------

class TestFilterPapersByInterests:
    def setup_method(self):
        import agents.morning_brief as mb
        self.fn = mb._filter_papers_by_interests

    def test_picks_matching_paper(self):
        papers = [
            _make_paper("Attention Is All You Need", "transformer attention mechanism"),
            _make_paper("Unrelated Paper", "some other topic about databases"),
            _make_paper("Another Unrelated One", "biology stuff"),
        ]
        interests = ["attention", "transformer"]
        result = self.fn(papers, interests, max_results=2)
        assert len(result) == 1
        assert result[0]["title"] == "Attention Is All You Need"

    def test_returns_empty_when_no_match(self):
        papers = [
            _make_paper("Quantum Gravity Models", "spacetime curvature and loops"),
            _make_paper("Protein Folding via Energy", "biochemistry structural analysis"),
        ]
        interests = ["attention", "diffusion", "neural", "language model"]
        result = self.fn(papers, interests, max_results=2)
        assert result == []

    def test_caps_at_max_results(self):
        papers = [
            _make_paper(f"Neural Paper {i}", f"deep learning neural network paper {i}")
            for i in range(10)
        ]
        interests = ["neural", "deep learning"]
        result = self.fn(papers, interests, max_results=2)
        assert len(result) == 2

    def test_empty_interests_returns_empty(self):
        papers = [_make_paper("Any Paper", "any content")]
        result = self.fn(papers, [], max_results=2)
        assert result == []

    def test_empty_papers_returns_empty(self):
        result = self.fn([], ["attention", "diffusion"], max_results=2)
        assert result == []

    def test_match_is_case_insensitive(self):
        papers = [_make_paper("DIFFUSION Models Survey", "DENOISING approach")]
        interests = ["diffusion", "denoising"]
        result = self.fn(papers, interests, max_results=2)
        assert len(result) == 1

    def test_summary_match_counts(self):
        papers = [_make_paper("Generic Title", "this paper is about attention mechanisms")]
        interests = ["attention"]
        result = self.fn(papers, interests, max_results=2)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _fetch_hf_daily_papers
# ---------------------------------------------------------------------------

class TestFetchHfDailyPapers:
    @pytest.mark.asyncio
    async def test_handles_http_failure(self):
        import agents.morning_brief as mb
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("agents.morning_brief.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await mb._fetch_hf_daily_papers(limit=5)

        assert result == []

    @pytest.mark.asyncio
    async def test_handles_malformed_json_non_list(self):
        import agents.morning_brief as mb
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"error": "unexpected shape"}

        with patch("agents.morning_brief.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await mb._fetch_hf_daily_papers(limit=5)

        assert result == []

    @pytest.mark.asyncio
    async def test_extracts_title_summary_url(self):
        import agents.morning_brief as mb
        items = [
            _hf_api_item("Attention Is All You Need", "transformer summary", "1706.03762"),
            _hf_api_item("Diffusion Beats GANs", "diffusion summary", "2105.05233"),
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = items

        with patch("agents.morning_brief.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await mb._fetch_hf_daily_papers(limit=5)

        assert len(result) == 2
        assert result[0]["title"] == "Attention Is All You Need"
        assert result[0]["summary"] == "transformer summary"
        assert result[0]["url"] == "https://arxiv.org/abs/1706.03762"
        assert result[1]["title"] == "Diffusion Beats GANs"
        assert result[1]["url"] == "https://arxiv.org/abs/2105.05233"

    @pytest.mark.asyncio
    async def test_skips_items_missing_title_or_summary(self):
        import agents.morning_brief as mb
        items = [
            {"paper": {"title": "Good Paper", "summary": "good summary", "id": "2501.00001"}},
            {"paper": {"title": "", "summary": "no title", "id": "2501.00002"}},
            {"paper": {"title": "No Summary", "summary": "", "id": "2501.00003"}},
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = items

        with patch("agents.morning_brief.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await mb._fetch_hf_daily_papers(limit=5)

        assert len(result) == 1
        assert result[0]["title"] == "Good Paper"

    @pytest.mark.asyncio
    async def test_handles_network_exception(self):
        """Any exception during fetch returns [] without re-raising."""
        import agents.morning_brief as mb

        with patch("agents.morning_brief.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(side_effect=ConnectionError("timeout"))
            mock_client_cls.return_value = mock_client

            result = await mb._fetch_hf_daily_papers(limit=5)

        assert result == []
