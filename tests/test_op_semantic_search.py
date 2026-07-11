"""Tier 2: semantic_search macro op handler OS invariants (ADR-0033 Phase 1;
FP-0057 Phase 2a renamed from `recall`).

Tests use FakeEmbeddingProvider (monkeypatched into the semantic_search
handler's provider-direct embed call, #1303 S-I.4) and a real
SqliteIndexBackend for end-to-end semantic_search dispatch. The
co-vet #1 multi-model-correctness suite at the bottom of this file is the
CRITICAL falsifying coverage for the recall -> semantic_search rename.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.embedding.provider import EmbedBatchResult
from reyn.data.index.backend import ChunkRecord
from reyn.data.index.backends.sqlite import SqliteIndexBackend
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import SemanticSearchIROp
from reyn.security.permissions.permissions import PermissionDecl

# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------

class FakeEmbeddingProvider:
    """Returns a fixed 3-dim vector [1.0, 0.0, 0.0] for any text."""

    def __init__(self) -> None:
        self._batch_size = 10

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        vectors = [[1.0, 0.0, 0.0] for _ in texts]
        return EmbedBatchResult(vectors=vectors, model=model, total_tokens=len(texts))

    def estimate_tokens(self, texts: list[str]) -> int:
        return len(texts)

    def get_dimension(self, model: str) -> int:
        return 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
    )


async def _seed(workspace_root: Path, source: str, chunks: list[ChunkRecord]) -> None:
    backend = SqliteIndexBackend(workspace_root=workspace_root)
    await backend.write(source, chunks, mode="append")


def _chunk(text: str, vec: list[float], ch: str, model: str = "m") -> ChunkRecord:
    return ChunkRecord(
        text=text,
        vector=vec,
        metadata={
            "source_path": "f.txt",
            "source_type": "generic",
            "content_hash": ch,
            "embedding_model": model,
            "chunk_index": 0,
            "size_tokens": len(text.split()),
            "parent_context": None,
        },
        score=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recall_happy_path_returns_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: recall macro embeds query, queries source, returns merged top-K."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    import os
    monkeypatch.chdir(tmp_path)

    await _seed(tmp_path, "src1", [
        _chunk("relevant result", [1.0, 0.0, 0.0], "r1"),
        _chunk("less relevant",   [0.0, 1.0, 0.0], "r2"),
    ])

    ctx = _make_ctx(tmp_path)
    op = SemanticSearchIROp(
        kind="semantic_search",
        query="test query",
        sources=["src1"],
        top_k=5,
        embedding_model="standard",
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    # With the fake provider returning [1,0,0], "relevant result" has score ≈ 1
    assert len(result["chunks"]) >= 1
    assert result["mode"] in ("semantic", "fallback", "mixed")


@pytest.mark.asyncio
async def test_recall_empty_sources_returns_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: recall with empty sources list returns fallback immediately."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    import os
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    op = SemanticSearchIROp(
        kind="semantic_search",
        query="anything",
        sources=[],
        top_k=5,
        embedding_model="standard",
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert result["chunks"] == []
    assert result["mode"] == "fallback"


@pytest.mark.asyncio
async def test_recall_merges_multiple_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: recall merges chunks from multiple sources, sorted by score."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    import os
    monkeypatch.chdir(tmp_path)

    await _seed(tmp_path, "src1", [_chunk("from source 1", [1.0, 0.0, 0.0], "s1c1")])
    await _seed(tmp_path, "src2", [_chunk("from source 2", [1.0, 0.0, 0.0], "s2c1")])

    ctx = _make_ctx(tmp_path)
    op = SemanticSearchIROp(
        kind="semantic_search",
        query="test",
        sources=["src1", "src2"],
        top_k=10,
        embedding_model="standard",
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    texts = [c["text"] for c in result["chunks"]]
    assert "from source 1" in texts
    assert "from source 2" in texts


@pytest.mark.asyncio
async def test_recall_top_k_limits_merged_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: recall top_k limits total chunks returned across all sources."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    import os
    monkeypatch.chdir(tmp_path)

    chunks1 = [_chunk(f"a{i}", [1.0, 0.0, 0.0], f"a{i}") for i in range(5)]
    chunks2 = [_chunk(f"b{i}", [1.0, 0.0, 0.0], f"b{i}") for i in range(5)]
    await _seed(tmp_path, "src1", chunks1)
    await _seed(tmp_path, "src2", chunks2)

    ctx = _make_ctx(tmp_path)
    op = SemanticSearchIROp(
        kind="semantic_search",
        query="test",
        sources=["src1", "src2"],
        top_k=3,
        embedding_model="standard",
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert not result["chunks"][3:]  # top_k=3 caps result count


@pytest.mark.asyncio
async def test_recall_mode_all_semantic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: when all sources return semantic results, mode='semantic'."""
    fake = FakeEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    import os
    monkeypatch.chdir(tmp_path)

    await _seed(tmp_path, "s1", [_chunk("chunk", [1.0, 0.0, 0.0], "c1")])

    ctx = _make_ctx(tmp_path)
    op = SemanticSearchIROp(
        kind="semantic_search",
        query="anything",
        sources=["s1"],
        top_k=5,
        embedding_model="standard",
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    # s1 has content, embed returns a vector → semantic
    assert result["mode"] == "semantic"


@pytest.mark.asyncio
async def test_recall_embed_failure_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: a provider-direct embed failure → graceful fallback (empty
    chunks, mode=fallback) + a recall_embed_failed event (#1303 S-I.4 replaced
    the embed sub-op error-status check with a try/except around provider.embed)."""
    class _RaisingProvider:
        def __init__(self) -> None:
            self._batch_size = 10

        async def embed(self, texts: list[str], model: str) -> Any:
            raise RuntimeError("embedding endpoint down")

        def estimate_tokens(self, texts: list[str]) -> int:
            return 0

        def get_dimension(self, model: str) -> int:
            return 3

    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: _RaisingProvider())
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    op = SemanticSearchIROp(
        kind="semantic_search", query="q", sources=["s1"], top_k=5, embedding_model="standard",
    )
    result = await execute_op(op, ctx)

    assert result["chunks"] == []
    assert result["mode"] == "fallback"
    assert any(e.type == "semantic_search_embed_failed" for e in ctx.events.all())


# ---------------------------------------------------------------------------
# co-vet #1 — multi-model correctness (CRITICAL)
# ---------------------------------------------------------------------------

class _PerModelEmbeddingProvider:
    """Real EmbeddingProvider-protocol instance returning a DIFFERENT query
    vector per model (mirrors two distinct embedding spaces). Also records
    every (texts, model) call so a test can assert embed was called exactly
    ONCE per DISTINCT model (never once-for-all, never once-per-source)."""

    _QUERY_VECTORS = {
        "modelA": [1.0, 0.0, 0.0],
        "modelB": [0.0, 1.0, 0.0],
    }

    def __init__(self) -> None:
        self._batch_size = 10
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        self.calls.append((tuple(texts), model))
        vec = self._QUERY_VECTORS.get(model, [0.0, 0.0, 1.0])
        return EmbedBatchResult(vectors=[vec for _ in texts], model=model, total_tokens=len(texts))

    def estimate_tokens(self, texts: list[str]) -> int:
        return len(texts)

    def get_dimension(self, model: str) -> int:
        return 3


@pytest.mark.asyncio
async def test_semantic_search_multi_model_embeds_once_per_distinct_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: co-vet #1 (CRITICAL) — a 2-source call where the sources are
    indexed under DIFFERENT embedding models embeds the query ONCE PER
    DISTINCT MODEL (never once-for-all with a single shared vector), and
    each source is queried with its OWN model's matching vector — never a
    caller-supplied `embedding_model` (auto-adopt from the index).

    FALSIFY: `src_a` (indexed under "modelA") holds a chunk whose vector
    ONLY matches modelA's query vector, and `src_b` (indexed under
    "modelB") holds a chunk whose vector ONLY matches modelB's query
    vector. If the implementation regressed to embed-once-for-all (a single
    shared query vector for every source, the pre-fix `recall` bug class),
    at most ONE of the two "_relevant" chunks could ever rank top — the
    other source's genuinely-relevant chunk would be invisible / outranked
    by its "_irrelevant" sibling, which shares the WRONG shared vector's
    space. This test goes RED under that regression.
    """
    fake = _PerModelEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)
    monkeypatch.chdir(tmp_path)

    # src_a: indexed under modelA. Only the modelA query vector [1,0,0]
    # correctly ranks "a_relevant" over "a_irrelevant".
    await _seed(tmp_path, "src_a", [
        _chunk("a_relevant",   [1.0, 0.0, 0.0], "a1", model="modelA"),
        _chunk("a_irrelevant", [0.0, 1.0, 0.0], "a2", model="modelA"),
    ])
    # src_b: indexed under modelB. Only the modelB query vector [0,1,0]
    # correctly ranks "b_relevant" over "b_irrelevant".
    await _seed(tmp_path, "src_b", [
        _chunk("b_relevant",   [0.0, 1.0, 0.0], "b1", model="modelB"),
        _chunk("b_irrelevant", [1.0, 0.0, 0.0], "b2", model="modelB"),
    ])

    ctx = _make_ctx(tmp_path)
    op = SemanticSearchIROp(
        kind="semantic_search",
        query="cross-model query",
        sources=["src_a", "src_b"],
        # top_k=2 is the TOTAL cap across both model groups (round-robin
        # combine) — large enough for each group's single best match to
        # survive the combine step.
        top_k=2,
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    texts = [c["text"] for c in result["chunks"]]
    # Each source's GENUINELY relevant chunk is present — proof each source
    # was queried in ITS OWN model's vector space, not a shared/wrong one.
    assert "a_relevant" in texts, f"src_a's correct-model match missing: {texts}"
    assert "b_relevant" in texts, f"src_b's correct-model match missing: {texts}"
    assert "a_irrelevant" not in texts
    assert "b_irrelevant" not in texts

    # embed was called exactly ONCE per DISTINCT model (2 calls total for
    # 2 sources under 2 distinct models) — never once-for-all (1 call) and
    # never once-per-source when models coincide (that case is covered by
    # the single-model tests above, which stay at 1 call).
    models_called = sorted(model for _texts, model in fake.calls)
    assert models_called == ["modelA", "modelB"], (
        f"expected exactly one embed call per distinct model, got: {fake.calls}"
    )


@pytest.mark.asyncio
async def test_semantic_search_single_model_multi_source_embeds_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a same-model multi-source call embeds the query exactly ONCE
    (not once per source) — the multi-model grouping does not regress the
    pre-existing single-model-multi-source cost posture."""
    fake = _PerModelEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)
    monkeypatch.chdir(tmp_path)

    await _seed(tmp_path, "s1", [_chunk("from s1", [1.0, 0.0, 0.0], "c1", model="modelA")])
    await _seed(tmp_path, "s2", [_chunk("from s2", [1.0, 0.0, 0.0], "c2", model="modelA")])

    ctx = _make_ctx(tmp_path)
    op = SemanticSearchIROp(
        kind="semantic_search", query="q", sources=["s1", "s2"], top_k=5,
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    # unpack: exactly one embed call (not once-per-source) — raises ValueError
    # on the wrong count instead of pinning a bare length.
    (only_call,) = fake.calls
    assert only_call[1] == "modelA"


@pytest.mark.asyncio
async def test_semantic_search_auto_adopts_source_model_ignores_caller_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: co-vet #1 — a source's recorded embedding model wins over a
    caller-supplied `embedding_model` (auto-adopt, never caller-supplied per
    source). Passing a mismatching `embedding_model` does not change which
    model the already-indexed source is queried with."""
    fake = _PerModelEmbeddingProvider()
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)
    monkeypatch.chdir(tmp_path)

    await _seed(tmp_path, "src_a", [
        _chunk("a_relevant", [1.0, 0.0, 0.0], "a1", model="modelA"),
    ])

    ctx = _make_ctx(tmp_path)
    # Caller passes "modelB" as a fallback default — irrelevant here since
    # src_a already has a recorded model ("modelA").
    op = SemanticSearchIROp(
        kind="semantic_search", query="q", sources=["src_a"], top_k=5,
        embedding_model="modelB",
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    assert [c["text"] for c in result["chunks"]] == ["a_relevant"]
    # Only modelA was ever called — the source's OWN recorded model, not the
    # caller-supplied fallback.
    assert [model for _texts, model in fake.calls] == ["modelA"]


# ---------------------------------------------------------------------------
# co-vet fix — query-embed redaction-egress seam (symmetric with index_update)
# ---------------------------------------------------------------------------

class _RecordingEmbeddingProvider:
    """Real EmbeddingProvider-protocol instance that records the EXACT texts
    it was handed — the egress boundary to the external embedding API. Used to
    prove the query text reaching the provider has been redacted (never the
    raw secret)."""

    def __init__(self) -> None:
        self._batch_size = 10
        self.received_texts: list[str] = []

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        self.received_texts.extend(texts)
        return EmbedBatchResult(
            vectors=[[1.0, 0.0, 0.0] for _ in texts], model=model, total_tokens=len(texts),
        )

    def estimate_tokens(self, texts: list[str]) -> int:
        return len(texts)

    def get_dimension(self, model: str) -> int:
        return 3


@pytest.mark.asyncio
async def test_semantic_search_query_embed_redacts_secret_at_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: co-vet fix — a secret-pattern QUERY is redacted BEFORE it
    reaches the embedding provider (the egress boundary to the external
    embedding API), and the `embed_secret_redacted` audit-event fires on the
    query path. This is because the query embed now dispatches through the
    shared `embed` op (`execute_op(EmbedIROp(...))`), inheriting the Phase 1
    PRE-embed `redact_secrets` egress seam — symmetric with `index_update`'s
    ingestion embed (architect ruling (a): one redaction-gated embed
    mechanism, no provider-direct bypass).

    FALSIFY: revert the query embed in `semantic_search.handle` to
    provider-direct (`provider.embed([op.query], model)`) → the raw secret
    reaches the provider AND no `embed_secret_redacted` event fires → both
    asserts below go RED. (Verified locally: strip → RED, restore → GREEN.)
    """
    fake = _RecordingEmbeddingProvider()
    # The embed op resolves its provider via op_runtime.embed.get_provider —
    # patch THAT (the query embed now routes through the embed op, not a
    # provider-direct call in semantic_search).
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)
    monkeypatch.chdir(tmp_path)

    await _seed(tmp_path, "docs", [_chunk("some chunk", [1.0, 0.0, 0.0], "c1")])

    ctx = _make_ctx(tmp_path)
    # A secret-shaped query (matches the FP-0050 redact_secrets patterns —
    # same shape as tests/test_op_embed.py's PRE-embed seam test).
    secret_query = 'api_key = "abcdefghijklmnopqrstuvwxyz123456"'
    op = SemanticSearchIROp(
        kind="semantic_search", query=secret_query, sources=["docs"], top_k=5,
    )
    result = await execute_op(op, ctx)

    assert result.get("status") != "error", result
    # The provider (= egress boundary to the external embedding API) never
    # receives the raw secret value; it sees the redacted form.
    assert fake.received_texts, "provider must have been called for the query embed"
    assert "abcdefghijklmnopqrstuvwxyz123456" not in fake.received_texts[0]
    assert "REDACTED" in fake.received_texts[0]
    # The seam firing on the QUERY path is observable (P6 audit-event) —
    # mirrors index_update's ingestion redaction.
    assert any(e.type == "embed_secret_redacted" for e in ctx.events.all())
