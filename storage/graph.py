"""Embedded Graphiti+Kuzu graph for long-term memory.

Singleton initialized lazily. The subprocess holds the Kuzu file lock.
"""
from __future__ import annotations

import asyncio
import logging
import os as _os

# Opt out of graphiti-core's PostHog telemetry by default; users can override
# by exporting GRAPHITI_TELEMETRY_ENABLED=true.
_os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

import os
from pathlib import Path

from graphiti_core import Graphiti
from graphiti_core.driver.kuzu_driver import KuzuDriver
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.nodes import EpisodeType

from agents import config as _cfg
from tools import embeddings as _embed

logger = logging.getLogger(__name__)

_GRAPH: Graphiti | None = None
_GRAPH_LOCK = asyncio.Lock()


def _graph_path() -> Path:
    """Path to the Kuzu database FILE (not a directory). Kuzu manages
    the on-disk format itself."""
    data_dir = Path(os.environ.get("HIKARI_DATA_DIR") or "data")
    return data_dir / "hikari.kuzu"


async def get_graph() -> Graphiti:
    """Return the singleton Graphiti instance. First call builds indices."""
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH
    async with _GRAPH_LOCK:
        if _GRAPH is not None:
            return _GRAPH
        graph_path = _graph_path()
        # Owner-only access to the parent data dir; Kuzu will create the
        # actual db file inside on first open.
        graph_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            graph_path.parent.chmod(0o700)
        except OSError:
            pass
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY required for graphiti (cheap LLM via openrouter)")
        model = str(_cfg.get("graph.llm_model", "deepseek/deepseek-chat"))
        llm_config = LLMConfig(
            api_key=api_key,
            model=model,
            base_url="https://openrouter.ai/api/v1",
        )
        client = OpenAIGenericClient(config=llm_config)
        embedder = FastembedAdapter()
        driver = KuzuDriver(db=str(graph_path))
        g = Graphiti(graph_driver=driver, llm_client=client, embedder=embedder)
        await g.build_indices_and_constraints()
        # Lock down the kuzu file once Kuzu has created it.
        try:
            if graph_path.exists():
                graph_path.chmod(0o600)
        except OSError:
            pass
        _GRAPH = g
        logger.info("graph: ready (kuzu@%s) at %s", _kuzu_version(), graph_path)
        return g


class FastembedAdapter(EmbedderClient):
    """Local-only embedder satisfying graphiti's EmbedderClient interface.
    Wraps tools.embeddings (fastembed + BAAI/bge-small-en-v1.5, 384-dim).
    Keeps Hikari off any hosted embeddings API."""

    async def create(self, input_data):
        if isinstance(input_data, str):
            return await _embed.aembed(input_data)
        if isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
            batch = await _embed.aembed_batch(input_data)
            return batch[0] if batch else [0.0] * _embed.EMBEDDING_DIM
        raise TypeError(f"FastembedAdapter: unsupported input type {type(input_data).__name__}")

    async def create_batch(self, input_data_list):
        return await _embed.aembed_batch(input_data_list)


def _kuzu_version() -> str:
    try:
        import kuzu
        return kuzu.__version__
    except Exception:
        return "?"


async def add_episode_safe(
    name: str,
    episode_body: str,
    *,
    source: EpisodeType = EpisodeType.text,
    source_description: str = "",
    reference_time=None,
    group_id: str = "hikari_chat",
) -> bool:
    """Dual-write helper: add_episode wrapped in try/except. Returns True on
    success, False on failure (logged). Never raises."""
    from datetime import datetime, timezone
    if reference_time is None:
        reference_time = datetime.now(timezone.utc)
    try:
        g = await get_graph()
        await g.add_episode(
            name=name,
            episode_body=episode_body,
            source=source,
            source_description=source_description,
            reference_time=reference_time,
            group_id=group_id,
        )
        return True
    except Exception:
        logger.exception("graph.add_episode failed (non-fatal)")
        return False


def schedule_episode(
    name: str,
    episode_body: str,
    *,
    source_description: str = "",
    group_id: str = "hikari_chat",
) -> bool:
    """Fire-and-forget schedule of add_episode_safe. Safe to call from sync
    contexts: if no event loop is running, silently no-ops (logged). Returns
    True if a task was scheduled."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("schedule_episode: no running loop (sync caller %s); skipping", name)
        return False
    loop.create_task(add_episode_safe(
        name=name,
        episode_body=episode_body,
        source=EpisodeType.text,
        source_description=source_description,
        group_id=group_id,
    ))
    return True


async def search(query: str, *, group_id: str = "hikari_chat", num_results: int = 8) -> list:
    """Read-side helper. Returns list of EntityEdge objects from Graphiti."""
    try:
        g = await get_graph()
        return await g.search(query=query, group_ids=[group_id], num_results=num_results)
    except Exception:
        logger.exception("graph.search failed")
        return []
