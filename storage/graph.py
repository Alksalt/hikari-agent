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
import sqlite3
from datetime import UTC
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
    """Return the singleton Graphiti instance. First call builds indices.

    On partial-init failure (FTS index creation, build_indices_and_constraints,
    etc.), explicitly tear down the KuzuDriver before raising — otherwise its
    underlying kuzu.Database holds the file lock, and the NEXT get_graph() call
    creates a SECOND Database against the same path, which Kuzu rejects with
    the misleading "Database path cannot be a directory" message (it's actually
    a same-process lock conflict).
    """
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
            raise RuntimeError(
                "OPENROUTER_API_KEY required for graphiti (cheap LLM via openrouter)"
            )
        model = str(_cfg.get("graph.llm_model", "deepseek/deepseek-v4-flash"))
        llm_config = LLMConfig(
            api_key=api_key,
            model=model,
            base_url="https://openrouter.ai/api/v1",
        )
        client = OpenAIGenericClient(config=llm_config)
        embedder = FastembedAdapter()
        driver: KuzuDriver | None = None
        try:
            driver = KuzuDriver(db=str(graph_path))
            # graphiti_core ≥0.29 checks driver._database before cloning for group routing,
            # but KuzuDriver never initialises this attribute.  Pin it so the check is a no-op.
            driver._database = "hikari_chat"
            # graphiti_core 0.29 added kuzu FTS indices in graph_queries.py but omitted them
            # from KuzuDriver.setup_schema() and made build_indices_and_constraints() a no-op.
            # Create them explicitly; ignore errors if they already exist on a reopened DB.
            import kuzu as _kuzu
            _fts_conn = _kuzu.Connection(driver.db)
            for _fts_q in [
                "CALL CREATE_FTS_INDEX('Episodic', 'episode_content', ['content', 'source', 'source_description']);",
                "CALL CREATE_FTS_INDEX('Entity', 'node_name_and_summary', ['name', 'summary']);",
                "CALL CREATE_FTS_INDEX('Community', 'community_name', ['name']);",
                "CALL CREATE_FTS_INDEX('RelatesToNode_', 'edge_name_and_fact', ['name', 'fact']);",
            ]:
                try:
                    _fts_conn.execute(_fts_q)
                except Exception:
                    pass  # already exists on a reopened database
            _fts_conn.close()
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
        except Exception:
            # Tear down the partial driver so the next attempt can succeed.
            # Without this, the failed driver's kuzu.Database stays alive and
            # holds the file lock, causing every retry to hit Kuzu's
            # "Database path cannot be a directory" same-process lock error.
            if driver is not None:
                try:
                    db_obj = getattr(driver, "db", None)
                    if db_obj is not None and hasattr(db_obj, "close"):
                        db_obj.close()
                except Exception:
                    logger.exception("get_graph: kuzu.Database.close() failed during cleanup")
                try:
                    close_fn = getattr(driver, "close", None)
                    if callable(close_fn):
                        maybe_coro = close_fn()
                        if hasattr(maybe_coro, "__await__"):
                            await maybe_coro
                except Exception:
                    logger.exception("get_graph: KuzuDriver.close() failed during cleanup")
                # Drop the reference so GC can run.
                del driver
            raise


class FastembedAdapter(EmbedderClient):
    """Local-only embedder satisfying graphiti's EmbedderClient interface.
    Wraps tools.embeddings (fastembed + BAAI/bge-small-en-v1.5, 384-dim).
    Keeps Hikari off any hosted embeddings API."""

    async def create(self, input_data):
        if isinstance(input_data, str):
            return await _embed.aembed(input_data)
        if isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
            batch = await _embed.aembed_batch(input_data)
            return batch if batch else [[0.0] * _embed.EMBEDDING_DIM for _ in input_data]
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
    fact_id: int | None = None,
) -> bool:
    """Dual-write helper: add_episode wrapped in try/except. Returns True on
    success, False on failure (logged). Never raises."""
    from datetime import datetime
    if reference_time is None:
        reference_time = datetime.now(UTC)
    elif isinstance(reference_time, str):
        try:
            reference_time = datetime.fromisoformat(reference_time)
        except ValueError:
            reference_time = datetime.now(UTC)
    if fact_id is not None:
        source_description = f"fact_id:{fact_id}|{source_description}"
    try:
        g = await get_graph()
        result = await g.add_episode(
            name=name,
            episode_body=episode_body,
            source=source,
            source_description=source_description,
            reference_time=reference_time,
            group_id=group_id,
        )
    except Exception:
        logger.exception("graph.add_episode failed (non-fatal)")
        return False
    if result is None:
        logger.warning("graph.add_episode returned None — treating as failure (fact_id=%s name=%s)", fact_id, name)
        return False
    episode = getattr(result, "episode", result)
    has_id = bool(getattr(episode, "uuid", None) or getattr(episode, "id", None))
    if not has_id:
        logger.warning("graph.add_episode returned without uuid/id (fact_id=%s name=%s repr=%r)", fact_id, name, result)
        return False
    return True


def schedule_episode(
    name: str,
    episode_body: str,
    source_id: int,
    *,
    source_description: str = "",
) -> int | None:
    """Write an outbox row for this episode. Returns outbox row id or None on dedup.

    Replaces the old fire-and-forget pattern: instead of scheduling an async
    task, we insert a pending row into graph_outbox (same SQLite DB). The
    scheduler's process_outbox worker picks it up every 30s and calls Graphiti.
    source_id is required — it is the facts.id that owns this episode.
    """
    import json as _json
    from datetime import UTC, datetime

    from storage import db

    payload = {
        "v": 1,
        "name": name,
        "episode_body": episode_body,
        "source": "text",
        "source_description": source_description or "fact",
        "group_id": "hikari_chat",
        "reference_time": datetime.now(UTC).isoformat(),
        "fact_id": source_id,
    }
    try:
        return db.graph_outbox_insert("facts", source_id, _json.dumps(payload))
    except sqlite3.IntegrityError:
        return None
    except Exception:
        logger.warning("schedule_episode: outbox insert failed (fact_id=%s)", source_id, exc_info=True)
        return None


async def process_outbox(limit: int = 50, max_per_call: int = 10) -> dict:
    """Drain pending outbox rows by calling Graphiti's add_episode.

    Returns {"polled": N, "sent": s, "failed": f, "skipped": 0}.
    Never raises — Graphiti failures mark rows failed and continue.

    Infrastructure errors (OPENROUTER_API_KEY missing, GRAPHITI_ENABLED=false)
    are transient: rows stay 'pending' so the outbox drains once the env is fixed.
    """
    import json as _json

    from storage import db

    # Detect infra-level blockers up front so the error string propagates to
    # graph_outbox_mark_failed's transient-check logic, keeping rows 'pending'.
    if not _graphiti_enabled():
        return {"polled": 0, "sent": 0, "failed": 0, "skipped": 0}
    if not os.environ.get("OPENROUTER_API_KEY"):
        rows = db.graph_outbox_pending(limit=limit)
        rows = rows[:max_per_call]
        for row in rows:
            db.graph_outbox_mark_failed(row["id"], "OPENROUTER_API_KEY not set (transient)")
        return {"polled": len(rows), "sent": 0, "failed": 0, "skipped": len(rows)}

    rows = db.graph_outbox_pending(limit=limit)
    rows = rows[:max_per_call]
    out = {"polled": len(rows), "sent": 0, "failed": 0, "skipped": 0}
    if not rows:
        return out
    for row in rows:
        try:
            payload = _json.loads(row["payload_json"])
        except (ValueError, TypeError) as e:
            db.graph_outbox_mark_failed(row["id"], f"payload_json invalid: {e}")
            out["failed"] += 1
            continue
        try:
            ok = await add_episode_safe(
                name=payload.get("name", f"fact_{row['source_id']}"),
                episode_body=payload.get("episode_body", ""),
                source_description=payload.get("source_description", "fact"),
                reference_time=payload.get("reference_time"),
                fact_id=payload.get("fact_id") or row.get("source_id"),
            )
        except Exception as e:
            db.graph_outbox_mark_failed(row["id"], f"add_episode_safe raised: {e}")
            out["failed"] += 1
            continue
        if ok:
            db.graph_outbox_mark_sent(row["id"])
            out["sent"] += 1
        else:
            db.graph_outbox_mark_failed(row["id"], "add_episode_safe returned False")
            out["failed"] += 1
    return out


_GRAPHITI_DISABLED_LOGGED = False


def _graphiti_enabled() -> bool:
    return os.environ.get("GRAPHITI_ENABLED", "true").strip().lower() not in ("false", "0")


async def search(query: str, *, group_id: str = "hikari_chat", num_results: int = 8) -> list:
    """Read-side helper. Returns list of EntityEdge objects from Graphiti.

    Returns [] immediately (one boot-time INFO, not per-call ERROR) when
    GRAPHITI_ENABLED=false so recall.py produces zero ERROR log lines.
    """
    global _GRAPHITI_DISABLED_LOGGED
    if not _graphiti_enabled():
        if not _GRAPHITI_DISABLED_LOGGED:
            logger.info("graph.search: GRAPHITI_ENABLED=false — graph reads are disabled")
            _GRAPHITI_DISABLED_LOGGED = True
        return []
    try:
        g = await get_graph()
        return await g.search(query=query, group_ids=[group_id], num_results=num_results)
    except Exception:
        logger.exception("graph.search failed")
        return []
