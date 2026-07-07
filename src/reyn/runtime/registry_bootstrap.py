"""Reusable ``AgentRegistry`` construction — extracted from ``reyn chat``'s
startup so a non-interactive, one-shot caller (``reyn pipe run``) can spawn
real ``agent:`` pipeline steps without duplicating/drifting from the same
construction ``reyn chat`` already does.

Corrected scope (see the PR that introduced this module): a pipeline
``agent:`` step's real dispatch (``reyn.runtime.session_api.run_agent_step``)
needs only an ``AgentRegistry`` capable of ``spawn_session_recorded(mode=
"ephemeral")`` + one ``MessageBus`` turn — the lightweight ephemeral-session
primitive, NOT a live chat session, NOT a router loop, and NOT
``run_pipeline``'s own IS-6 driver-session/MessageBus-attach machinery (that
machinery exists for the TOP-LEVEL pipeline run's own crash-resilience, an
unrelated concern to what one ``agent:`` step needs). So a real,
fully-standalone ``AgentRegistry`` — no live chat REPL, no TTY — is both
necessary and sufficient.

Two tiers of extraction, deliberately:

- :func:`build_state_log` / :func:`build_budget_tracker` — the small, purely
  mechanical pieces every frontend factory site builds identically. ``reyn
  chat``'s own ``run()`` now calls these (byte-identical logic, extract-method
  only — see the PR body's before/after diff) so they cannot silently drift
  from ``reyn pipe run``'s copy.
- :func:`build_agent_registry_from_project` — the full standalone
  construction a **minimal, non-interactive, one-shot** caller needs
  (``reyn pipe run`` today). It is deliberately NOT a superset of ``reyn
  chat``'s own richer construction (model selection, ``--exclude-tools``,
  environment-backend choice, interactive CUI logging, ``--grant-file-write``,
  …) — those stay ``chat.py``'s own bespoke bits, built the same way as
  before, on top of the same ``build_scoped_chat_session``/``AgentRegistry``
  seams. Forcing chat's full parameter surface (~25 kwargs) through this
  helper would either (a) duplicate that surface here (real drift risk, zero
  evidenced benefit — no second caller needs it yet) or (b) require
  chat.py to rebuild its registry after calling this helper (impossible —
  ``AgentRegistry``'s ``session_factory`` closure is baked in at
  construction). A future caller that needs chat's fuller scoped surface
  should compose ``build_scoped_chat_session`` directly, as ``chat.py`` does.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reyn.config import ReynConfig
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.budget.budget import BudgetTracker
    from reyn.runtime.registry import AgentRegistry


def build_state_log(project_root: Path) -> "StateLog":
    """The process-shared WAL every frontend anchors on
    ``<project_root>/.reyn/state/wal.jsonl`` (PR21). Extracted from
    ``chat.py``'s identical construction line — no behavior change."""
    from reyn.core.events.state_log import StateLog

    return StateLog(project_root / ".reyn" / "state" / "wal.jsonl")


def build_budget_tracker(
    cost_config: Any, project_root: Path, *, hydrate: bool = True,
) -> "BudgetTracker":
    """The process-shared budget tracker (PR22), optionally hydrated from the
    persistent ledger + in-memory-counter snapshot (PR25 / R-D8).

    ``hydrate=True`` (``reyn chat``'s existing behavior, byte-identical) reads
    the ledger/state files under ``<project_root>/.reyn/state/`` so cap
    enforcement survives a crash + restart across a *multi-turn* session.
    ``hydrate=False`` skips that (a one-shot, single-invocation caller like
    ``reyn pipe run`` has no persistent multi-turn budget to resume — each
    invocation starts a fresh, unlimited-unless-configured tracker)."""
    from reyn.runtime.budget.budget import BudgetTracker

    tracker = BudgetTracker(cost_config)
    if hydrate:
        tracker.hydrate(project_root / ".reyn" / "state" / "budget_ledger.jsonl")
        budget_state_path = project_root / ".reyn" / "state" / "budget_state.json"
        tracker.load_state(budget_state_path)
        tracker.set_state_path(budget_state_path)
    return tracker


def build_agent_registry_from_project(
    project_root: Path,
    config: "ReynConfig",
    *,
    non_interactive: bool = False,
    agent_name: "str | None" = None,
) -> "AgentRegistry":
    """Build a minimal, standalone ``AgentRegistry`` for a non-interactive,
    one-shot caller — e.g. ``reyn pipe run``'s ``agent:`` step support.

    Deliberate v1 scope choices (see module docstring for the "why extract
    only this much" rationale):

    - **No hydration** (:func:`build_budget_tracker` ``hydrate=False``) — a
      one-shot CLI invocation has no persistent multi-turn budget to resume.
    - **Host environment backend only** (``environment_backend=None``,
      ``workspace_base_dir=project_root``, ``workspace_state_dir=
      project_root/".reyn"``) — mirrors ``build_environment_backend``'s own
      host-backend default (``env_backend.py``). No ``--docker``/
      ``--sandbox-backend`` CLI surface for v1; a caller needing a container
      backend should use ``reyn chat``/``reyn run`` instead.
    - **Operator-trusted permissions** — ``file.read``/``file.write``/
      ``http.get`` default to ``allow`` (``setdefault``, so an explicit
      ``reyn.yaml`` permission stays authoritative), mirroring how ``reyn
      pipe install`` already treats a local CLI invocation as an
      operator-trusted entry point (a human running a command directly, not
      an LLM-driven turn).
    - **``interactive=not non_interactive``** on the ``PermissionResolver`` —
      a one-shot caller has no one to answer an interactive approval prompt.
    - **Default model tier** (``config.model``) + a fresh ``ModelResolver``
      built straight from ``config`` — no CLI ``--model`` surface for v1.

    ``agent_name``, if given, is not verified to exist here (registry
    construction always ensures the ``default`` agent's profile exists, per
    ``AgentRegistry.__init__``); the caller decides what identity to spawn
    against (e.g. an ``AgentStep``'s own ``identity`` narrows further, or
    falls back to the pipeline run's ``default_identity``).
    """
    from reyn.config import load_project_context
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.factory_config import SessionFactoryConfig
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.scoped_session_factory import build_scoped_chat_session
    from reyn.security.permissions.permissions import PermissionResolver

    state_log = build_state_log(project_root)
    budget_tracker = build_budget_tracker(config.cost, project_root, hydrate=False)

    perm_config = dict(getattr(config, "permissions", {}) or {})
    # Operator-trusted default grant (mirrors `reyn chat --grant-file-write` /
    # `reyn pipe install`'s CLI-is-operator-trusted posture). setdefault
    # preserves any explicit operator-configured value.
    perm_config.setdefault("file.read", "allow")
    perm_config.setdefault("file.write", "allow")
    perm_config.setdefault("http.get", "allow")
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        file_zone_root=project_root,
        interactive=not non_interactive,
    )

    project_context = load_project_context(config, project_root)
    resolver = ModelResolver(
        config.models,
        default_class=config.model,
        purpose_classes=config.model_class_by_purpose,
    )
    factory_config = SessionFactoryConfig.from_config(config, project_root)
    ws_base_dir = project_root
    ws_state_dir = project_root / ".reyn"

    def _session_factory(profile: "AgentProfile"):
        _ctx_perm, _profile_excluded = registry.resolved_profile_for(profile.name)
        s = build_scoped_chat_session(
            agent_name=profile.name,
            model=config.model,
            resolver=resolver,
            permission_resolver=perm_resolver,
            safety=config.safety,
            mcp_servers=config.mcp,
            output_language=config.output_language,
            prompt_cache_enabled=config.prompt_cache_enabled,
            project_context=project_context,
            agent_role=profile.role,
            compaction_config=config.chat.compaction,
            reasoning_config=config.chat.reasoning,
            registry=registry,
            allowed_mcp=profile.allowed_mcp,
            task_backend=registry.task_backend,
            events_config=config.events,
            cost_warn_config=config.cost_warn,
            state_log=state_log,
            budget_tracker=budget_tracker,
            hooks_config=config.hooks,
            fs_watch_config=config.fs_watch,
            factory_config=factory_config,
            eager_embedding_build=False,
            agent_id=None,
            exclude_tools=None,
            excluded_categories=_profile_excluded,
            contextual_permission=_ctx_perm,
            router_max_iterations=config.safety.loop.max_router_iterations,
            non_interactive=non_interactive,
            environment_backend=None,
            sandbox_backend=None,
            workspace_base_dir=ws_base_dir,
            workspace_state_dir=ws_state_dir,
        )
        s.load_history()
        return s

    registry = AgentRegistry(
        project_root=project_root,
        session_factory=_session_factory,
        state_log=state_log,
        factory_config=factory_config,
        environment_backend=None,
        workspace_state_dir=ws_state_dir,
    )
    return registry
