"""Local embeddings via fastembed + BAAI/bge-small-en-v1.5.

384-dim, ~50MB model, runs on CPU. Outputs are L2-normalized.
Single-user scale (a few thousand rows) — no need for a hosted API.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastembed import TextEmbedding

logger = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384


@lru_cache(maxsize=1)
def _model() -> TextEmbedding:
    from fastembed import TextEmbedding

    logger.info("loading embedding model %s (one-time)", MODEL_NAME)
    return TextEmbedding(model_name=MODEL_NAME)


def embed(text: str) -> list[float]:
    """Sync embed. Returns a 384-float list (L2-normalized)."""
    text = (text or "").strip()
    if not text:
        return [0.0] * EMBEDDING_DIM
    gen = _model().embed([text])
    vecs = list(gen)
    return vecs[0].tolist() if vecs else [0.0] * EMBEDDING_DIM


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Sync batch embed."""
    if not texts:
        return []
    cleaned = [(t or "").strip() or " " for t in texts]
    gen = _model().embed(cleaned)
    return [v.tolist() for v in gen]


async def aembed(text: str) -> list[float]:
    return await asyncio.to_thread(embed, text)


async def aembed_batch(texts: list[str]) -> list[list[float]]:
    return await asyncio.to_thread(embed_batch, texts)
