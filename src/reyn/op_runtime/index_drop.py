"""index_drop op handler — remove an indexed source entirely (ADR-0033 Phase 1).

Destructive op: drops the SQLite backend + removes the SourceManifest entry.

Permission gate: mirrors require_mcp_install pattern (ADR-0029).
  - Skill must declare `permissions.index_drop: true`.
  - Config may hard-allow or hard-deny (permissions.index_drop: allow/deny).
  - Interactive prompt or REYN_INDEX_DROP_AUTO_APPROVE=1 CI escape hatch.

P6 event: emits `index_dropped` after completion (audit trail).
"""
from __future__ import annotations

from typing import Literal

from reyn.index import SqliteIndexBackend
from reyn.index.source_manifest import get_source_manifest
from reyn.schemas.models import IndexDropIROp

from . import register
from .context import OpContext


async def handle(
    op: IndexDropIROp,
    ctx: OpContext,
    caller: Literal["preprocessor", "control_ir"],
) -> dict:
    """Execute an index_drop op (ADR-0033 §2.1).

    Returns:
      {removed: bool, chunks_dropped: int}
    """
    # B48-NF-W2-S7 fix (2026-05-22): same defensive guard as index_query.
    # See index_query.py for full rationale.
    if ctx.workspace is None:
        raise ValueError(
            "index_drop: op_runtime context has no workspace. Index ops "
            "require a workspace to locate the SQLite backend; pass an "
            "OpContext with a populated `workspace` field. This typically "
            "indicates the calling tool (e.g. drop_source) was invoked from "
            "a router-side path without a workspace."
        )

    workspace_root = ctx.workspace.base_dir

    # Permission gate (ADR-0029 mirror — ask default)
    if ctx.permission_resolver is not None:
        bus = ctx.intervention_bus if ctx.intervention_bus is not None else _DenyBus()
        await ctx.permission_resolver.require_index_drop(
            ctx.permission_decl, op.source, bus,  # type: ignore[arg-type]
        )

    backend = SqliteIndexBackend(workspace_root=workspace_root)
    manifest = get_source_manifest(workspace_root)

    drop_result = await backend.drop(op.source)
    removed_from_manifest = await manifest.remove(op.source)

    # Emit P6 event (audit trail)
    ctx.events.emit(
        "index_dropped",
        source=op.source,
        chunks_dropped=drop_result["chunks_dropped"],
        manifest_removed=removed_from_manifest,
    )

    return {
        "removed": drop_result["removed"] or removed_from_manifest,
        "chunks_dropped": drop_result["chunks_dropped"],
    }


class _DenyBus:
    """Minimal InterventionBus stub that denies all prompts.

    Used when the resolver needs a bus but none is supplied (non-interactive).
    The resolver's _interactive flag will auto-deny before calling request(),
    so this stub's request() is a safety net only.
    """

    async def request(self, iv: object) -> object:
        from reyn.user_intervention import InterventionAnswer
        return InterventionAnswer(choice_id="no", text="")


register("index_drop", handle)
