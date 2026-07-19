"""index_drop op handler — remove an indexed source entirely (ADR-0033 Phase 1).

Destructive op: drops the SQLite backend + removes the SourceManifest entry.

Permission gate: mirrors require_mcp_install pattern (ADR-0029).
  - Caller must declare `permissions.index_drop: true`.
  - Config may hard-allow or hard-deny (permissions.index_drop: allow/deny).
  - Interactive prompt or REYN_INDEX_DROP_AUTO_APPROVE=1 CI escape hatch.

P6 event: emits `index_dropped` after completion (audit trail).
"""
from __future__ import annotations

from typing import Literal

from reyn.data.index import SqliteIndexBackend
from reyn.data.index.source_manifest import get_source_manifest
from reyn.schemas.models import IndexDropIROp

from . import register
from .context import OpContext, sandbox_policy_from_ctx


async def handle(
    op: IndexDropIROp,
    ctx: OpContext,
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

    # Permission gate (#571 collapse arc Phase 5): the caller must
    # declare ``file.write: [.reyn/config/index/sources.yaml]``. The
    # bool-axis ``require_index_drop`` per-source prompt is removed;
    # per-source granularity is not preserved (= drop is destructive
    # and the per-source distinction was operator-UX rather than
    # security).
    # #2856 Part B: resolve unconditionally — forwarded into the backend
    # below regardless of whether a permission_resolver is present, so the
    # destructive drop self-gates at the real write site on every
    # caller/surface (mirrors index_update).
    sandbox_policy = sandbox_policy_from_ctx(ctx)
    sandbox_write_paths = sandbox_policy.write_paths if sandbox_policy is not None else None

    if ctx.permission_resolver is not None:
        sources_yaml = workspace_root / ".reyn" / "config" / "index" / "sources.yaml"
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(sources_yaml), ctx.actor,
            sandbox_policy=sandbox_policy, bus=ctx.intervention_bus,
        )
        # #1199 S3.4 Part1: gate the actual deletion target — the source dir —
        # not just the manifest, so the destructive drop respects the phase
        # sandbox write_paths cap (S3.1c-2 ∩). The SQLite/dir removal stays
        # host-direct; the gate fires before backend.drop opens/removes it.
        source_dir = workspace_root / ".reyn" / "cache" / "index" / op.source
        await ctx.permission_resolver.require_file_write(
            ctx.permission_decl, str(source_dir), ctx.actor,
            sandbox_policy=sandbox_policy, bus=ctx.intervention_bus,
        )

    # #2856 Part B: forward the cap into the backend — `drop` now self-gates
    # the source dir at the real deletion site (sqlite.py), not just via the
    # require_file_write above (which is skipped entirely when
    # permission_resolver is None, e.g. a future safe-mode drop entry point).
    backend = SqliteIndexBackend(
        workspace_root=workspace_root, sandbox_write_paths=sandbox_write_paths,
    )
    manifest = get_source_manifest(workspace_root)

    drop_result = await backend.drop(op.source)
    removed_from_manifest = await manifest.remove(
        op.source, sandbox_write_paths=sandbox_write_paths,
    )

    # #2259 PR-1: record the FULL post-drop sources registry as a truncation-surviving config
    # generation so it recovers (the yaml is a derived projection). The helper guards
    # internally — no-op when there is no WAL or the path is outside the project `.reyn`.
    from reyn.core.events.config_recovery import record_config_generation  # noqa: PLC0415
    await record_config_generation(
        getattr(ctx, "state_log", None), manifest.path, await manifest.snapshot(),
    )

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


from reyn.core.offload.canonical import index_drop_to_canonical  # noqa: E402

register("index_drop", handle, canonical=index_drop_to_canonical)
