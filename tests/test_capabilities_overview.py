"""capabilities_overview — the 'what can you do' answer, generated from the catalog."""
import json

import pytest

from tools.capabilities.overview import capabilities_overview


@pytest.mark.asyncio
async def test_overview_returns_grouped_areas():
    res = await capabilities_overview.handler({})
    data = res["data"]
    domains = {a["domain"] for a in data["areas"]}
    assert "gmail" in domains and "scheduling" in domains
    assert "router" not in domains and "meta" not in domains  # hidden
    for area in data["areas"]:
        assert area["tool_count"] > 0
        assert isinstance(area["examples"], list)


@pytest.mark.asyncio
async def test_overview_includes_try_phrases_from_menu():
    res = await capabilities_overview.handler({})
    assert "what can you do?" in res["data"]["try"]


@pytest.mark.asyncio
async def test_overview_excludes_wildcards():
    res = await capabilities_overview.handler({})
    text = json.dumps(res["data"])
    assert "*" not in text
