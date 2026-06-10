"""Single-source Tool→OpContext bridge for op_runtime delegation.

Tool handlers that delegate to an ``op_runtime`` handler need an
:class:`~reyn.op_runtime.context.OpContext`. The router binds the real one
(populated ``PermissionDecl``, a real ``Workspace`` rooted at the agent's
``workspace_base_dir``, the flattened MCP map) via
``ctx.router_state.op_context_factory``; phase / test callers fall back to a
minimal synthesis.

This bridge is the ONE place that resolves that — extracted from
``tools/file.py`` (#1442) so every delegating tool shares it. A tool that
hand-builds its own ``OpContext`` from ``ctx.workspace`` instead (mcp_verbs did,
pre-#1442) gets ``ctx.workspace`` which is ``None`` on the chat-router path →
the op silently resolves cwd instead of the agent workspace. Routing all such
tools through this single bridge keeps them at parity with file ops by
construction.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reyn.tools.types import ToolContext


def build_legacy_op_context(ctx: "ToolContext") -> Any:
    """Build an OpContext for op_runtime delegation.

    Preferred (= router-side production, ADR-0026 Phase 3.5): use the
    ``ctx.router_state.op_context_factory`` callable bound by RouterLoop. The
    factory yields the same OpContext the legacy router branches received —
    populated PermissionDecl (= operator file/mcp declarations), a real
    Workspace (rooted at the agent's ``workspace_base_dir``), and the flattened
    MCP servers map.

    Fallback (= phase-side dispatch, test sites): synthesize a minimal OpContext
    from ToolContext fields with ``PermissionDecl()`` empty. The fallback is
    documented as M3 transitional in ADR-0026 Open Q #7; callers that need real
    permission gating must populate ``router_state.op_context_factory`` (router)
    or supply ``phase_state.op_context`` (phase) when those wirings land.
    """
    rs = ctx.router_state
    if rs is not None and rs.op_context_factory is not None:
        return rs.op_context_factory()

    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl

    # Propagate the active phase's PermissionDecl via phase_state.op_context
    # (FP-0008 Tool→OpContext bridge fix 2026-05-28).
    phase_op_ctx = (
        ctx.phase_state.op_context if ctx.phase_state is not None else None
    )
    return OpContext(
        workspace=ctx.workspace,
        events=ctx.events,
        permission_decl=(
            phase_op_ctx.permission_decl
            if phase_op_ctx is not None
            else PermissionDecl()
        ),
        permission_resolver=ctx.permission_resolver,
        skill_name="",
    )
