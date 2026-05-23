"""Tier 2: index_drop op handler OS invariants (ADR-0033 Phase 1).

Tests permission gate, backend drop, manifest removal, and P6 event emission.
No mocks — uses real SqliteIndexBackend, SourceManifest, PermissionResolver,
and EventLog.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.events.events import EventLog
from reyn.index.backend import ChunkRecord
from reyn.index.backends.sqlite import SqliteIndexBackend
from reyn.index.source_manifest import SourceEntry, get_source_manifest
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import IndexDropIROp
from reyn.workspace.workspace import Workspace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _phase5_index_drop_decl(resolver: PermissionResolver, tmp_path: Path) -> PermissionDecl:
    """Phase 5 successor to ``PermissionDecl(index_drop=True)``.

    Builds the explicit ``file.write`` decl for the canonical manifest
    path and session-approves it so ``require_file_write`` passes.
    """
    canonical = str(tmp_path / ".reyn" / "index" / "sources.yaml")
    resolver.session_approve_path(canonical, "test_op_index_drop", "file.write")
    return PermissionDecl(file_write=[{"path": canonical, "scope": "just_path"}])


def _make_ctx(
    tmp_path: Path,
    *,
    permission_resolver: PermissionResolver | None = None,
    permission_decl: PermissionDecl | None = None,
) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=permission_decl or PermissionDecl(),
        permission_resolver=permission_resolver,
        skill_name="test_op_index_drop",
    )


def _resolver(
    tmp_path: Path,
    *,
    config: dict | None = None,
    interactive: bool = False,
) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config or {},
        project_root=tmp_path,
        interactive=interactive,
    )


def _chunk(text: str, vec: list[float], ch: str) -> ChunkRecord:
    return ChunkRecord(
        text=text,
        vector=vec,
        metadata={
            "source_path": "f.txt",
            "source_type": "generic",
            "content_hash": ch,
            "embedding_model": "m",
            "chunk_index": 0,
            "size_tokens": 2,
            "parent_context": None,
        },
        score=None,
    )


async def _seed(workspace_root: Path, source: str) -> None:
    backend = SqliteIndexBackend(workspace_root=workspace_root)
    await backend.write(source, [_chunk("content", [1.0, 0.0], "h1")], mode="append")

    manifest = get_source_manifest(workspace_root)
    entry = SourceEntry(
        name=source,
        description=f"Test source {source}",
        path="f.txt",
        backend="sqlite",
        chunk_count=1,
    )
    await manifest.upsert(entry)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drop_removes_backend_and_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: index_drop removes backend index and manifest entry."""
    import os
    monkeypatch.chdir(tmp_path)
    import os as _os
    _os.environ["REYN_INDEX_DROP_AUTO_APPROVE"] = "1"

    try:
        await _seed(tmp_path, "my_source")

        resolver = _resolver(tmp_path, config={"index_drop": "allow"})
        ctx = _make_ctx(
            tmp_path,
            permission_resolver=resolver,
            permission_decl=_phase5_index_drop_decl(resolver, tmp_path),
        )

        op = IndexDropIROp(kind="index_drop", source="my_source")
        result = await execute_op(op, ctx, caller="control_ir")

        assert result.get("status") != "error", result
        assert result["removed"] is True

        # Manifest entry gone
        manifest = get_source_manifest(tmp_path)
        entry = await manifest.get("my_source")
        assert entry is None
    finally:
        _os.environ.pop("REYN_INDEX_DROP_AUTO_APPROVE", None)


@pytest.mark.asyncio
async def test_drop_emits_p6_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: index_drop emits 'index_dropped' event for P6 audit trail."""
    import os
    monkeypatch.chdir(tmp_path)
    os.environ["REYN_INDEX_DROP_AUTO_APPROVE"] = "1"

    try:
        await _seed(tmp_path, "audit_src")

        events = EventLog()
        ws = Workspace(events=events)
        resolver = _resolver(tmp_path, config={"index_drop": "allow"})
        ctx = OpContext(
            workspace=ws,
            events=events,
            permission_decl=_phase5_index_drop_decl(resolver, tmp_path),
            permission_resolver=resolver,
            skill_name="test_op_index_drop",
        )

        op = IndexDropIROp(kind="index_drop", source="audit_src")
        await execute_op(op, ctx, caller="control_ir")

        # Verify event was emitted (EventLog.all() returns Event objects with .type)
        event_types = [e.type for e in events.all()]
        assert "index_dropped" in event_types
    finally:
        os.environ.pop("REYN_INDEX_DROP_AUTO_APPROVE", None)


@pytest.mark.asyncio
async def test_drop_nonexistent_source_returns_not_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: dropping a non-existent source returns removed=False without error."""
    import os
    monkeypatch.chdir(tmp_path)
    os.environ["REYN_INDEX_DROP_AUTO_APPROVE"] = "1"

    try:
        resolver = _resolver(tmp_path, config={"index_drop": "allow"})
        ctx = _make_ctx(
            tmp_path,
            permission_resolver=resolver,
            permission_decl=_phase5_index_drop_decl(resolver, tmp_path),
        )

        op = IndexDropIROp(kind="index_drop", source="nonexistent")
        result = await execute_op(op, ctx, caller="control_ir")

        assert result.get("status") != "error", result
        assert result["removed"] is False
        assert result["chunks_dropped"] == 0
    finally:
        os.environ.pop("REYN_INDEX_DROP_AUTO_APPROVE", None)


@pytest.mark.asyncio
async def test_drop_denied_when_permission_not_declared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: index_drop is denied when file.write for sources.yaml is not declared.

    #571 collapse arc Phase 5: the bool-axis ``index_drop`` is gone;
    authorisation flows through ``require_file_write`` on
    ``.reyn/index/sources.yaml``. An empty PermissionDecl fails the gate.
    """
    import os
    monkeypatch.chdir(tmp_path)

    await _seed(tmp_path, "guarded_src")

    resolver = _resolver(tmp_path)
    # PermissionDecl without the canonical sources.yaml file.write entry.
    ctx = _make_ctx(
        tmp_path,
        permission_resolver=resolver,
        permission_decl=PermissionDecl(),
    )

    op = IndexDropIROp(kind="index_drop", source="guarded_src")
    result = await execute_op(op, ctx, caller="control_ir")

    assert result["status"] == "denied"
