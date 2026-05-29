"""Regression tests for FastembedAdapter's graphiti EmbedderClient contract.

graphiti's ``EmbedderClient.create`` must return ONE flat ``list[float]`` for
list input (mirroring the reference ``OpenAIEmbedder`` which returns
``data[0].embedding``). A prior bug returned the whole nested batch
(``[[...384...]]``), which made graphiti build ``CAST(... AS FLOAT[1])`` and
broke every Kuzu cosine search on both read and write paths.

These mock the underlying embedder so they're fast and deterministic — they
test the adapter's reshaping contract, not the real fastembed model.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import storage.graph as graph_mod
from tools import embeddings as _embed

DIM = _embed.EMBEDDING_DIM
_VEC_A = [0.1] * DIM
_VEC_B = [0.2] * DIM


def _is_flat_vector(v) -> bool:
    return isinstance(v, list) and len(v) == DIM and all(isinstance(x, float) for x in v)


@pytest.mark.asyncio
async def test_create_list_input_returns_flat_vector():
    """create(['x']) must return a flat 384-float vector, not a nested batch."""
    with patch.object(graph_mod._embed, "aembed_batch",
                       new=AsyncMock(return_value=[_VEC_A])):
        out = await graph_mod.FastembedAdapter().create(input_data=["hello"])
    assert _is_flat_vector(out), f"expected flat {DIM}-vector, got nested/other: {type(out)}"
    assert not isinstance(out[0], list), "vector is double-nested — the FLOAT[1] bug"


@pytest.mark.asyncio
async def test_create_multi_element_list_returns_single_vector():
    """Per the contract, create() returns ONE vector even for multi-text input."""
    with patch.object(graph_mod._embed, "aembed_batch",
                       new=AsyncMock(return_value=[_VEC_A, _VEC_B])):
        out = await graph_mod.FastembedAdapter().create(input_data=["a", "b"])
    assert _is_flat_vector(out)
    assert out == _VEC_A  # first vector only


@pytest.mark.asyncio
async def test_create_str_input_returns_flat_vector():
    with patch.object(graph_mod._embed, "aembed",
                      new=AsyncMock(return_value=_VEC_A)):
        out = await graph_mod.FastembedAdapter().create(input_data="hello")
    assert _is_flat_vector(out)


@pytest.mark.asyncio
async def test_create_empty_batch_fallback_is_flat_zero_vector():
    """When the embedder yields nothing, fall back to a flat zero vector (not nested)."""
    with patch.object(graph_mod._embed, "aembed_batch",
                      new=AsyncMock(return_value=[])):
        out = await graph_mod.FastembedAdapter().create(input_data=["x"])
    assert _is_flat_vector(out)
    assert out == [0.0] * DIM


@pytest.mark.asyncio
async def test_create_batch_still_returns_nested_list():
    """create_batch keeps the many-vectors contract (one vector per input text)."""
    with patch.object(graph_mod._embed, "aembed_batch",
                      new=AsyncMock(return_value=[_VEC_A, _VEC_B])):
        out = await graph_mod.FastembedAdapter().create_batch(["a", "b"])
    assert isinstance(out, list) and len(out) == 2
    assert all(_is_flat_vector(v) for v in out)
