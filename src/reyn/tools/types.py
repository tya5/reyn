"""Type definitions for the unified tool registry (ADR-0026 M1/M4).

ToolDefinition is the single source of truth for a capability's
identity, metadata, gates, and handler. ToolGates encodes the
per-protocol allow/deny declaration. ToolContext is the
protocol-agnostic execution context handed to handlers; the router
dispatcher builds it before invocation. ToolHandler is the async
callable signature.

RouterCallerState is a typed sub-object replacing the loose Any type on
ToolContext.router_state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Mapping, Protocol

if TYPE_CHECKING:
    from reyn.data.skills.registry import SkillEntry


# ToolGates: per-protocol allow/deny gate at the registry level.
# This is Layer 1 of the 3-layer gate model (= ADR-0026 §3):
#   Layer 1: role gate (this dataclass)
#   Layer 2: phase narrowing (Phase.allowed_ops)
#   Layer 3: permission resolver (per-call runtime)
@dataclass(frozen=True)
class ToolGates:
    router: Literal["allow", "deny"] = "allow"
    phase:  Literal["allow", "deny"] = "allow"


# ToolResult: canonical result shape returned by handlers. The router
# dispatcher serializes this shape to a JSON string for tool_result
# content. Handler returns whatever Mapping[str, Any] makes semantic
# sense for the capability; the dispatcher does the shape adaptation.
ToolResult = Mapping[str, Any]


@dataclass
class RouterCallerState:
    """Per-protocol state that router-style invocations need access to.

    Populated by RouterLoop / dispatch_tool when invoking a
    ToolDefinition handler in router context. Handlers that need
    session-scoped resources (agent registry, MCP servers, etc.)
    or async-dispatch callbacks (send_to_agent)
    consume them via this object.

    All fields are Optional so test contexts can populate only the
    subset a given handler consumes.
    """
    # Catalog discovery (= for catalog tools list_agents / describe_agent handlers)
    agent_registry: Any = None
    available_agents: list[Mapping[str, Any]] | None = None

    # IS-1 (docs/proposals/reyn-pipeline-v0.9-design-resolutions.md R6): the
    # PipelineRegistry the run_pipeline tool looks up a registered Pipeline by
    # name in. Threaded explicitly (mirrors agent_registry above) rather than a
    # hidden global, since IS-1 registration is programmatic per-owner. None =
    # host doesn't support run_pipeline (surfacing to the live LLM catalog is
    # a later slice; this field exists so the handler + tests have a seam).
    pipeline_registry: Any = None

    # Async dispatch callbacks (= for delegate_to_agent / plan
    # handlers that need to interact with chain / task lifecycle)
    send_to_agent: Callable[..., Awaitable[Any]] | None = None

    # #2103 S1bc: session-spawn dispatch. The session_spawn handler spawns a
    # fresh-context session under the agent (rewind-tracked via session_spawned),
    # applies the per-session capability narrowing (S1a), and submits the task.
    # Bound by RouterLoop with chain_id pre-bound; None when the host doesn't
    # support session-spawn (= duck-typed / hasattr-guarded at caller-state build).
    spawn_session_fn: Callable[..., Awaitable[Any]] | None = None

    # #2103 B-tool: agent-spawn dispatch. The agent_spawn handler creates a new agent
    # under the spawner (rewind-tracked via agent_created carrying the OS-set parent
    # lineage), capped at ⊆ the spawner by construction (B-core), with an optional
    # restrict-only narrowing. None when the host doesn't support agent-spawn.
    spawn_agent_fn: Callable[..., Awaitable[Any]] | None = None

    # #2103 C1: topology-create dispatch. The topology_create handler wires the spawner's
    # spawn-subtree agents into a topology (routed through registry.create_topology — the
    # logged emit seam, WAL-tracked for rewind), restricting members to the creator's
    # subtree so profile bindings stay ⊆-creator by construction. None when the host
    # doesn't support topology-create.
    topology_create_fn: Callable[..., Awaitable[Any]] | None = None

    # Session-scoped chain identity (= for plan tool, delegate
    # tool, etc.)
    chain_id: str | None = None

    # Cost / model context (= for plan tool cost-aware decomposition)
    budget: Any = None                    # BudgetGateway instance
    router_model: str | None = None
    available_tool_names: list[str] | None = None

    # #1667: catalog categories to skip at the SOURCE (``_enumerate_category``),
    # so an excluded category vanishes UNIFORMLY from ``catalog_entries`` (every
    # scheme's flat list) + ``list_actions`` + dispatch — orthogonal to
    # ``exclude_tools`` (which filters top-level ``tools=`` by name and cannot
    # reach the universal catalog source). The task-agent / external-repo eval path
    # sets e.g. ``{"reyn_source"}``; the general/interactive agent leaves it empty.
    excluded_categories: frozenset[str] = frozenset()

    # Memory access (= for memory tools when invoked router-side;
    # router uses MemoryService directly, phase wraps via
    # ctx.workspace callbacks)
    memory_service: Any = None

    # Catalog access callbacks (= for catalog stub handlers
    # list_agents / describe_agent; RouterLoop populates with bound methods,
    # the stubs delegate to keep router/registry decoupled from RouterLoopHost type)
    list_agents_fn: Callable[[str], list[Mapping[str, Any]]] | None = None
    describe_agent_fn: Callable[[str], Mapping[str, Any]] | None = None

    # OpContext factory (= for file / mcp / web handlers that delegate
    # to op_runtime).  Bound by RouterLoop to ``host.make_router_op_context``
    # so handlers can build a permission-aware OpContext (= populated
    # PermissionDecl + Workspace + actor="chat_router") matching the
    # legacy router branch behavior.  When None, handlers fall back to
    # minimal OpContext synthesis (= test sites / phase-side).
    op_context_factory: Callable[[], Any] | None = None

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
    # ``Callable[..., Awaitable[Any]]`` to allow optional ``offset`` /
    # ``limit`` kwargs (= line-slice symmetry with ``read_file`` /
    # ``reyn_src_read``). Concrete signature is
    # ``(layer: str, slug: str, *, offset: int | None = None,
    # limit: int | None = None) -> Awaitable[dict]``.
    read_memory_body_fn: Callable[..., Awaitable[Any]] | None = None
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
    # catalog ``rag_corpus`` category enumeration. Shape:
    # ``[{name, description, backend?, chunk_count?}, ...]``.
    # Populated by RouterLoop from ``SourceManifest.get_all()`` so
    # ``list_actions(category=["rag_corpus"])`` returns the configured
    # corpora as ``rag_corpus__<name>`` qualified names without round-
    # tripping the manifest file per invocation. ``None`` = router did
    # not provide a manifest snapshot (= test sites / plan-step hosts);
    # the catalog handler treats this identically to an empty list.
    available_rag_sources: list[Mapping[str, Any]] | None = None

    # FP-0034 Phase 2 step 1: ActionEmbeddingIndex for the
    # ``search_actions`` semantic search wrapper.  RouterLoop owns the
    # session-scoped instance and triggers a background build when
    # ``action_retrieval.embedding_class`` is configured.  The
    # search_actions handler calls ``query()`` via this reference when
    # ``is_ready()`` returns True; otherwise it returns an empty result
    # so the LLM gracefully sees "no semantic results" rather than a
    # crash.  ``None`` = no index (= embedding not configured / fake
    # caller path); search_actions returns an empty result.
    action_embedding_index: Any = None

    # FP-0034 Phase 2 step 1: embedding provider + model class for
    # the search_actions query path.  RouterLoop binds these from the
    # session's EmbeddingProvider + the configured
    # ``action_retrieval.embedding_class`` so search_actions can embed
    # the user's query and rank against the index.  ``None`` = not
    # configured; handler returns an empty result.
    embedding_provider: Any = None
    embedding_model_class: str | None = None

    # FP-0034 Phase 2: sandbox backend name for the exec category
    # D14 visibility gate.  RouterLoop binds this from
    # ``session._sandbox_config.backend`` so ``list_actions(category=
    # ["exec"])`` returns ``exec__sandboxed_exec`` when a real backend
    # is configured (= not "noop" / not None).  ``None`` = sandbox not
    # configured or noop backend; exec category stays hidden.
    sandbox_backend: str | None = None

    # #2548 PR-A: skill registry snapshot — enabled skills available at
    # router construction time. Filtered to enabled=True by the
    # builder (build_skill_registry); only auto_invoke=True
    # entries are rendered into the L1 system-prompt ## Skills block.
    # None = not populated (test sites / contexts without a project
    # root). Construction-time only — per-turn hot-reload is a later PR.
    available_skills: "list[SkillEntry] | None" = None


# ToolContext: protocol-agnostic execution context. Built by the
# router dispatcher before invoking the handler.
# Universal fields: events / permission_resolver / workspace.
# Router-specific state is carried on the router_state sub-object.
@dataclass
class ToolContext:
    """Protocol-agnostic execution context (= ADR-0026 §2).

    Universal fields (events, permission_resolver, workspace) are
    populated regardless of caller. router_state carries the
    router-specific state sub-object.
    """
    events: Any                                      # EventLog
    permission_resolver: Any | None                  # PermissionResolver
    workspace: Any                                   # Workspace
    caller_kind: Literal["router"]                   # audit field emitted into tool_* events
    # Router-specific state sub-object.
    router_state: RouterCallerState | None = None    # populated for caller_kind="router"
    # #1673: the config-aware ModelResolver, threaded so tool handlers that spawn a
    # sub-run hand the spawned OpContext a REAL resolver + a config-following model
    # class instead of resolver=None + the literal "standard" (which litellm rejects
    # with BadRequestError — the latent bug).
    # Also completes #1672 CAT-3 (tool sub-runs follow model_class_by_purpose).
    resolver: Any | None = None                      # ModelResolver | None
    # #2073 S3: the CALLING session's HotReloader, so a self-reload tool
    # (hooks_add) requests a reload on THIS session's reloader (per-session
    # correctness in multi-agent — a process-wide global would reload the wrong
    # session). None in non-session/test contexts → the tool falls back to the
    # process-wide get_active_hot_reloader().
    hot_reloader: Any | None = None
    # #2259 PR-1: the process-shared WAL (StateLog), threaded from the calling
    # session so a recovery-core config tool handler (cron / hooks) — and the
    # OpContext it builds for an op handler (mcp_install / index_drop) — can record a
    # config GENERATION (keyed by the WAL head) after persisting its `.yaml`. None in
    # non-session / test contexts → the handler skips it (the opt-in contract).
    state_log: Any | None = None                     # StateLog | None


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
    """Single source of truth for a capability exposed to router-style
    LLM invocations.

    Per ADR-0026 §2. Held in a ToolRegistry; rendered to OpenAI tools[]
    via render_for_router(). render_for_phase() renders an op-spec shape
    retained only for the render-shape invariant tests (no production
    caller after the control-IR / phase-dispatch removal).
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
    # FP-0050 / #1822: tool self-declares that its result carries content from
    # OUTSIDE the trust boundary (external server / internet / user-written disk).
    # The content-threat guard fences (structurally marks as data) such results
    # at the tool-result chokepoint; trusted-internal results are scan-only.
    # P7: a generic OS-level bool the tool sets — not a hardcoded tool-name list.
    returns_external_content: bool = False

    # #2123: the tool declares it routes through the unified registry dispatch path
    # (RouterLoop._invoke_via_registry) — i.e. it belongs in REGISTRY_DISPATCH_TOOLS,
    # which is DERIVED from this flag (single SoT), not a hand-maintained frozenset.
    # The drift class this kills: a router-only tool advertised at build_tools but
    # missing from the dispatch set (→ "unhandled tool"), wired at one seam but not
    # the others (#2120 / #2122 / read_tool_result). The cross-seam guard asserts
    # every ADVERTISED bare router tool has this flag. P7: a generic OS-level bool the
    # tool sets — not a hardcoded tool-name list. False = dispatched elsewhere
    # (op-runtime / other path) or not router-dispatched.
    router_dispatched: bool = False

    # Per-call schema enrichment hook (= ADR-0026 M4 Phase 3).
    # When set, callers (= build_tools / phase catalog builder) invoke
    # this hook AFTER render_for_router/render_for_phase to inject
    # per-session dynamic data into the schema (canonical example:
    # delegate_to_agent.to enum from available_agents).
    #
    # Signature: (rendered_tool_dict, RouterCallerState) -> rendered_tool_dict
    #   - rendered_tool_dict: the dict produced by render_for_router /
    #     render_for_phase (= function/parameters/etc shape)
    #   - RouterCallerState: contains available_agents and other
    #     per-session data the enricher may consult
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
        enricher to inject per-call dynamic data (e.g. delegate_to_agent.to
        enum from RouterCallerState.available_agents). When either is None (= 24/26
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
        """Render to an op-spec entry shape (kind / description / args_schema / purity).

        No production caller remains (the phase-dispatch / control-IR
        executor path was removed); retained for the render-shape
        invariant tests. See PR note: candidate for follow-on removal
        alongside ToolGates.phase.
        """
        return {
            "kind": self.name,
            "description": self.description,
            "args_schema": dict(self.parameters),
            "purity": self.purity,
        }
