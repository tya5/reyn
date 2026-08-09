"""Tier 2: S3.4 Part1 — index/recall/embed FS-ops routed through the permission gate.

#1199 S3.4 Part1 closes the hole where index reads/writes opened sqlite3 host-direct
(bypassing require_file_*), so S3.1c-2's SandboxLayer ∩ never applied. Two seams:
  - OS-side op handlers (index_query / index_drop) call require_file_read/write
    with the phase sandbox_policy ∩ BEFORE invoking the backend.
  - the write path forwards the sandbox write_paths cap onto the
    `OpContext.default_sandbox_policy` the caller builds (an `OpContext` with
    `permission_resolver=None`, mirroring a subprocess/safe-mode-shaped
    caller), and the `index_update` op forwards it into the
    `SqliteIndexBackend`/`SourceManifest` construction, which self-gate at
    their OWN real write sites (#2856 Part B — this superseded the #2851/F3
    wrapper pre-flight, which duplicated the same path-check by hand).

#3901 PR-B ③ retired FILE_READ from SandboxLayer's permission-∩ projection
(an operator cannot know a sandbox's path floor, so it is no longer treated
as permission), so index_query's require_file_read call still fires but no
longer narrows on a sandbox read cap specifically — see
test_index_query_sandbox_policy_does_not_narrow_the_read below. The WRITE
side (index_drop / index_update / SourceManifest) is unaffected: those self-
gate `sandbox_write_paths` directly at their own real write sites (below),
independent of SandboxLayer.

FP-0066 P1c note: the safe-mode `reyn.api.safe.index_update` wrapper this
file used to also exercise directly (a thin `_set_context`/`index_update_
async` dispatch onto the SAME op below) was retired as a user-facing
surface — the in-core index is OS-internal only now (user RAG = FP-0063
plugin). The wrapper-specific assertion (`test_safe_index_update_forwards_
cap_to_backend`) is removed with it; the underlying fs-op-gate invariant it
exercised — cap-forwarding through `OpContext.default_sandbox_policy` into
the `index_update` op's backend construction, with no `permission_resolver`
— is NOT lost: `test_index_update_backend_self_gate_fires_with_no_resolver`
below drives the identical `index_update` op handler directly with the same
resolver=None / cap shape (its own docstring already called this out as
"Symmetric with the safe-mode-path assertion" before the wrapper was
retired). No other surviving `reyn.api.safe.*` module dispatches through
`execute_op`/`OpContext` at all, so there is no alternative safe-mode
vehicle to re-base onto — this op-level test is the surviving coverage.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.data.index.backend import ChunkRecord
from reyn.data.index.backends.sqlite import SqliteIndexBackend
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import IndexDropIROp, IndexQueryIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


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


# ── OS-side op-handler gates (read / drop) ───────────────────────────────────


def _ctx(tmp_path: Path, *, sandbox_policy: dict | None) -> OpContext:
    events = EventLog()
    ws = Workspace(events, base_dir=tmp_path)
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=resolver, actor="s",
        default_sandbox_policy=sandbox_policy,
    )


def test_index_query_sandbox_policy_does_not_narrow_the_read(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: index_query routes through require_file_read with the phase
    sandbox_policy ∩ — but #3901 PR-B ③ retired FILE_READ from
    SandboxLayer's permission-∩ projection (an operator cannot know a
    sandbox's path floor, so it is no longer treated as permission — see
    effective.py's SandboxLayer docstring), so a sandbox_policy passed
    alongside a query no longer narrows it on this axis. Before PR-B this
    test pinned the OPPOSITE claim (a sandbox read-restriction denied the
    query); that intersection is gone by design, not by regression."""
    from reyn.core.op_runtime.index_query import handle

    monkeypatch.chdir(tmp_path)  # so .reyn/index/... is in the default read zone
    ctx = _ctx(tmp_path, sandbox_policy={"network": False})
    op = IndexQueryIROp(kind="index_query", source="src", query_vector=[0.1], top_k=1)
    result = asyncio.run(handle(op, ctx))
    assert result["mode"] == "fallback"  # no index yet, but NOT denied by the sandbox_policy


def test_index_query_no_policy_passes(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: with no sandbox policy, the in-zone index read is not blocked
    (returns fallback for an absent index)."""
    from reyn.core.op_runtime.index_query import handle

    monkeypatch.chdir(tmp_path)
    ctx = _ctx(tmp_path, sandbox_policy=None)
    op = IndexQueryIROp(kind="index_query", source="src", query_vector=[0.1], top_k=1)
    result = asyncio.run(handle(op, ctx))
    assert result["mode"] == "fallback"  # no index yet, but NOT denied


def test_index_drop_gates_source_dir_by_sandbox_cap(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: index_drop gates the SOURCE DIR (the deletion target) under the
    sandbox write_paths cap — a cap excluding it DENIES the destructive drop."""
    from reyn.core.op_runtime.index_drop import handle

    monkeypatch.chdir(tmp_path)
    ctx = _ctx(tmp_path, sandbox_policy={"write_paths": ["/sandboxed"]})
    op = IndexDropIROp(kind="index_drop", source="src")
    with pytest.raises(PermissionError):
        asyncio.run(handle(op, ctx))


# ── #2856 Part B: cap-forwarding ALL 4 index ops → backend/manifest
#    real-write-site self-gate (not just the require_file_write gate above,
#    which is skipped entirely when permission_resolver is None) ────────────


def _no_resolver_ctx(tmp_path: Path, *, sandbox_policy: dict | None) -> OpContext:
    """An OpContext with NO permission_resolver (mirrors the safe-mode
    wrapper's ctx shape) — isolates the backend/manifest self-gate from the
    require_file_write gate, which only fires when resolver is not None."""
    events = EventLog()
    ws = Workspace(events, base_dir=tmp_path)
    return OpContext(
        workspace=ws, events=events, permission_decl=PermissionDecl(),
        permission_resolver=None, actor="s",
        default_sandbox_policy=sandbox_policy,
    )


def test_index_drop_backend_self_gate_fires_with_no_resolver(tmp_path: Path) -> None:
    """Tier 2: #2856 Part B falsify — with NO permission_resolver (the
    require_file_write gate above is skipped entirely), a write_paths cap
    excluding the source dir still DENIES `index_drop`, because the op now
    forwards the cap into `SqliteIndexBackend(sandbox_write_paths=...)`,
    which self-gates `drop()` at the real deletion site."""
    from reyn.core.op_runtime.index_drop import handle

    asyncio.run(_write(SqliteIndexBackend(workspace_root=tmp_path)))
    source_dir = tmp_path / ".reyn" / "cache" / "index" / "s"
    ctx = _no_resolver_ctx(tmp_path, sandbox_policy={"write_paths": ["/sandboxed"]})
    op = IndexDropIROp(kind="index_drop", source="s")
    with pytest.raises(PermissionError):
        asyncio.run(handle(op, ctx))
    assert source_dir.exists()  # denied BEFORE the rmtree side effect


def test_index_drop_backend_self_gate_strip_falsify(tmp_path: Path) -> None:
    """Tier 2: ★∩-falsification — the SAME drop the cap denies succeeds once
    the cap is dropped (None), proving the self-gate (not something else) is
    load-bearing."""
    from reyn.core.op_runtime.index_drop import handle

    asyncio.run(_write(SqliteIndexBackend(workspace_root=tmp_path)))
    source_dir = tmp_path / ".reyn" / "cache" / "index" / "s"
    assert source_dir.exists()
    ctx = _no_resolver_ctx(tmp_path, sandbox_policy=None)
    op = IndexDropIROp(kind="index_drop", source="s")
    result = asyncio.run(handle(op, ctx))
    assert result["removed"] is True
    assert not source_dir.exists()


def test_index_update_backend_self_gate_fires_with_no_resolver(tmp_path: Path) -> None:
    """Tier 2: #2856 Part B falsify — with NO permission_resolver, a
    write_paths cap excluding the source's index dir still DENIES
    `index_update`'s write, because the op forwards the cap into
    `SqliteIndexBackend(sandbox_write_paths=...)`, which self-gates `write()`
    at the real write site. Symmetric with the safe-mode-path assertion in
    test_2b_safe_index_update.py (same op, same cap, same real site)."""
    from reyn.core.op_runtime.index_update import handle
    from reyn.data.embedding import register_provider
    from reyn.schemas.models import IndexUpdateIROp

    register_provider("fake_2856", _FakeProvider)
    ctx = _no_resolver_ctx(tmp_path, sandbox_policy={"write_paths": ["/sandboxed"]})
    op = IndexUpdateIROp(
        kind="index_update",
        source="src",
        chunks=[{"text": "hello", "metadata": {"content_hash": "h1", "source_path": "s.md"}}],
        embedding_model="fake_2856",
    )
    import os
    os.environ["REYN_EMBEDDING_PROVIDER"] = "fake_2856"
    try:
        with pytest.raises(PermissionError):
            asyncio.run(handle(op, ctx))
    finally:
        os.environ.pop("REYN_EMBEDDING_PROVIDER", None)
    assert not (tmp_path / ".reyn" / "cache" / "index" / "src" / "index.db").exists()


# ── SourceManifest's OWN real-write-site self-gate (F3 root — the manifest
#    write is NOT covered by the backend's db_file self-gate) ────────────────


def test_source_manifest_upsert_self_gate_denies_outside_config_dir(tmp_path: Path) -> None:
    """Tier 2: #2856 Part B — `SourceManifest.upsert`'s own real write site
    (`_atomic_write`) denies when the forwarded cap excludes
    `.reyn/config/index/sources.yaml`, closing F3 (the manifest write is a
    SEPARATE write from the backend's `db_file`, so the backend self-gate
    alone does not cover it)."""
    from reyn.data.index.source_manifest import SourceEntry, get_source_manifest

    manifest = get_source_manifest(tmp_path)
    entry = SourceEntry(name="src", description="d", path="p")
    with pytest.raises(PermissionError):
        asyncio.run(
            manifest.upsert(
                entry, sandbox_write_paths=[str(tmp_path / ".reyn" / "cache")],
            )
        )
    assert not (tmp_path / ".reyn" / "config" / "index" / "sources.yaml").exists()


def test_source_manifest_upsert_self_gate_strip_falsify(tmp_path: Path) -> None:
    """Tier 2: ★∩-falsification — the SAME upsert the cap denies succeeds
    once the cap is dropped (None)."""
    from reyn.data.index.source_manifest import SourceEntry, get_source_manifest

    manifest = get_source_manifest(tmp_path)
    entry = SourceEntry(name="src", description="d", path="p")
    asyncio.run(manifest.upsert(entry, sandbox_write_paths=None))
    assert (tmp_path / ".reyn" / "config" / "index" / "sources.yaml").exists()
