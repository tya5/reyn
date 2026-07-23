"""#1402: single-source scoped Session construction.

Three frontends build a ``Session`` with overlapping-but-divergent scoped
wiring:

- ``cli/commands/chat.py`` (chat-CLI / ``run-once``) — the full scoped set;
- ``web/deps.py`` (A2A) — partial (capabilities accreted one-by-one);
- ``cli/commands/mcp.py`` ``run_serve`` (stdio MCP) — partial.

A scoped capability hand-added to one factory silently leaked from the others —
the forwarding-gap class (sibling to base_dir #1410, permission-zone #1415,
exec-seam #1419, empty-stop #1424). This factory is the single chokepoint:

- the drift-prone **SCOPED** params are **required** keyword args (no default),
  so every frontend MUST pass them explicitly — ``None`` / off means "not used
  here" *documented*, never silently omitted. Adding a new scoped capability
  here forces all three factories to decide (completeness-by-construction);
- the common base params flow through ``**base`` so a non-scoped ``Session``
  param can never drift between factories.

This is a **behavior-preserving** refactor (#1402 lead decision): each factory
passes its current explicit values, so runtime behaviour is unchanged. The
missing-capability gaps the divergence revealed (e.g. A2A lacks env-backend /
container-rooting) are an explicit-default-documented follow-up — a consumer
that needs one (e.g. an A2A SWE runner) flips that factory's default to a real
value in one line.

The multi-callsite invariant (no factory constructs ``Session`` directly —
all route through here) is pinned by
``tests/test_scoped_session_factory_invariant_1402.py``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from reyn.runtime.agent import Agent
from reyn.runtime.factory_config import SessionFactoryConfig
from reyn.runtime.session import Session
from reyn.runtime.session_params import (
    CapabilityScope,
    PresentationWiring,
    ReactivityConfig,
)

if TYPE_CHECKING:
    from pathlib import Path

    from reyn.runtime.presentation_consumer import PresentationConsumer


def build_scoped_chat_session(
    *,
    # ── SCOPED capability surface (REQUIRED — no defaults) ──────────────────
    # The drift surface: every frontend MUST pass these explicitly. Add a field
    # here when a new scoped capability lands → all three factories are forced
    # to provide it (completeness-by-construction).
    environment_backend: Any,  # EnvironmentBackend | None — agent FS-seam backend instance
    sandbox_backend: Any,  # SandboxBackend | None — agent exec-seam backend instance
    workspace_base_dir: "Path | None",  # #187 chat OpContext FS root (container repo) / None=host cwd
    workspace_state_dir: "Path | None",  # #187 host-side OS state dir
    exclude_tools: "frozenset[str] | set[str] | None",  # #1400 tool names hidden + execution-blocked
    excluded_categories: "frozenset[str] | set[str] | None",  # #1667 catalog categories hidden at source (reyn_repo for external-repo eval)
    contextual_permission: "object | None",  # #1827 S3 per-session capability_profile narrowing (ContextualPermission); from registry.resolved_profile_for; None = no narrowing
    agent_id: str | None,  # FP-0016 agent-id-scoped memory
    router_max_iterations: int,  # #187 per-message tool-call budget
    non_interactive: bool,  # #1439 Fix #1: run-once (no TTY) → SP proceeds instead of asking a clarifying question. Per-frontend: chat-CLI = not isatty(); A2A/MCP/dogfood = False (interactive byte-identical)
    presentation_consumer: "PresentationConsumer",  # #2708 P1: the surface's present-sink CONSUMER (orphan-impossible). Its .sink(session) yields the PresentationRenderer wired per-turn. REQUIRED (no default) so a surface cannot silently omit a present sink; a bare renderer can't be the kwarg because the outbox sink needs the not-yet-built Session (.sink defers it). Per-frontend: CUI=OutboxPresentationConsumer / web/mcp/dogfood=NullPresentationConsumer(reviewed NA)
    eager_embedding_build: bool,  # build the action embedding index up-front
    allowed_mcp: list[str] | None,  # per-profile MCP allow-list
    # ── per-session config — the UNIFORM, reyn.yaml-derived bundle (#2093) ──
    # All previously-individual uniform args (sandbox_config / multimodal_config
    # / action_retrieval_config / embedding_config / router_config / retry_config /
    # chat_tool_use_scheme) now arrive as ONE bundle, built
    # once per frontend via SessionFactoryConfig.from_config — so a new uniform arg is
    # added in one place and reaches all five factory sites (completeness-by-
    # construction; the sandbox_config drift class is structurally prevented).
    factory_config: "SessionFactoryConfig",
    # ── common base (pass-through; session identity/infra, not a drift surface) ──
    **base: Any,
) -> Session:
    """Construct a ``Session`` with the scoped capability + per-session
    config surface passed explicitly. See module docstring for the drift-class
    rationale."""
    # FP-0043 Stage 2: assemble the Agent identity value object at this single
    # construction chokepoint (it gathers every identity input — the explicit
    # scoped env/sandbox/workspace params + the name/model/resolver/role that flow
    # via ``**base``). Session reads all identity fields through this object
    # (delegating properties). This is the prerequisite seam for N Sessions sharing
    # one Agent (a later stage); behaviour here is byte-identical.
    # #3133 Priority-0 step-2: the 9 identity fields are POPPED from ``base``
    # (not just read) so Session — whose flat identity params were removed —
    # receives ``agent=`` exactly once, never a duplicate flat projection.
    _agent_kwargs: dict[str, Any] = dict(
        agent_name=base.pop("agent_name"),
        role=base.pop("agent_role", ""),
        model=base.pop("model", "standard"),
        permission_resolver=base.pop("permission_resolver", None),
        workspace_base_dir=workspace_base_dir,
        workspace_state_dir=workspace_state_dir,
        sandbox_config=factory_config.sandbox_config,
        sandbox_backend=sandbox_backend,
        environment_backend=environment_backend,
    )
    # #3133 P0-follow-up: agent_id folds into Agent (identity SSoT) instead of
    # a separate Session param — None means "let Agent's own default_factory
    # (_default_agent_id) apply", matching the pre-fold fallback behaviour.
    if agent_id is not None:
        _agent_kwargs["agent_id"] = agent_id
    agent = Agent(**_agent_kwargs)
    # #3121 step1: group the scoped capability / presentation / reactivity
    # params into their cohesive parameter objects at this single construction
    # chokepoint. ``intervention_bridge`` and the ``hooks_config``/
    # ``composers_config``/``fs_watch_config`` reyn.yaml blocks arrive via
    # ``**base`` (each frontend passes them straight through to
    # ``build_scoped_chat_session``) -- popped here so they join their
    # PresentationWiring / ReactivityConfig siblings instead of flowing
    # through Session as flat params.
    intervention_bridge = base.pop("intervention_bridge", None)
    reactivity = ReactivityConfig(
        hooks_config=base.pop("hooks_config", None),
        composers_config=base.pop("composers_config", None),
        fs_watch_config=base.pop("fs_watch_config", None),
    )
    return Session(
        agent=agent,
        router_max_iterations=router_max_iterations,
        non_interactive=non_interactive,
        eager_embedding_build=eager_embedding_build,
        allowed_mcp=allowed_mcp,
        reactivity=reactivity,
        capability_scope=CapabilityScope(
            exclude_tools=exclude_tools,
            excluded_categories=excluded_categories,
            contextual_permission=contextual_permission,
            available_skills=factory_config.available_skills,  # #2548 PR-A
            skill_collisions=factory_config.skill_collisions,  # #3100 Axis 4
        ),
        presentation_wiring=PresentationWiring(
            presentation_registry=factory_config.presentation_registry,  # FP-0054 PR-C
            presentation_consumer=presentation_consumer,  # #2708 P1: present-sink consumer (.sink deferred to Session init)
            intervention_bridge=intervention_bridge,
        ),
        multimodal_config=factory_config.multimodal_config,
        action_retrieval_config=factory_config.action_retrieval_config,
        embedding_config=factory_config.embedding_config,
        router_config=factory_config.router_config,
        retry_config=factory_config.retry_config,  # #1835
        chat_tool_use_scheme=factory_config.chat_tool_use_scheme,
        pipeline_registry=factory_config.pipeline_registry,  # #2575
        observability_config=factory_config.observability_config,  # P5 ADR-0039
        **base,
    )
