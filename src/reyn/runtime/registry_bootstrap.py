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
  environment-backend choice, interactive CUI logging, …) — those stay
  ``chat.py``'s own bespoke bits, built the same way as before, on top of
  the same ``build_scoped_chat_session``/``AgentRegistry`` seams. Fail-
  closed-by-default permissions ARE ported (``perm_config`` reads exactly
  ``reyn.yaml``'s own ``permissions:`` section, byte-identical to
  ``chat.py``'s own no-flag posture) since that is a correctness/security
  property this helper must not silently drop, not merely a CLI
  convenience — #3924 removed the CLI-level ``--grant-file-write`` flag
  both this helper and ``chat.py`` used to also read (owner ruling:
  per-invocation permission flags don't scope well in a multi-agent
  system; measured zero real call sites outside its own tests). An
  operator opts a project into that capability durably via
  ``permissions.file.write: ["<zone-root>"]`` in ``reyn.yaml`` instead.
  Forcing chat's full parameter surface (~25 kwargs) through this
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
    ``BudgetTracker.hydrate`` (#2945) bounds this to a compacted checkpoint +
    ledger tail rather than a full lifetime re-parse — see its docstring.
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
    - **Fail-closed permissions by default** — ``perm_config`` is exactly
      whatever ``reyn.yaml``'s own ``permissions:`` section declares, byte-
      identical to ``reyn chat``'s own no-flag default (#3924 removed the
      CLI-level ``--grant-file-write`` flag both this helper and
      ``chat.py`` used to also read; an operator opts a project into
      ``file.read``/``file.write`` durably via ``reyn.yaml`` instead).
      ``http.get`` is NEVER blanket-granted here, matching ``reyn chat``
      (which relies on ``require_http_get``'s interactive JIT-approval
      prompt instead of a blanket grant); a non-interactive caller without
      a JIT prompt to answer is correctly denied HTTP access unless
      ``reyn.yaml`` itself grants it — the same outcome a non-interactive
      ``reyn chat`` invocation would have. A pipeline installed from an
      untrusted source (``reyn pipe install --source``) gains only what
      ``reyn.yaml`` already grants merely by being RUN — no per-invocation
      widening.
    - **``interactive=not non_interactive``** on the ``PermissionResolver`` —
      a one-shot caller has no one to answer an interactive approval prompt.
    - **Default model tier** (``config.llm.model``) + a fresh ``ModelResolver``
      built straight from ``config`` — no CLI ``--model`` surface for v1.

    ``agent_name``, if given, is not verified to exist here (registry
    construction always ensures the ``default`` agent's profile exists, per
    ``AgentRegistry.__init__``); the caller decides what identity to spawn
    against (e.g. an ``AgentStep``'s own ``identity`` narrows further, or
    falls back to the pipeline run's ``default_identity``).
    """
    from reyn.config import load_project_context, resolve_project_context_path
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.factory_config import SessionFactoryConfig
    from reyn.runtime.presentation_consumer import OutboxPresentationConsumer
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.scoped_session_factory import build_scoped_chat_session
    from reyn.security.permissions.permissions import PermissionResolver

    state_log = build_state_log(project_root)
    budget_tracker = build_budget_tracker(config.cost, project_root, hydrate=False)

    perm_config = dict(getattr(config, "permissions", {}) or {})
    # Fail-closed by default (byte-identical to `reyn chat`'s own no-flag
    # posture) — NEVER blanket-grant http.get (see docstring). #3924 removed
    # the --grant-file-write CLI flag this used to also check.
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        file_zone_root=project_root,
        interactive=not non_interactive,
    )

    project_context = load_project_context(config, project_root)
    # #3787: the resolved path (not just the content) so Session can watch it
    # for edits at the turn boundary — read-only detection, see
    # ProjectContextWatcher's module docstring for why this is not the #2073
    # hot-reload IN-set.
    project_context_path = resolve_project_context_path(config, project_root)
    resolver = ModelResolver(
        config.llm.models,
        default_class=config.llm.model,
        purpose_classes=config.llm.model_class_by_purpose,
        model_max_class=config.llm.model_max_class,  # #4206 T1 (②bounding)
    )
    # #4689: register this resolver's llm.models.<tier>.max_input_tokens
    # declarations (class -> resolved model string -> declared ceiling)
    # into the process-shared registry reyn.llm.model_budget's 8 call
    # sites all consult — done HERE (a ModelResolver-construction site),
    # not inside config parsing, because a class -> model-string
    # resolution needs the resolver itself, which does not exist yet at
    # config-load time.
    from reyn.llm.model_budget import register_max_input_overrides

    register_max_input_overrides(resolver.max_input_token_overrides())
    factory_config = SessionFactoryConfig.from_config(config, project_root)
    ws_base_dir = project_root
    ws_state_dir = project_root / ".reyn"

    def _session_factory(profile: "AgentProfile", *, presentation_consumer=None, intervention_bridge=None):
        _ctx_perm, _profile_excluded = registry.resolved_profile_for(profile.name)
        s = build_scoped_chat_session(
            # #2708 P1: the reusable registry base session (reyn pipe run's default
            # identity + driver spawns). Outbox-backed, byte-identical to the pre-#2708
            # uniform default: pipe run OVERRIDES the OpContext sink post-hoc with a
            # self-delivering stdout renderer (pipe.py). #2708 P3.1: an ATTACHED pipeline
            # driver spawn now passes a parent-bound SpawnBridgePresentationConsumer
            # override (present reaches the parent by construction, replacing the removed
            # #2707 forward); None (default / non-spawn) keeps the outbox-backed consumer.
            presentation_consumer=presentation_consumer or OutboxPresentationConsumer(),
            # #2708 P3.2a: forward the attached pipeline driver's intervention bridge
            # (SpawnBridgeInterventionListener) so a driver ask_user reaches the parent's live
            # operator; None (default / non-spawn / detached) = self-bound fail-closed.
            intervention_bridge=intervention_bridge,
            agent_name=profile.name,
            model=config.llm.model,
            resolver=resolver,
            permission_resolver=perm_resolver,
            safety=config.safety,
            mcp_servers=config.mcp,
            output_language=config.output_language,
            prompt_cache_enabled=config.llm.prompt_cache_enabled,
            project_context=project_context,
            project_context_path=project_context_path,
            agent_role=profile.role,
            compaction_config=config.chat.compaction,
            reasoning_config=config.chat.reasoning,
            empty_stop_retry=config.chat.empty_stop_retry,  # #4677
            registry=registry,
            allowed_mcp=profile.allowed_mcp,
            events_config=config.audit_events,
            cost_warn_config=config.cost_warn,
            offload_config=config.offload,
            render_template_config=config.render_template,
            state_log=state_log,
            budget_tracker=budget_tracker,
            hooks_config=config.hooks,
            composers_config=config.composers,
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
    )
    return registry
