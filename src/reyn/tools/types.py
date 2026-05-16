"""Type definitions for the unified tool registry (ADR-0026 M1/M4).

ToolDefinition is the single source of truth for a capability's
identity, metadata, gates, and handler. ToolGates encodes the
per-protocol allow/deny declaration. ToolContext is the
protocol-agnostic execution context handed to handlers; per-protocol
dispatchers build it before invocation. ToolHandler is the async
callable signature.

M4 Phase 2: RouterCallerState and PhaseCallerState replace the loose
Any types on ToolContext.router_state and ToolContext.phase_state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol


# ToolGates: per-protocol allow/deny gate at the registry level.
# This is Layer 1 of the 3-layer gate model (= ADR-0026 §3):
#   Layer 1: role gate (this dataclass)
#   Layer 2: phase narrowing (Phase.allowed_ops)
#   Layer 3: permission resolver (per-call runtime)
@dataclass(frozen=True)
class ToolGates:
    router: Literal["allow", "deny"] = "allow"
    phase:  Literal["allow", "deny"] = "allow"


# ToolResult: canonical result shape returned by handlers. Each
# protocol-specific dispatcher adapts this shape to its own surface
# (= router serializes to JSON string for tool_result content;
# phase wraps in {kind, status} envelope for control_ir_results).
# Handler returns whatever Mapping[str, Any] makes semantic sense
# for the capability; dispatcher does the shape adaptation.
ToolResult = Mapping[str, Any]


@dataclass
class RouterCallerState:
    """Per-protocol state that router-style invocations need access to.

    Populated by RouterLoop / dispatch_tool when invoking a
    ToolDefinition handler in router context. Handlers that need
    session-scoped resources (skill registry, agent registry, etc.)
    or async-dispatch callbacks (send_to_agent, dispatch_plan_tool)
    consume them via this object.

    All fields are Optional to allow:
    - Test contexts that need only a subset
    - Gradual migration where Wave 1+2 NotImplementedError stubs
      become real handlers as fields are populated

    M3 / M4 Phase 1 / 2 status: structure defined; production
    population (= router_loop wiring) is M4 Phase 3.
    """
    # Catalog discovery (= for catalog tools list_skills /
    # describe_skill / list_agents / describe_agent handlers)
    skill_registry: Any = None
    agent_registry: Any = None
    available_skills: list[Mapping[str, Any]] | None = None
    available_agents: list[Mapping[str, Any]] | None = None

    # Async dispatch callbacks (= for delegate_to_agent / plan
    # handlers that need to interact with chain / task lifecycle)
    send_to_agent: Callable[..., Awaitable[Any]] | None = None
    dispatch_plan_tool: Callable[..., Awaitable[Any]] | None = None

    # Session-scoped chain identity (= for plan tool, delegate
    # tool, etc.)
    chain_id: str | None = None

    # Cost / model context (= for plan tool cost-aware decomposition)
    budget: Any = None                    # BudgetGateway instance
    router_model: str | None = None
    available_tool_names: list[str] | None = None

    # Memory access (= for memory tools when invoked router-side;
    # router uses MemoryService directly, phase wraps via
    # ctx.workspace callbacks)
    memory_service: Any = None

    # Catalog access callbacks (= for catalog stub handlers
    # list_skills / describe_skill / list_agents / describe_agent;
    # RouterLoop populates with bound methods, the stubs delegate
    # to keep router/registry decoupled from RouterLoopHost type)
    list_skills_fn: Callable[[str], list[Mapping[str, Any]]] | None = None
    describe_skill_fn: Callable[[str], Mapping[str, Any]] | None = None
    list_agents_fn: Callable[[str], list[Mapping[str, Any]]] | None = None
    describe_agent_fn: Callable[[str], Mapping[str, Any]] | None = None

    # OpContext factory (= for file / mcp / web handlers that delegate
    # to op_runtime).  Bound by RouterLoop to ``host.make_router_op_context``
    # so handlers can build a permission-aware OpContext (= populated
    # PermissionDecl + Workspace + skill_name="chat_router") matching the
    # legacy router branch behavior.  When None, handlers fall back to
    # minimal OpContext synthesis (= test sites / phase-side).
    op_context_factory: Callable[[], Any] | None = None

    # Skill invocation callback (= for invoke_skill handler; bound by
    # RouterLoop to ``host.run_skill_awaitable`` with chain_id pre-applied
    # so the multi-hop chain identity propagates into nested run_skill /
    # delegate_to_agent paths.  Without this, ``invoke_skill`` via
    # op_runtime caller="control_ir" would not carry chain_id and PR14
    # pending_chain semantics would break for sub-skill delegations.
    #
    # FP-0012: blocking call — used by plan-mode steps that need the
    # nested skill's result inline to feed the next step. Chat-mode now
    # prefers ``spawn_skill_fn`` (below) for non-blocking dispatch.
    run_skill_fn: Callable[..., Awaitable[Any]] | None = None

    # FP-0012: non-blocking skill dispatch callback. When set, the
    # ``invoke_skill`` handler prefers this over ``run_skill_fn`` and
    # returns the spawn-ack dict (``{status: "spawned", run_id,
    # chain_id, note}``) immediately. The actual skill task runs in
    # the background; completion is delivered to the chat router via
    # the ``"skill_completed"`` inbox kind which injects a user-role
    # message into the existing conversation thread for narration.
    # Plan-mode RouterLoops bind this to None so plan steps keep
    # their blocking semantics via ``run_skill_fn``.
    spawn_skill_fn: Callable[..., Awaitable[Any]] | None = None

    # RouterLoopHost reference for handlers that need duck-typed access
    # to host methods not covered by individual callable fields (=
    # MCP tools that already shipped with ``ctx.router_state`` treated as
    # host duck-type before Phase 3 step 2 introduced the typed sub-object).
    # When set, handlers may access ``rs.host.mcp_list_servers()`` etc.
    # directly.  Phase-side and test sites leave it None.
    host: Any = None

    # Memory tool callbacks (= for memory cluster handlers; Phase 3.5-B-heavy).
    # Bound by RouterLoop to its private ``_list_memory`` /
    # ``_read_memory_body`` / ``_remember`` / ``_forget`` helpers so
    # registry handlers consume the SAME parsed-index path the legacy
    # router branches used (= host.get_memory_index() routed through
    # the agent-aware session layer).  Without this, registry handlers
    # would read MEMORY.md from a path not aware of per-agent dirs.
    list_memory_fn: Callable[[str], list[Mapping[str, Any]]] | None = None
    read_memory_body_fn: Callable[[str, str], Awaitable[Any]] | None = None
    remember_fn: Callable[..., Awaitable[Any]] | None = None
    forget_fn: Callable[[str, str], Awaitable[Any]] | None = None

    # FP-0032: MCP server list for enum injection into call_mcp_tool /
    # describe_mcp_tool. Shape: [{name, description, tools?: [{name, ...}]}, ...]
    # Matches the ``mcp_servers`` arg passed to build_tools() and
    # build_system_prompt().  Populated by RouterLoop when building the
    # RouterCallerState for MCP tool dispatch.  When None, enum injection
    # is skipped and the schema falls back to plain string (graceful empty case).
    mcp_servers: list[Mapping[str, Any]] | None = None

    # FP-0034 Phase 2 prep: indexed RAG corpora snapshot for the universal
    # catalog ``rag.corpus`` category enumeration. Shape:
    # ``[{name, description, backend?, chunk_count?}, ...]``.
    # Populated by RouterLoop from ``SourceManifest.get_all()`` so
    # ``list_actions(category=["rag.corpus"])`` returns the configured
    # corpora as ``rag.corpus__<name>`` qualified names without round-
    # tripping the manifest file per invocation. ``None`` = router did
    # not provide a manifest snapshot (= test sites / plan-step hosts);
    # the catalog handler treats this identically to an empty list.
    available_rag_sources: list[Mapping[str, Any]] | None = None


@dataclass
class PhaseCallerState:
    """Per-protocol state that phase-style invocations need access to.

    Populated by ControlIRExecutor when invoking a ToolDefinition
    handler in phase context. Handlers that need phase-scoped
    resources (already-built OpContext, skill_run_id, run_visit_count,
    etc.) consume them via this object.

    All fields are Optional. Test contexts may populate only what's
    needed. M3 / M4 Phase 1 / 2 status: structure defined; production
    population (= control_ir_executor wiring) is M4 Phase 3.
    """
    # Phase identity
    skill_run_id: str | None = None
    phase_name: str | None = None
    run_visit_count: int | None = None

    # Pre-built OpContext (= for capabilities like mcp_call_tool that
    # currently hand-build OpContext; once phase-side dispatch
    # consumes the registry, OpContext is built once at the
    # dispatcher layer and passed through)
    op_context: Any = None

    # Workspace callbacks (= currently accessed via ctx.workspace
    # directly by memory tools; this field is for forward
    # consistency when ToolContext.workspace becomes a typed
    # interface vs the current loose Any)
    workspace_callbacks: Mapping[str, Callable[..., Awaitable[Any]]] | None = None


# ToolContext: protocol-agnostic execution context. Built by the
# dispatcher (router or phase) before invoking the handler.
# Universal fields: events / permission_resolver / workspace.
# Per-protocol-specific state can be accessed via caller_kind branching.
#
# M4 Phase 2: router_state and phase_state are now typed sub-objects
# (RouterCallerState / PhaseCallerState) instead of loose Any. Default
# to None when not relevant for the call site.
#
# Migration note: pre-Phase-2 code that wrote
# `ctx.router_state = some_dict` continues to work because
# RouterCallerState defaults all fields to None and the
# sub-objects are dataclasses (= structural compatibility for
# most read patterns is preserved). Test sites and handler
# sites should migrate to typed access.
@dataclass
class ToolContext:
    """Protocol-agnostic execution context (= ADR-0026 §2).

    Universal fields (events, permission_resolver, workspace) are
    populated regardless of protocol. caller_kind discriminates
    which sub-object holds protocol-specific state.

    M4 Phase 2: router_state and phase_state are now typed
    sub-objects (RouterCallerState / PhaseCallerState) instead of
    loose Any. Default to None when not relevant for the call site.
    """
    events: Any                                      # EventLog
    permission_resolver: Any | None                  # PermissionResolver
    workspace: Any                                   # Workspace
    caller_kind: Literal["router", "phase"]
    # Per-protocol-specific state (= ADR-0026 Open Question #3, resolved M4 Phase 2):
    # Typed sub-objects carry caller-kind-specific state.
    router_state: RouterCallerState | None = None    # populated for caller_kind="router"
    phase_state: PhaseCallerState | None = None      # populated for caller_kind="phase"


# ToolHandler: async callable signature.
# Returns canonical ToolResult; raises on error (dispatcher wraps).
class ToolHandler(Protocol):
    async def __call__(
        self,
        args: Mapping[str, Any],
        ctx: ToolContext,
    ) -> ToolResult: ...


@dataclass(frozen=True)
class ToolDefinition:
    """Single source of truth for a capability exposed to both
    router-style and phase-style LLM invocations.

    Per ADR-0026 §2. Held in a ToolRegistry; rendered to OpenAI tools[]
    via render_for_router(); rendered to Control IR
    available_control_ops via render_for_phase().
    """
    # Identity
    name: str                                        # canonical name (= ADR-0026 Open Question #6)
    description: str                                 # LLM-facing description
    parameters: Mapping[str, Any]                    # JSON schema (object root)

    # Gating
    gates: ToolGates

    # Implementation
    handler: ToolHandler

    # Metadata
    category: str                                    # = "io" / "discovery" / "memory" / etc.
    purity: Literal["pure", "side_effect", "read_only", "world_pure"] = "side_effect"
    dispatch_kind: Literal["sync", "async"] = "sync"  # async = result via deferred channel
                                                     # (= delegate_to_agent / plan;
                                                     # router-side only consideration)

    # Per-call schema enrichment hook (= ADR-0026 M4 Phase 3).
    # When set, callers (= build_tools / phase catalog builder) invoke
    # this hook AFTER render_for_router/render_for_phase to inject
    # per-session dynamic data into the schema (canonical example:
    # invoke_skill.name enum from available_skills, delegate_to_agent.to
    # enum from available_agents).
    #
    # Signature: (rendered_tool_dict, RouterCallerState) -> rendered_tool_dict
    #   - rendered_tool_dict: the dict produced by render_for_router /
    #     render_for_phase (= function/parameters/etc shape)
    #   - RouterCallerState: contains available_skills / available_agents
    #     and other per-session data the enricher may consult
    #   - returns: a NEW dict with dynamic enrichment applied (do NOT
    #     mutate the input; static schema is the canonical render)
    #
    # None (default) = static render is used as-is. This is the path
    # for the 24/26 capabilities whose schemas don't depend on per-session
    # data.
    schema_enricher: Any = None  # Callable[[dict, RouterCallerState], dict] | None

    # Future metadata anchors (commented out; surface as needed):
    # cost_weight: float = 1.0
    # rate_limit_class: str | None = None
    # log_redaction: tuple[str, ...] = ()

    # Protocol-specific renders
    def render_for_router(self, *, state: RouterCallerState | None = None) -> dict:
        """Render to OpenAI tools[] entry shape used by call_llm_tools.

        Identical structure to the existing ToolSpec.to_openai_dict().

        M4 Phase 3: when ``schema_enricher`` is set on the ToolDefinition AND
        ``state`` is provided, the static render is post-processed by the
        enricher to inject per-call dynamic data (e.g. invoke_skill.name enum
        from RouterCallerState.available_skills). When either is None (= 24/26
        capabilities, plus all callers that don't supply state), the static
        render is returned as-is.
        """
        rendered = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }
        if self.schema_enricher is not None and state is not None:
            rendered = self.schema_enricher(rendered, state)
        return rendered

    def render_for_phase(self) -> dict:
        """Render to a Control IR available_control_ops entry shape.

        Mirrors the structure that
        kernel/control_ir_executor.py::_build_phase_tool_catalog
        produces today. Phase-side dispatch uses this when constructing
        the phase context's available_control_ops list.
        """
        return {
            "kind": self.name,
            "description": self.description,
            "args_schema": dict(self.parameters),
            "purity": self.purity,
        }
