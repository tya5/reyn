"""Tier 2: embed op handler OS invariants (ADR-0033 Phase 1).

All tests use a FakeEmbeddingProvider — no litellm API calls. The handler
is tested through the public execute_op path so registration is also
exercised.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reyn.embedding.provider import EmbedBatchResult
from reyn.events.events import EventLog
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import EmbedIROp
from reyn.workspace.workspace import Workspace

# ---------------------------------------------------------------------------
# Fake provider — injected into the handler via monkeypatching get_provider
# ---------------------------------------------------------------------------

class FakeEmbeddingProvider:
    """Deterministic provider: returns unit vector with index-0 = len(text)/100."""

    def __init__(self, dim: int = 4) -> None:
        self._dim = dim
        self._batch_size = 10  # exposes batch_size for handler

    async def embed(self, texts: list[str], model: str) -> EmbedBatchResult:
        vectors = [
            [len(t) / 100.0] + [0.0] * (self._dim - 1)
            for t in texts
        ]
        return EmbedBatchResult(
            vectors=vectors,
            model=model,
            total_tokens=sum(len(t) for t in texts),
        )

    def estimate_tokens(self, texts: list[str]) -> int:
        return sum(len(t) for t in texts)

    def get_dimension(self, model: str) -> int:
        return self._dim


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


def _make_op_inline(texts: list[str], model: str = "standard") -> EmbedIROp:
    return EmbedIROp(kind="embed", texts=texts, model=model)


def _write_jsonl(path: Path, chunks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk) + "\n")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_form_a_inline_returns_vectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: Form A inline embed returns vectors for each input text."""
    fake = FakeEmbeddingProvider(dim=4)
    import reyn.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path)
    op = _make_op_inline(["hello world", "foo"])

    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert len(result["vectors"]) == 2
    # Deterministic: len("hello world") = 11 → 11/100 = 0.11
    assert abs(result["vectors"][0][0] - 0.11) < 1e-6
    assert result["total_tokens"] == len("hello world") + len("foo")


@pytest.mark.asyncio
async def test_form_a_empty_texts_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: Form A with empty texts list short-circuits without calling provider."""
    fake = FakeEmbeddingProvider()
    import reyn.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path)
    op = _make_op_inline([])

    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert result["vectors"] == []
    assert result["total_tokens"] == 0


@pytest.mark.asyncio
async def test_form_b_artifact_embeds_and_writes_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: Form B artifact reference embeds chunks and writes output JSONL."""
    fake = FakeEmbeddingProvider(dim=4)
    import reyn.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    monkeypatch.chdir(tmp_path)

    # Create input artifact
    input_path = tmp_path / "artifacts" / "chunks.jsonl"
    chunks = [
        {
            "text": "chunk one",
            "metadata": {
                "content_hash": "h1",
                "source_path": "f.txt",
                "source_type": "generic",
                "embedding_model": "",
                "chunk_index": 0,
                "size_tokens": 2,
            },
        },
        {
            "text": "chunk two",
            "metadata": {
                "content_hash": "h2",
                "source_path": "f.txt",
                "source_type": "generic",
                "embedding_model": "",
                "chunk_index": 1,
                "size_tokens": 2,
            },
        },
    ]
    _write_jsonl(input_path, chunks)

    output_rel = "artifacts/chunks_with_vectors.jsonl"
    op = EmbedIROp(
        kind="embed",
        input_artifact="artifacts/chunks.jsonl",
        output_artifact=output_rel,
        model="standard",
    )

    ctx = _make_ctx(tmp_path)
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert result["embedded_count"] == 2
    assert result["skipped_count"] == 0

    output_path = tmp_path / output_rel
    assert output_path.exists()
    lines = [json.loads(l) for l in output_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    assert "vector" in lines[0]
    assert len(lines[0]["vector"]) == 4


@pytest.mark.asyncio
async def test_form_b_idempotent_reruns_skip_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: Form B re-run skips chunks whose content_hash already in output."""
    fake = FakeEmbeddingProvider(dim=4)
    import reyn.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    monkeypatch.chdir(tmp_path)

    input_path = tmp_path / "artifacts" / "chunks.jsonl"
    chunks = [
        {
            "text": "already embedded",
            "metadata": {"content_hash": "existing-hash", "source_path": "f.txt",
                         "source_type": "g", "embedding_model": "m", "chunk_index": 0, "size_tokens": 2},
        },
        {
            "text": "new chunk",
            "metadata": {"content_hash": "new-hash", "source_path": "f.txt",
                         "source_type": "g", "embedding_model": "m", "chunk_index": 1, "size_tokens": 2},
        },
    ]
    _write_jsonl(input_path, chunks)

    # Pre-populate output with the first chunk
    output_rel = "artifacts/out.jsonl"
    output_path = tmp_path / output_rel
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_line = {
        "text": "already embedded",
        "metadata": {"content_hash": "existing-hash", "embedding_model": "m"},
        "vector": [0.1, 0.0, 0.0, 0.0],
    }
    output_path.write_text(json.dumps(existing_line) + "\n", encoding="utf-8")

    op = EmbedIROp(
        kind="embed",
        input_artifact="artifacts/chunks.jsonl",
        output_artifact=output_rel,
        model="standard",
    )
    ctx = _make_ctx(tmp_path)
    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", result
    assert result["embedded_count"] == 1   # only "new chunk"
    assert result["skipped_count"] == 1    # "already embedded" skipped


@pytest.mark.asyncio
async def test_form_b_missing_input_artifact_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: Form B with non-existent input_artifact returns error status."""
    fake = FakeEmbeddingProvider()
    import reyn.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path)
    op = EmbedIROp(
        kind="embed",
        input_artifact="artifacts/nonexistent.jsonl",
        output_artifact="artifacts/out.jsonl",
        model="standard",
    )

    result = await execute_op(op, ctx, caller="control_ir")
    assert result["status"] == "error"
    assert "not found" in result["error"].lower() or "nonexistent" in result["error"]


@pytest.mark.asyncio
async def test_mutual_exclusion_both_set_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: setting both texts and input_artifact on EmbedIROp returns error."""
    fake = FakeEmbeddingProvider()
    import reyn.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: fake)

    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path)
    op = EmbedIROp(
        kind="embed",
        texts=["hello"],
        input_artifact="artifacts/chunks.jsonl",
        output_artifact="artifacts/out.jsonl",
        model="standard",
    )

    result = await execute_op(op, ctx, caller="control_ir")
    assert result["status"] == "error"
