"""Shared FastAPI dependencies for reyn.web.

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
from typing import Annotated

from fastapi import Depends

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
        tracker = BudgetTracker(config.cost)
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
        from reyn.permissions.permissions import PermissionResolver
        config = _load_config()
        root = _get_project_root()
        perm_config = getattr(config, "permissions", {}) or {}
        _perm_resolver = PermissionResolver(
            config_permissions=perm_config,
            project_root=root,
            # Web gateway: non-interactive. Permission prompts become denials
            # unless pre-approved in reyn.yaml / .reyn/approvals.yaml.
            interactive=False,
        )
    return _perm_resolver


def get_perm_resolver():
    return _get_perm_resolver()


# ---------------------------------------------------------------------------
# AgentRegistry  (process-shared)
# ---------------------------------------------------------------------------

_registry = None


def _get_registry():
    global _registry
    if _registry is None:
        from reyn.chat.profile import AgentProfile
        from reyn.chat.registry import AgentRegistry
        from reyn.chat.session import ChatSession
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
        resolver = ModelResolver(config.models)
        model = config.model
        output_language = config.output_language

        limits = config.limits

        # registry is referenced inside the factory closure — defined below.
        registry_ref: list = []

        def _session_factory(profile: AgentProfile) -> ChatSession:
            registry = registry_ref[0]
            s = ChatSession(
                agent_name=profile.name,
                model=model,
                resolver=resolver,
                permission_resolver=perm_resolver,
                limits=limits,
                mcp_servers=config.mcp,
                output_language=output_language,
                prompt_cache_enabled=config.prompt_cache_enabled,
                project_context=project_context,
                agent_role=profile.role,
                compaction_config=config.chat.compaction,
                registry=registry,
                max_hop_depth=config.multi_agent.max_hop_depth,
                chain_timeout_seconds=config.multi_agent.chain_timeout_seconds,
                allowed_skills=profile.allowed_skills,
                events_config=config.events,
                state_log=state_log,
                budget_tracker=budget_tracker,
            )
            s.load_history()
            return s

        registry = AgentRegistry(
            project_root=root,
            session_factory=_session_factory,
            state_log=state_log,
        )
        registry_ref.append(registry)
        _registry = registry

    return _registry


def get_registry():
    return _get_registry()


__all__ = [
    "get_project_root",
    "get_reyn_config",
    "get_state_log",
    "get_budget_tracker",
    "get_perm_resolver",
    "get_registry",
    "ProjectRoot",
]
