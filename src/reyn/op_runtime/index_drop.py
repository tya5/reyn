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
from .context import OpContext, sandbox_policy_from_ctx


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

    # Permission gate (#571 collapse arc Phase 5): the skill must
    # declare ``file.write: [.reyn/index/sources.yaml]``. The
    # bool-axis ``require_index_drop`` per-source prompt is removed;
    # per-source granularity is not preserved (= drop is destructive
    # and the per-source distinction was operator-UX rather than
    # security).
    if ctx.permission_resolver is not None:
        sandbox_policy = sandbox_policy_from_ctx(ctx)
        sources_yaml = workspace_root / ".reyn" / "index" / "sources.yaml"
        ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(sources_yaml), ctx.skill_name,
            sandbox_policy=sandbox_policy,
        )
        # #1199 S3.4 Part1: gate the actual deletion target — the source dir —
        # not just the manifest, so the destructive drop respects the phase
        # sandbox write_paths cap (S3.1c-2 ∩). The SQLite/dir removal stays
        # host-direct; the gate fires before backend.drop opens/removes it.
        source_dir = workspace_root / ".reyn" / "index" / op.source
        ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(source_dir), ctx.skill_name,
            sandbox_policy=sandbox_policy,
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
