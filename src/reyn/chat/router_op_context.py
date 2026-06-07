"""#1412: single-source the chat-router OpContext construction.

Two hosts built the ``skill_name="chat_router"`` OpContext with ~95% identical
code: ``ChatSession._make_router_op_context`` (session.py) and
``RouterHostAdapter.make_router_op_context`` (services/router_host_adapter.py).
They drifted — #1410/#1411 threaded ``base_dir`` to one and lagged the other
(the #187 wrong-FS class). This factory is the single source for the common
construction (PermissionDecl with the #571 axes, the canonical ``.reyn/`` write
paths + session-approval, the Workspace FS root, the OpContext).

**Behavior-preserving** (#1412 lead decision): the fields that legitimately
differ per host — ``agent_id`` / ``intervention_bus`` / ``multimodal_config`` /
``media_store`` / ``compact_now`` / ``run_id`` — are caller-supplied params, so
each host passes its CURRENT values (incl. ``None`` where it doesn't wire one).
The divergence the single-sourcing makes explicit (e.g. RouterHostAdapter never
wires ``agent_id``) is surfaced for a follow-up classification, NOT folded here
(same "root-fix surfaces a latent gap" pattern as #1402 -> #1431).

Which-ops trace (#1412): ChatSession's impl serves file ops (``_file_op``) +
MCP ops (which wire ``intervention_bus`` POST-HOC on the returned ctx), so its
``intervention_bus=None`` at construction is a wiring-style difference, not a
missing capability; media/multimodal/compact ops go via the registry /
RouterHostAdapter path. The lone capability-gap candidate is RouterHostAdapter's
unset ``agent_id`` (registry-dispatched memory ops lose agent-scope) — a
follow-up to classify drift-vs-intentional.
"""
from __future__ import annotations

from typing import Any

# Canonical OS mutation paths the chat router declares + session-approves so
# LLM-emitted mcp_install / index_drop / mcp_drop_server ops pass the uniform
# permission gates without per-op prompts (#571 collapse arc).
_CANONICAL_WRITE_PATHS = (
    ".reyn/mcp.yaml",
    ".reyn/cron.yaml",
    ".reyn/index/sources.yaml",
)


def build_router_op_context(
    *,
    events: Any,
    permission_resolver: Any,
    file_permissions: dict | None,
    mcp_servers: list[dict] | None,
    mcp_servers_flat: list[dict],
    allowed_mcp: list[str] | None,
    workspace_base_dir: Any,  # Path | None — #187 container repo root
    workspace_state_dir: Any,  # Path | None — host-side OS state dir
    environment_backend: Any,  # FS seam backend instance (#1200 PR-F1)
    sandbox_backend: Any,  # exec seam backend instance (#1200 PR-F2)
    sandbox_policy: Any,  # raw policy → resolve_sandbox_policy here (#1339)
    # ── per-host fields (caller-supplied; behavior-preserving) ─────────────
    agent_id: str | None,  # FP-0016 memory scope (RouterHostAdapter: None — gap candidate)
    intervention_bus: Any = None,  # ChatSession wires post-hoc; RouterHostAdapter inline
    multimodal_config: Any = None,  # #364
    media_store: Any = None,  # #383
    compact_now: Any = None,  # #272/#1128
    run_id: str | None = None,  # chat router is outside run scope (#FP-0021)
) -> Any:
    """Build the chat-router OpContext (the single source for both hosts).

    The PermissionDecl + canonical-path approval + Workspace + OpContext are
    identical across hosts; per-host fields are passed explicitly. See module
    docstring for the drift-class + behavior-preserving rationale."""
    from reyn.op_runtime.context import OpContext
    from reyn.permissions.permissions import PermissionDecl
    from reyn.sandbox.policy import resolve_sandbox_policy
    from reyn.workspace.workspace import Workspace

    file_perms = file_permissions or {}
    servers = mcp_servers or []

    file_read = [{"path": p, "scope": "recursive"} for p in file_perms.get("read", [])]
    file_write = [{"path": p, "scope": "recursive"} for p in file_perms.get("write", [])]
    mcp_names = [s["name"] for s in servers]

    # #571 collapse arc Phase 5: explicit list axes for the canonical mutations.
    file_write = list(file_write) + [
        {"path": p, "scope": "just_path"} for p in _CANONICAL_WRITE_PATHS
    ]
    decl = PermissionDecl(
        file_read=file_read,
        file_write=file_write,
        mcp=mcp_names,
        allowed_mcp=allowed_mcp,
        # #571 Phase 7: wildcard http.get (per-host 4-layer prompt at runtime) +
        # the MCP registry host specifically (mcp_install startup_guard pre-approval).
        http_get=[
            {"host": "registry.modelcontextprotocol.io"},
            {"host": "*"},
        ],
        # #571 Phase 6: wildcard secret.write (operator per-value prompt is the gate).
        secret_write=["*"],
    )
    # Session-approve the canonical OS mutation paths so require_file_write passes
    # silently for LLM-emitted ops. Skipped when no resolver (ad-hoc test ctx).
    if permission_resolver is not None:
        for canonical in _CANONICAL_WRITE_PATHS:
            permission_resolver.session_approve_path(canonical, "chat_router", "file.write")

    workspace = Workspace(
        events=events,
        permission_resolver=permission_resolver,
        skill_name="chat_router",
        # #187: chat OpContext FS root = the container repo root with a container
        # env-backend (e.g. /testbed); state_dir stays host-side. None → cwd default.
        base_dir=workspace_base_dir,
        state_dir=workspace_state_dir,
        environment_backend=environment_backend,
    )
    return OpContext(
        workspace=workspace,
        events=events,
        permission_decl=decl,
        permission_resolver=permission_resolver,
        skill_name="chat_router",
        mcp_servers=mcp_servers_flat,
        run_id=run_id,
        agent_id=agent_id,
        intervention_bus=intervention_bus,
        multimodal_config=multimodal_config,
        media_store=media_store,
        compact_now=compact_now,
        sandbox_backend=sandbox_backend,
        # #1339: resolve the operator-or-default sandbox policy (was None → the
        # op_runtime handler fell back to LLM-set op fields = sandbox-escape gap).
        default_sandbox_policy=resolve_sandbox_policy(
            sandbox_policy,
            write_paths=[str(workspace.base_dir)],
        ),
    )
