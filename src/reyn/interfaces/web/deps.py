"""Shared FastAPI dependencies for reyn.interfaces.web.

Mirrors the construction order in cli/commands/chat.py:
    BudgetTracker → hydrate → PermissionResolver → AgentRegistry

The dependency graph is:
    project_root  (cached application-lifetime singleton)
    ↓
    reyn_config   (loaded once from reyn.yaml / reyn.local.yaml)
    ↓
    state_log     (process-shared WAL for crash recovery, PR21)
    budget_tracker (process-shared cost enforcer, PR22/PR25)
    perm_resolver  (process-shared permission gating)
    ↓
    agent_registry (process-shared agent + session lifecycle)

All heavy objects are created once via module-level singletons and exposed
as FastAPI Depends callables so the router files stay thin.

Per P7: no skill-specific strings anywhere in this module. All engine data
is treated as opaque from the gateway's perspective.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from reyn.interfaces.web.run_registry import RunRegistry

# ---------------------------------------------------------------------------
# project_root discovery
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_project_root() -> Path:
    """Discover project root via reyn.config._find_project_root, same as chat.py."""
    from reyn.config import _find_project_root
    return _find_project_root(Path.cwd()) or Path.cwd()


def get_project_root() -> Path:
    return _get_project_root()


ProjectRoot = Annotated[Path, Depends(get_project_root)]


# ---------------------------------------------------------------------------
# ReynConfig
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_config():
    from reyn.config import load_config
    return load_config()


def get_reyn_config():
    return _load_config()


# ---------------------------------------------------------------------------
# StateLog  (PR21 WAL — process-shared)
# ---------------------------------------------------------------------------

_state_log = None


def _get_state_log():
    global _state_log
    if _state_log is None:
        from reyn.events.state_log import StateLog
        root = _get_project_root()
        _state_log = StateLog(root / ".reyn" / "state" / "wal.jsonl")
    return _state_log


def get_state_log():
    return _get_state_log()


# ---------------------------------------------------------------------------
# BudgetTracker  (PR22/PR25 — process-shared)
# ---------------------------------------------------------------------------

_budget_tracker = None


def _get_budget_tracker():
    global _budget_tracker
    if _budget_tracker is None:
        from reyn.budget.budget import BudgetTracker
        config = _load_config()
        root = _get_project_root()
        tracker = BudgetTracker(config.cost, safety=config.safety)
        tracker.hydrate(root / ".reyn" / "state" / "budget_ledger.jsonl")
        # R-D8: restore in-memory counters (per-agent / per-chain-skill)
        # for cap enforcement across crash.
        budget_state_path = root / ".reyn" / "state" / "budget_state.json"
        tracker.load_state(budget_state_path)
        tracker.set_state_path(budget_state_path)
        _budget_tracker = tracker
    return _budget_tracker


def get_budget_tracker():
    return _get_budget_tracker()


# ---------------------------------------------------------------------------
# PermissionResolver  (process-shared — .reyn/approvals.yaml is process-wide)
# ---------------------------------------------------------------------------

_perm_resolver = None


def _get_perm_resolver():
    global _perm_resolver
    if _perm_resolver is None:
        from reyn.security.permissions.permissions import PermissionResolver
        config = _load_config()
        root = _get_project_root()
        # Copy so the #1401 grant setdefault below never mutates the shared config.
        perm_config = dict(getattr(config, "permissions", {}) or {})
        # Mirror the CLI path: unsafe python steps require an explicit opt-in.
        # In the web gateway (non-interactive) the equivalent of --allow-unsafe-python
        # is python.unsafe: allow in reyn.yaml / reyn.local.yaml permissions.
        unsafe_python_allowed = perm_config.get("python.unsafe") == "allow"
        # #1401: --grant-file-write grants file.read/write at the resolver layer
        # (mirrors `reyn chat`/run.py/eval). Bounded by the sandbox write_paths ∩
        # the env-backend repo zone. setdefault preserves explicit operator config.
        _ov = get_cli_scoped_overrides()
        if _ov.grant_file_write:
            perm_config.setdefault("file.read", "allow")
            perm_config.setdefault("file.write", "allow")
        _perm_resolver = PermissionResolver(
            config_permissions=perm_config,
            project_root=root,
            # #1401/#1414: anchor the default file-zone on the container repo root
            # under a container env-backend (None for host → project_root default).
            file_zone_root=_ov.workspace_base_dir,
            # Web gateway: non-interactive. Permission prompts become denials
            # unless pre-approved in reyn.yaml / .reyn/approvals.yaml.
            interactive=False,
            unsafe_python_allowed=unsafe_python_allowed,
        )
    return _perm_resolver


def get_perm_resolver():
    return _get_perm_resolver()


# ---------------------------------------------------------------------------
# FP-0041 #489 PR-D2.5: external transport outbox interceptor wiring
# ---------------------------------------------------------------------------


def _wire_external_outbox_interceptor(session, routing) -> None:
    """Build and attach the external transport outbox interceptor.

    Composes:
      - per-session MCP dispatcher (= closes over ``session`` so the
        tool call uses the session's router OpContext: workspace,
        events log, permission resolver).
      - ``make_outbox_interceptor`` factory (= dispatch matrix landed
        in PR-D2: ExternalRef + dispatchable kind → route_to_mcp).

    Sets ``session._outbox_interceptor`` so ``_put_outbox`` consults
    it on each agent reply.

    The dispatcher resolves the MCP tool name ``<server>__<tool>``
    (= the convention ``external_transports`` config uses) into a
    server + tool pair, builds an ``MCPIROp``, and dispatches via
    ``op_runtime.mcp.handle`` — the same path the LLM-callable
    ``call_mcp_tool`` uses. Permission gating runs (= operator must
    have declared ``mcp: [<server>]`` for the agent OR config-level
    allow); failures propagate as exceptions and are surfaced through
    ``RouteResult(status="error", ...)`` by the routing primitive.
    """
    from reyn.chat.external_routing import make_outbox_interceptor

    async def _mcp_dispatcher(mcp_tool: str, args: dict):
        if "__" not in mcp_tool:
            raise ValueError(
                f"external_transports mcp_tool must be '<server>__<tool>', "
                f"got {mcp_tool!r}",
            )
        server, tool = mcp_tool.split("__", 1)
        from reyn.op_runtime.mcp import handle as mcp_handle
        from reyn.schemas.models import MCPIROp
        op = MCPIROp(kind="mcp", server=server, tool=tool, args=dict(args))
        ctx = session._make_router_op_context()
        return await mcp_handle(op=op, ctx=ctx, caller="external_routing")

    interceptor = make_outbox_interceptor(
        routing=routing,
        mcp_dispatcher=_mcp_dispatcher,
    )
    session._outbox_interceptor = interceptor


# ---------------------------------------------------------------------------
# #1401: CLI-scoped capability overrides (env-backend / exclude-tools / grant)
# ---------------------------------------------------------------------------
# `reyn web` with the scoped flags builds the env-backend INSTANCE in the CLI
# process and threads it here as a module-global (NOT an env-var: an instance
# can't ride a string, and rebuilding it app-side would double-build/attach the
# container = re-introducing the very drift class #1402/#1412 rooted). The lazy
# process-global session factory + perm resolver read it; they are NOT
# request-scoped, so app.state — read via Request in handlers — does not fit
# (using a module-global is a factory-shape decision, not ignoring a seam).
# Only set under no-reload (same process); --reload is guarded at the CLI.
from contextlib import contextmanager  # noqa: E402
from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class CliScopedOverrides:
    """The `reyn web` scoped capability overrides (#1401). All defaults = the
    pre-#1401 web/A2A behaviour (no env-backend, no exclude, no grant)."""

    environment_backend: object | None = None  # FS+exec backend INSTANCE (single-shared)
    workspace_base_dir: object | None = None   # container repo root (Path | None)
    workspace_state_dir: object | None = None  # host-side OS state dir (Path | None)
    exclude_tools: "frozenset | None" = None   # tool names hidden from the LLM catalog
    grant_file_write: bool = False             # grant file.read/write at the resolver


_cli_scoped: "CliScopedOverrides | None" = None


def set_cli_scoped_overrides(overrides: "CliScopedOverrides | None") -> None:
    """Set/clear the CLI-scoped overrides. `reyn web` run() calls this ONCE
    before uvicorn.run (no-reload). Resetting the cached lazy singletons here
    keeps the next perm-resolver / registry build pick the new overrides up."""
    global _cli_scoped, _perm_resolver, _registry
    _cli_scoped = overrides
    _perm_resolver = None
    _registry = None


def get_cli_scoped_overrides() -> "CliScopedOverrides":
    return _cli_scoped or CliScopedOverrides()


@contextmanager
def cli_scoped_overrides(overrides: "CliScopedOverrides"):
    """Test isolation: apply the overrides for the block, restore after
    (incl. the cached perm-resolver / registry singletons)."""
    global _cli_scoped, _perm_resolver, _registry
    prev = (_cli_scoped, _perm_resolver, _registry)
    set_cli_scoped_overrides(overrides)
    try:
        yield
    finally:
        _cli_scoped, _perm_resolver, _registry = prev


# ---------------------------------------------------------------------------
# AgentRegistry  (process-shared)
# ---------------------------------------------------------------------------

_registry = None


def _get_registry():
    global _registry
    if _registry is None:
        from reyn.chat.profile import AgentProfile
        from reyn.chat.registry import AgentRegistry
        from reyn.chat.scoped_session_factory import build_scoped_chat_session
        from reyn.config import load_project_context

        config = _load_config()
        root = _get_project_root()
        state_log = _get_state_log()
        budget_tracker = _get_budget_tracker()
        perm_resolver = _get_perm_resolver()
        project_context = load_project_context(config, root)

        import os
        if config.api_base:
            os.environ.setdefault("LITELLM_API_BASE", config.api_base)

        from reyn.llm.model_resolver import ModelResolver
        resolver = ModelResolver(
            config.models,
            default_class=config.model,
            purpose_classes=config.model_class_by_purpose,
        )
        model = config.model
        output_language = config.output_language

        # registry is referenced inside the factory closure — defined below.
        registry_ref: list = []

        # `reyn web --eager-embedding-build` flag (parity with `reyn chat`).
        # When set, ChatSession waits for the action_embedding_index build
        # synchronously on the first router turn so ``search_actions`` is
        # visible in tools[] from Turn 1 instead of only after the
        # background build completes. Default False keeps existing
        # behaviour (background build, ``search_actions`` initially
        # hidden until the index is ready).
        _eager_embedding_build = (
            os.environ.get("REYN_WEB_EAGER_EMBEDDING_BUILD", "").strip() == "1"
        )

        def _session_factory(profile: AgentProfile) -> ChatSession:
            registry = registry_ref[0]
            _scoped = get_cli_scoped_overrides()  # #1401 CLI-scoped capabilities
            s = build_scoped_chat_session(
                agent_name=profile.name,
                model=model,
                resolver=resolver,
                permission_resolver=perm_resolver,
                safety=config.safety,
                mcp_servers=config.mcp,
                output_language=output_language,
                prompt_cache_enabled=config.prompt_cache_enabled,
                project_context=project_context,
                agent_role=profile.role,
                compaction_config=config.chat.compaction,
                reasoning_config=config.chat.reasoning,  # #1652
                registry=registry,
                allowed_skills=profile.allowed_skills,
                events_config=config.events,
                state_log=state_log,
                budget_tracker=budget_tracker,
                # B52 retro fix: A2A-side ChatSession was missing
                # ``sandbox_config`` propagation — ``reyn.yaml`` ``sandbox.backend``
                # set in reyn.local.yaml never reached the sandboxed_exec
                # handler via the chat-router path. Cron-side
                # ``web/server.py`` already passes this; the A2A factory
                # was the only gap. Surfaced by B52 W3-S5 retest where
                # ``sandbox.backend: noop`` config loaded correctly but
                # the runtime kept using Seatbelt.
                sandbox_config=config.sandbox,
                multimodal_config=config.multimodal,
                tool_calls_op_loop_skills=config.tool_calls_op_loop_skills,
                action_retrieval_config=config.action_retrieval,
                chat_tool_use_scheme=config.tool_use.chat,  # #1593 PR-2
                embedding_config=config.embedding,
                eager_embedding_build=_eager_embedding_build,
                # #1401: the 3 scoped capabilities, filled from the CLI override
                # holder (env-backend INSTANCE → both FS+exec seams = single-shared
                # sandbox #1200; container-rooting; exclude-tools). Default None =
                # the pre-#1401 web/A2A behaviour, so a plain `reyn web` is
                # byte-identical. allowed_mcp / agent_id / router_max_iterations
                # remain the #1431 item-2 gaps (separate capability decision).
                allowed_mcp=None,
                agent_id=None,
                exclude_tools=_scoped.exclude_tools,
                router_max_iterations=config.safety.loop.max_router_iterations,
                non_interactive=False,  # #1439 Fix #1: A2A byte-identical (run-once-only fix). A2A-non-interactive = documented follow-up (cf factory module doc)
                environment_backend=_scoped.environment_backend,
                sandbox_backend=_scoped.environment_backend,
                workspace_base_dir=_scoped.workspace_base_dir,
                workspace_state_dir=_scoped.workspace_state_dir,
            )
            s.load_history()
            # FP-0041 #489 PR-D2.5: external transport outbox interceptor.
            # When the operator has declared ``external_transports`` in
            # reyn.yaml (= e.g. Slack / LINE / Discord routing), build
            # the per-session interceptor so agent replies to inbox
            # messages carrying ``ExternalRef`` reply_to are dispatched
            # via the configured MCP tools instead of the TUI display
            # queue. The dispatcher closure captures ``s`` so each
            # session's MCP-tool invocations route through that
            # session's own router OpContext (= permission gate,
            # workspace, events log all from the right session).
            if config.external_transports.transports:
                _wire_external_outbox_interceptor(s, config.external_transports)
            return s

        registry = AgentRegistry(
            project_root=root,
            session_factory=_session_factory,
            state_log=state_log,
            # #1544: container shadow-git. ``_scoped`` is local to the session
            # factory above; fetch the overrides at this scope via the accessor.
            environment_backend=get_cli_scoped_overrides().environment_backend,
            # #1557 gap-#1: shadow git-dir under --state-dir (same accessor scope).
            workspace_state_dir=get_cli_scoped_overrides().workspace_state_dir,
            # #1582: time-travel workspace-capture opt-out (reyn.yaml config).
            workspace_capture=config.time_travel.workspace_capture,
            act_turn_capture=config.time_travel.act_turn_capture,  # #1560 opt-in
        )
        registry_ref.append(registry)
        _registry = registry

    return _registry


def get_registry():
    return _get_registry()


# ---------------------------------------------------------------------------
# RunRegistry  (process-singleton — attached to app.state.run_registry)
# ---------------------------------------------------------------------------


def get_run_registry(request: Request) -> "RunRegistry":
    """FastAPI dependency: return the process-singleton RunRegistry.

    Attached to ``app.state.run_registry`` by ``reyn.interfaces.web.server``.
    """
    return request.app.state.run_registry


__all__ = [
    "get_project_root",
    "get_reyn_config",
    "get_state_log",
    "get_budget_tracker",
    "get_perm_resolver",
    "get_registry",
    "get_run_registry",
    "ProjectRoot",
]
