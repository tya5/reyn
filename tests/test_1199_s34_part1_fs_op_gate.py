"""Tier 2: S3.4 Part1 — index/recall/embed FS-ops routed through the permission gate.

#1199 S3.4 Part1 closes the hole where index reads/writes opened sqlite3 host-direct
(bypassing require_file_*), so S3.1c-2's SandboxLayer ∩ never applied. Two seams:
  - OS-side op handlers (index_query / index_drop) call require_file_read/write
    with the phase sandbox_policy ∩ BEFORE invoking the backend.
  - the WRITE path runs in the safe subprocess (no ctx): the sandbox write_paths
    cap is forwarded into the subprocess (harness → embed_index) and
    SqliteIndexBackend self-gates the DB path before sqlite3.connect (co-signed B).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.events.events import EventLog
from reyn.index.backend import ChunkRecord
from reyn.index.backends.sqlite import SqliteIndexBackend
from reyn.op_runtime.context import OpContext
from reyn.schemas.models import IndexDropIROp, IndexQueryIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.workspace.workspace import Workspace


def _chunk() -> ChunkRecord:
    return ChunkRecord(
        text="x", vector=[0.1, 0.2, 0.0, 0.0],
        metadata={
            "content_hash": "h1", "source_path": "f", "source_type": "generic",
            "embedding_model": "m", "chunk_index": 0, "size_tokens": 1,
            "parent_context": None,
        },
        score=None,
    )


async def _write(backend: SqliteIndexBackend):
    return await backend.write("s", [_chunk()], "append")


# ── SqliteIndexBackend write self-gate (subprocess write path) ───────────────


def test_write_denies_outside_sandbox_cap(tmp_path: Path) -> None:
    """Tier 2: the host-direct index write DENIES when the DB path is outside the
    forwarded sandbox write_paths cap (gate fires before sqlite3.connect)."""
    b = SqliteIndexBackend(workspace_root=tmp_path, sandbox_write_paths=["/sandboxed"])
    with pytest.raises(PermissionError, match="sandbox"):
        asyncio.run(_write(b))
    # and nothing was written (denial precedes the open).
    assert not (tmp_path / ".reyn" / "index" / "s" / "index.db").exists()


def test_write_allows_within_sandbox_cap(tmp_path: Path) -> None:
    """Tier 2: a cap that covers the workspace allows the write (swe_bench ["/"]
    allow-all is the live no-blast case)."""
    b = SqliteIndexBackend(workspace_root=tmp_path, sandbox_write_paths=[str(tmp_path)])
    assert asyncio.run(_write(b))["written"] == 1


def test_write_no_cap_unchanged(tmp_path: Path) -> None:
    """Tier 2: sandbox_write_paths=None (in-process callers / no phase policy) →
    no sandbox gate, write behaves as pre-S3.4."""
    assert asyncio.run(_write(SqliteIndexBackend(workspace_root=tmp_path)))["written"] == 1


def test_write_sandbox_falsification(tmp_path: Path) -> None:
    """Tier 2: ★∩-falsification — the SAME write the cap DENIES is ALLOWED once the
    cap is dropped (None). Proves the self-gate is load-bearing (over-grant if removed)."""
    with pytest.raises(PermissionError):
        asyncio.run(_write(SqliteIndexBackend(
            workspace_root=tmp_path, sandbox_write_paths=["/sandboxed"],
        )))
    assert asyncio.run(_write(SqliteIndexBackend(
        workspace_root=tmp_path, sandbox_write_paths=None,
    )))["written"] == 1


# ── embed_index forwards the cap to the backend (subprocess context) ─────────


class _FakeProvider:
    def __init__(self, config=None) -> None:
        self._batch_size = 100

    async def embed(self, texts, model):
        return {"vectors": [[1.0, 0.0, 0.0, 0.0] for _ in texts],
                "model": model or "fake", "total_tokens": len(texts)}

    def estimate_tokens(self, texts):
        return len(texts)

    def get_dimension(self, model):
        return 4


def test_embed_index_forwards_cap_to_backend(tmp_path: Path) -> None:
    """Tier 2: the harness-set sandbox_write_paths context flows from embed_index
    into SqliteIndexBackend → a restrictive cap DENIES the streamed index write."""
    from reyn.embedding import register_provider
    from reyn.safe import embed_index as ei

    register_provider("fake_s34", _FakeProvider)
    ei._reset_context()
    ei._set_context(
        workspace_root=tmp_path, provider_name="fake_s34",
        sandbox_write_paths=["/sandboxed"],  # excludes tmp_path
    )
    chunk = {"text": "hello", "metadata": {"content_hash": "h1"}}
    try:
        with pytest.raises(PermissionError, match="sandbox"):
            asyncio.run(ei.embed_and_index_async([chunk], "src", "standard"))
    finally:
        ei._reset_context()


# ── OS-side op-handler gates (read / drop) ───────────────────────────────────


def _ctx(tmp_path: Path, *, sandbox_policy: dict | None) -> OpContext:
    events = EventLog()
    ws = Workspace(events, base_dir=tmp_path)
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, skill_name="s",
        default_sandbox_policy=sandbox_policy,
    )


def test_index_query_gated_by_sandbox_read_cap(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: index_query routes through require_file_read with the phase
    sandbox_policy ∩ — a read_paths cap excluding the index path DENIES the query
    (the in-zone DB path is narrowed by the sandbox)."""
    from reyn.op_runtime.index_query import handle

    monkeypatch.chdir(tmp_path)  # so .reyn/index/... is in the default read zone
    ctx = _ctx(tmp_path, sandbox_policy={"read_paths": ["/sandboxed"]})
    op = IndexQueryIROp(kind="index_query", source="src", query_vector=[0.1], top_k=1)
    with pytest.raises(PermissionError):
        asyncio.run(handle(op, ctx, caller="control_ir"))


def test_index_query_no_policy_passes(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: with no sandbox policy, the in-zone index read is not blocked
    (returns fallback for an absent index)."""
    from reyn.op_runtime.index_query import handle

    monkeypatch.chdir(tmp_path)
    ctx = _ctx(tmp_path, sandbox_policy=None)
    op = IndexQueryIROp(kind="index_query", source="src", query_vector=[0.1], top_k=1)
    result = asyncio.run(handle(op, ctx, caller="control_ir"))
    assert result["mode"] == "fallback"  # no index yet, but NOT denied


def test_index_drop_gates_source_dir_by_sandbox_cap(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: index_drop gates the SOURCE DIR (the deletion target) under the
    sandbox write_paths cap — a cap excluding it DENIES the destructive drop."""
    from reyn.op_runtime.index_drop import handle

    monkeypatch.chdir(tmp_path)
    ctx = _ctx(tmp_path, sandbox_policy={"write_paths": ["/sandboxed"]})
    op = IndexDropIROp(kind="index_drop", source="src")
    with pytest.raises(PermissionError):
        asyncio.run(handle(op, ctx, caller="control_ir"))
