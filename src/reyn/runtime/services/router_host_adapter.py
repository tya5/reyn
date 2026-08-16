"""RouterHostAdapter — concrete RouterLoopHost implementation.

Extracted from Session wave 3 PR3. Composes Session's collaborators
(MemoryService, SnapshotJournal, op-runtime callbacks) so RouterLoop has no
direct dependency on Session internals. The adapter satisfies the
RouterLoopHost Protocol structurally; Session constructs one and exposes
it via `self._router_host`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from reyn.config.chat import TimeoutConfig
from reyn.core.events.events import EventLog
from reyn.runtime.services.mcp_cache_file import (
    ProbeOutcome,
    ToolsAnswered,
    ToolsUnknown,
    answered_only,
)
from reyn.runtime.session_pure import merge_memory_indexes
from reyn.runtime.turn_origin import TurnOrigin
from reyn.security.permissions.capability_profile import compose_narrowing_mappings

#: Sentinel for "the turn_budget engine has not been computed yet" — distinct
#: from a legitimate `None` result (a tiny-context model that genuinely
#: cannot support force-close, `try_build_default_turn_budget_engine`'s own
#: contract). #3671 follow-up: the engine used to be built EAGERLY at Session
#: construction, which forces `TurnBudgetEngine.__init__`'s litellm catalog
#: lookup (`get_max_input_tokens`) onto the TUI startup path — before any
#: turn has run and the value is needed. See `_ensure_turn_budget_engine`.
_TURN_BUDGET_ENGINE_UNSET = object()

# Proposal 0067 P4d (#3978): the LLM-facing default for
# run_prompt(collect="attached")'s ``timeout`` when the tool call omits it. Mirrors
# ``session_api._DEFAULT_AGENT_STEP_TIMEOUT_S`` (same 120s figure,
# duplicated rather than imported — that constant is module-private and this
# is a different layer's own default, not a re-export of session_api's).
_RUN_PROMPT_DEFAULT_TIMEOUT_S: float = 120.0

if TYPE_CHECKING:
    from reyn.runtime.router_op_context import RouterOpContextSource

logger = logging.getLogger(__name__)

# #3475: THE default for the MCP tools-list per-server probe timeout lives on
# ``TimeoutConfig.mcp_probe_seconds`` (the config definition side, per #3461's
# ``FileScopes`` precedent) — this module reads it rather than repeating the
# literal, so it and ``interfaces/cli/commands/mcp.py``'s ``_probe_server_tools``
# derive their own defaults from the SAME source instead of two independently
# hardcoded ``5.0`` literals drifting apart.
_DEFAULT_MCP_PROBE_SECONDS = TimeoutConfig().mcp_probe_seconds


@dataclass(frozen=True)
class McpGatewayInputs:
    """#3482: the 3 raw gateway-identity inputs (#3447) whose ONLY reader is
    :meth:`RouterHostAdapter._mcp_list_via_gateway` (measured on current
    main, 890e22d2 — all three read exclusively inside that one method's
    gateway construction call. (Wording note: keep the class name away from
    a following "(" here — the #2813 completeness scanner reads that pattern
    as a live construction site.) A real clustering by consumer set, not a
    name-prefix grouping: the three arrived together in #3447 (the Path A
    fold) and are carried together to the same one construction site.

    Pure value object, no default field values (a default would let a caller's
    silent omission absorb a wiring change unnoticed — the byte-identical
    -refactor invariant #3082 established for this bundle family) and no
    construction logic (assembling the values stays the CALLER's job, same as
    before the bundle existed). The rationale applies to every bundle below."""

    mcp_connection_service: Any
    mcp_agent_id: "str | None"
    ephemeral_fn: "Callable[[], bool] | None"


@dataclass(frozen=True)
class PutOutboxInputs:
    """#3482: the two params whose consumer set is EXACTLY
    ``{adapter.put_outbox, router_loop::run_loop}`` — the raw outbox put and
    the reply tracker the same method appends the emitted text to.

    ``append_history`` deliberately stays OUT: it is read by
    ``append_history_entry`` as well, so its consumer set is strictly larger
    and bundling it here would be a name/locality grouping, not a measured
    one. Pure value object — no defaults, no construction logic."""

    put_outbox: "Callable[..., Awaitable[None]]"
    agent_replies_tracker: "Callable[[], list[str] | None]"


@dataclass(frozen=True)
class LiveSessionIdInputs:
    """#3482: the two params whose consumer set is EXACTLY
    ``{adapter.live_session_id}`` — the construction-time sid and the live
    callback that supersedes it. The property picks between them, which is why
    neither is meaningful without the other.

    #3607: ``session_id`` used to have a second reader —
    ``make_router_op_context`` threaded the CONSTRUCTION-TIME value into every
    OpContext while this property preferred the live callback, so a spawned
    session's ops carried the stale id. The op-context supplier now reads the
    live callback, and this pair's consumer set is the one accessor again.

    Pure value object — no defaults, no construction logic."""

    session_id: "str | None"
    live_session_id_fn: "Callable[[], str | None] | None"


class RouterHostAdapter:
    """Concrete RouterLoopHost implementation extracted from Session.

    Holds injected identity attrs, catalogue deps, and async callbacks so
    RouterLoop can call host methods without importing or referencing
    Session directly.

    Parameters
    ----------
    agent_name:
        Identity attribute — forwarded as ``chat_id`` and ``agent_name``.
    agent_role:
        Identity attribute.
    output_language:
        BCP-47 code or None. Stored as a plain attribute (not property) per
        the RouterLoopHost Protocol.
    op_context_source:
        The session's :class:`~reyn.runtime.router_op_context.RouterOpContextSource`
        — the op-context capability itself, shared with ``Session`` rather than
        copied. #3607: the adapter used to receive 16 separate materials and
        assemble its own OpContext from them; a supplier is the finished thing,
        so there is nothing left to assemble differently from the other caller.
    permission_resolver:
        PermissionResolver instance (or None) for config-derived gates.
    mcp_servers:
        Raw MCP server config dict (may have ``{servers: {...}}`` wrapper).
    project_context:
        Project context text injected into the router system prompt.
    events:
        The session's EventLog — exposed as ``host.events``.
    resolver:
        ModelResolver instance for ``resolve_model``.
    memory:
        The session's :class:`MemoryService` — the memory-store capability
        (``remember`` / ``forget`` / ``read_body``), exposed whole as
        ``host.memory``. #3607: the adapter used to receive four file
        primitives (``file_read`` / ``file_write`` / ``file_delete`` /
        ``file_regenerate_index``) instead, whose ONLY consumers were three
        RouterLoop privates that re-implemented the memory operations —
        domain rules (threat scan, frontmatter, index regen, knowledge
        ingest) included — on top of them. The rules moved to MemoryService
        and the primitives left the adapter with them.
    journal:
        SnapshotJournal instance for plan-lifecycle persistence.
    agent_registry:
        AgentRegistry (or None) for listing reachable peers.
    pipeline_registry:
        PipelineRegistry (or None, IS-5) — the ``run_pipeline`` tool's
        lookup source, exposed via ``get_pipeline_registry()``.
    chains:
        ChainManager (or None) — proposal 0067 P4 (#3978)'s
        describe_task/list_tasks/cancel_task read/act against THIS
        session's own pending_chains, exposed via ``get_chains()``
        (mirrors ``get_pipeline_registry()``, same threading shape).
    agent_workspace_dir:
        Path to ``.reyn/agents/<agent_name>`` — used for ``get_memory_index``.
    mcp_call_tool:
        Async callback ``(server: str, tool: str, args: dict) -> dict``.
    mcp_read_resource:
        Async callback ``(server: str, uri: str) -> dict`` (#2597 slice ②a).
    mcp_subscribe_resource:
        Async callback ``(server: str, uri: str) -> dict`` (#2597 slice ②b).
    mcp_unsubscribe_resource:
        Async callback ``(server: str, uri: str) -> dict`` (#2597 slice ②b).
    mcp_get_prompt:
        Async callback ``(server: str, name: str, arguments: dict | None) -> dict``
        (#2597 slice ②c).
    mcp_gateway_inputs:
        :class:`McpGatewayInputs` — the 3 raw gateway-identity inputs (#3447:
        ``mcp_connection_service`` / ``mcp_agent_id`` / ``ephemeral_fn``)
        whose only reader is ``_mcp_list_via_gateway`` (#3482 bundle — a real
        consumer-set cluster, all three arrived together in #3447's Path A
        fold and are carried together to the same one construction site):

        - ``mcp_connection_service``: the session-owned ``MCPConnectionService``
          (or ``None``) — the adapter's OWN ``mcp_list_servers``/``mcp_list_tools``/
          ``mcp_list_resources``/``mcp_list_resource_templates``/``mcp_list_prompts``
          methods build their ``MCPGateway`` directly (no session callback for the
          5 listing methods — they never needed permission-gated ``execute_op``
          state, only the same server-config inputs the adapter already
          duplicates via ``_mcp_servers_flat``/``_get_mcp_servers_for_router``).
        - ``mcp_agent_id``: the session's ``agent_id`` — distinct from
          ``agent_name`` above; threaded raw (identity is immutable for the
          session lifetime) as the listing methods' gateway pool key (its
          ``agent_id=`` constructor kwarg; wording kept away from a literal
          "Gateway(" per the #2813 scanner note below).
        - ``ephemeral_fn``: zero-arg callable returning the LIVE
          ``Session._ephemeral`` flag — a callable, not a snapshot bool,
          because ``_ephemeral`` is reassigned post-construction by the
          registry / pipeline executor (spawn-time flip), mirroring
          ``live_session_id_fn``'s same staleness hazard. ``None`` (test
          construction) behaves as never-ephemeral.
    put_outbox_inputs:
        :class:`PutOutboxInputs` — the raw ``(OutboxMessage) -> None`` put plus
        the ``list[str] | None`` agent-replies tracker (#3482 bundle: exact
        consumer-set match on ``put_outbox``).
    live_session_id_inputs:
        :class:`LiveSessionIdInputs` — the construction-time ``session_id`` and
        the live ``() -> str | None`` callback that supersedes it (#3482
        bundle: exact consumer-set match on the ``live_session_id`` property).
    append_history:
        Sync callback ``(ChatMessage) -> None``.
    peek_mid_turn_injection:
        #3792. Async callback ``() -> dict | None`` —
        ``Session.peek_mid_turn_injection``. ``None`` default: adapters built
        without it behave like a host that never implemented the hook.
    commit_mid_turn_injection:
        #3792. Async callback ``(msg_id: str) -> None`` —
        ``Session.commit_mid_turn_injection``. Same ``None``-default contract
        as ``peek_mid_turn_injection``.
    mark_untrusted_in_flight:
        #4381 PR-2 stage ③. Sync callback ``() -> None`` —
        ``Session._mark_untrusted_in_flight``. Same ``None``-default contract
        as ``peek_mid_turn_injection``.
    """

    # RouterLoopHost Protocol attributes (non-property)
    output_language: str | None

    # A long flat kwarg list is not a Parameter-Object problem in general —
    # grouping by shape was measured as a near-no-op (#3121 took Session 54 ->
    # 45 by adding 4 objects while leaving 41 flat params in place): a
    # param-object split only pays for itself where a REAL consumer-set cluster
    # exists. #3482 bundles every cluster the measurement finds and nothing
    # else; the bare params that remain are bare because no OTHER param is
    # carried to exactly the same destinations.
    #
    # That last sentence is NOT recorded here as prose, because prose rots
    # unchecked: the first #3482 pass wrote 58 per-param "no shared-consumer
    # partner" reasons into a registry whose gate only verified the reasons
    # were non-empty, and 6 of them were measurably false. The predicate is
    # computable, so it is COMPUTED —
    # ``scripts/measure_router_host_adapter_consumers.py`` measures each param's
    # consumer set and ``tests/runtime/test_router_host_adapter_param_gate_3482.py``
    # fails the moment a bare param acquires an exact-match partner (it must
    # become a bundle) or a written claim contradicts the measurement. Only the
    # residue a measurement CANNOT settle is written down, in
    # ``ROUTER_HOST_ADAPTER_CONSUMER_UNMEASURED`` / ``_BUNDLE_BLOCKED`` below.
    def __init__(
        self,
        *,
        agent_name: str,
        agent_role: str,
        output_language: str | None,
        op_context_source: "RouterOpContextSource",
        permission_resolver: Any,               # PermissionResolver | None
        mcp_servers: dict | None,
        project_context: str,
        events: EventLog,
        resolver: Any,                          # ModelResolver
        memory: Any,                            # MemoryService
        journal: Any,                           # SnapshotJournal
        state_log: Any = None,                  # StateLog | None — #2248 PR-A2 (config emit)
        agent_registry: Any,                    # AgentRegistry | None
        pipeline_registry: Any = None,          # PipelineRegistry | None — IS-5
        chains: Any = None,                     # ChainManager | None — #3978 P4
        presentation_registry: Any = None,      # PresentationRegistry | None — FP-0054 PR-C
        # #4740: (agent_name, sid, task) — sid alone collides across agents
        # (see SpawnTracker's own constructor comment for the full defect).
        record_spawned_task: "Callable[[str, str, str], None] | None" = None,  # #2103 S1bc-exec
        agent_workspace_dir: Path,
        # MCP op callbacks
        mcp_call_tool: Callable[..., Awaitable[dict]],
        # #2597 slice ②a: resources consumption (read/templates) — defaults to
        # None so pre-existing hand-built adapters (tests, other call sites that
        # construct RouterHostAdapter without resources support) stay valid; the
        # mcp_verbs handlers getattr-guard before calling.
        mcp_read_resource: "Callable[..., Awaitable[dict]] | None" = None,
        # #2597 slice ②b: resource subscriptions — same None-default /
        # getattr-guard pattern as the ②a resources callbacks above.
        mcp_subscribe_resource: "Callable[..., Awaitable[dict]] | None" = None,
        mcp_unsubscribe_resource: "Callable[..., Awaitable[dict]] | None" = None,
        # #2597 slice ②c: prompt fetch — same None-default / getattr-guard
        # pattern as the ②a resources callbacks above.
        mcp_get_prompt: "Callable[..., Awaitable[dict]] | None" = None,
        # #3447: the 5 mcp_list_* callbacks (servers/tools/resources/
        # resource_templates/prompts) were folded onto the adapter itself —
        # see the class docstring's mcp_gateway_inputs entry. These 3 raw
        # inputs (bundled #3482) replace them.
        mcp_gateway_inputs: McpGatewayInputs,
        # Action callbacks — each bundle below is one measured consumer-set
        # cluster (#3482); ``append_history`` stays bare because its consumer
        # set is strictly larger than ``put_outbox``'s (append_history_entry
        # reads it too).
        put_outbox_inputs: PutOutboxInputs,
        append_history: Callable,
        # #3792: mid-turn CLIENT_INPUT injection — a peek/commit pair, both
        # bare (no shared-consumer partner: nothing else is carried to
        # exactly Session.peek_mid_turn_injection /
        # Session.commit_mid_turn_injection). None-default so pre-existing
        # hand-built adapters (tests, other call sites) stay valid; RouterLoop
        # getattr-guards both, so an adapter that leaves them None behaves
        # exactly like a phase host (no-op seam).
        peek_mid_turn_injection: "Callable[[], Awaitable[dict | None]] | None" = None,
        commit_mid_turn_injection: "Callable[[str], Awaitable[None]] | None" = None,
        # #4381 PR-2 stage ③: sync callback () -> None — Session.
        # _mark_untrusted_in_flight. Bare, no shared-consumer partner (mirrors
        # peek/commit_mid_turn_injection's own bare-ness above). None-default
        # so pre-existing hand-built adapters (tests, other call sites) stay
        # valid; router_loop.py getattr-guards the call, same as every other
        # optional host method.
        mark_untrusted_in_flight: "Callable[[], None] | None" = None,
        # Proposal 0067 P1' (#3978): mark_task_pending — bare, no shared-
        # consumer partner (same reasoning as the peek/commit pair above).
        # None-default so pre-existing hand-built adapters stay valid;
        # RouterLoopHost.mark_task_pending is unconditional (not
        # getattr-guarded) but a None here becomes a no-op lambda below,
        # not a missing attribute.
        mark_task_pending: "Callable[[], None] | None" = None,
        # #1953 dynamic-wire + #2103 S1bc-exec: the chat session identity
        # (``emit_hook_event`` builds the LLM's own ``llm:<session_id>:*``
        # namespace from it — never an op field the LLM could forge) and the
        # live callback that supersedes it for a spawned session. Bundled
        # #3482: the ``live_session_id`` property is the exact consumer of both.
        live_session_id_inputs: LiveSessionIdInputs,
        # FP-0034 PR-3b-iii/iv: universal catalog wrapper visibility
        # (= reyn.yaml tool_use.universal_wrappers_enabled — #4552 PR-3
        # moved this from action_retrieval.universal_wrappers_enabled).
        # #4159: no default — this param used to default to False while
        # ActionRetrievalConfig's own default is True (PR-3b-iv), a silent
        # implicit/config mismatch: the production call site (Session)
        # always passed it explicitly so the mismatch never fired, but any
        # OTHER construction path that forgot to thread it would silently
        # get False without ever knowing the config said True — a missing
        # kwarg raising a loud TypeError closes that class outright (every
        # caller must now say what it means), rather than defaulting to
        # either value and leaving a forgetful caller undetectable.
        universal_wrappers_enabled: bool,
        # FP-0034 Phase 2 step 1: ActionEmbeddingIndex + EmbeddingProvider
        # for search_actions.  When all three are set (= operator set
        # ``embedding.enabled: true`` (FP-0066 §7) AND Session built a
        # provider AND the index has been initialized), search_actions
        # appears in tools= and routes to the index.  When any is None
        # the wrapper stays out of tools= (= D14 visibility gate).
        action_embedding_index: Any = None,
        embedding_provider: Any = None,
        embedding_model_class: str | None = None,
        # FP-0063 PC: this session's BudgetGateway, threaded onto every router
        # OpContext so the `embed` op can record its INDEPENDENT embedding-cost
        # aggregate (session scope on the gateway itself; agent/project scope
        # via the process-shared tracker the gateway holds). THIS is the
        # op-context builder the router-dispatched `embed` TOOL resolves
        # (RouterCallerState.op_context_factory = host.make_router_op_context,
        # tools/types.py) — i.e. the live interactive path. Without it the
        # user-facing embed is billed but recorded nowhere ($0.00), the exact
        # bug FP-0063 closes.
        # FP-0034 Phase 2: sandbox backend name for exec D14 visibility
        # gate. Passed from ``session._sandbox_config.backend`` so the
        # universal catalog ``_enumerate_category("exec")`` can decide
        # whether to expose ``exec``. Default None hides
        # the exec category (= noop / no sandbox configured).
        sandbox_backend: str | None = None,
        # #187: the FS EnvironmentBackend INSTANCE (docker for in-container repos)
        # for the router OpContext Workspace. Distinct from ``sandbox_backend``
        # (a STRING for the exec D14 gate). Without this the LIVE file-op
        # dispatch built a host-cwd Workspace (the #187 wrong-FS defect: file
        # ops on the reyn repo, not /testbed).
        environment_backend: Any = None,
        # #2548 PR-A: enabled skill registry snapshot (list[SkillEntry]).
        # Session builds it via build_skill_registry(config.skills) and passes
        # it in; RouterLoop reads it via get_available_skills() to render the
        # ## Skills block. None → no skills (byte-identical to no-skills config).
        # NOTE (#3196 co-vet round 2): this field is later MUTATED in place
        # by ``CapabilityVisibility.reapply_skill_visibility`` to the
        # per-session VISIBILITY-FILTERED view (a UX menu concern — which
        # skills the operator chose to hide from `## Skills` / `skill_list`).
        # It is therefore the wrong source for a TRUST decision — the `file`
        # op's skill-load provenance gate reads the BASE set through the
        # op-context supplier's ``available_skills_fn`` instead (#3196 co-vet
        # round 2), so hiding a skill from the menu cannot change whether it
        # is trusted.
        available_skills: Any = None,
        # B25-S5-1: when True, RouterLoop awaits the action embedding index
        # build synchronously on the first turn before computing the D14
        # search_actions visibility gate. Off by default (= lazy bg build).
        eager_embedding_build: bool = False,
        # FP-0022 fix (#53): callable that yields an InterventionBus for
        # router-initiated tools that need the 4-layer approval flow
        # (web_fetch interactive prompt, mcp install / drop ask gates).
        # Session passes a factory that wraps ``ChatInterventionBus(
        # session, run_id=None, actor="chat_router")``; tests can
        # pass None and the OpContext gets ``intervention_bus=None`` (=
        # config-deny path still raises, interactive prompt path raises
        # the documented RuntimeError telling the caller a bus is needed).
        intervention_bus_factory: Callable[[], Any] | None = None,
        # #2175: the safety.on_limit checkpoint + the shared per-run extension dict —
        # injected from Session (mirror the inter_agent_messaging injection) so the spawn SEAM can
        # route a spawn-limit exceed through the same mode-driven on_limit framework as
        # max_agent_hops. None → no checkpoint wired (headless/test) → degrade to
        # unattended (reject), the C3 hard-deny posture.
        handle_chat_limit_checkpoint: "Callable[..., Any] | None" = None,
        safety_extensions: "dict[str, float] | None" = None,
        # #1652: ReasoningConfig (continuity/display/recent_turns) + the session
        # callback that renders the bounded prior-reasoning text section (reads
        # history + applies the continuity gate). None → reasoning disabled.
        reasoning_config: Any = None,
        reasoning_continuity_section_fn: "Callable[[], str] | None" = None,
        # #4206 slice 2: ③ preference-axis live override for `display` ONLY
        # (continuity/recent_turns stay ② bounding, read off
        # `reasoning_config` unchanged) — None (every pre-slice-2 caller,
        # every test host) falls back to the frozen `reasoning_config.display`
        # value, byte-identical to before this slice.
        reasoning_display_fn: "Callable[[], bool] | None" = None,
        # #4206 Slice B (#4724): ③ preference-axis live overrides for the 7
        # cost.*.warn_ratio keys — same callback shape as
        # reasoning_display_fn above. None (every pre-Slice-B caller, every
        # test host) means "no overrides", byte-identical to before Slice B.
        warn_ratio_overrides_fn: "Callable[[], dict[str, float]] | None" = None,
        # #4206 ②: bounding-axis live composed `model` ceiling — same
        # callback shape as warn_ratio_overrides_fn above. None (every
        # pre-② caller, every test host) falls back to the resolver's own
        # project-level ceiling, byte-identical to before this slice.
        model_class_ceiling_fn: "Callable[[], str | None] | None" = None,
        # Issue #383 PR-C: media + tool-result file storage.
        media_store: Any = None,
        # #1128 size axis: per-turn tool-result cap/offload callable. Takes the
        # serialised tool-result string and returns it unchanged (within cap) or
        # an offloaded bounded preview. ``None`` = no cap (identity).
        cap_tool_result: Any = None,
        # #272 media axis: callable (tool_content_str) -> int giving the tokens
        # left for the media follow-up after the (capped) tool text, so
        # router_loop bounds media materialisation. ``None`` = unbounded (pre-#272).
        media_followup_budget: Any = None,
        # tool-result-schema-redesign §5 / opt-in flip: static per-session flag
        # (not a callable — config doesn't change mid-session) gating
        # build_offload_body's structured inline-size gate. Default False = offload
        # off unless the operator opts in via ``offload.enabled: true``.
        offload_enabled: bool = False,
        offload_structured_inline_max_chars: int | None = None,
        offload_structured_preview_chars: int | None = None,
        # #272/#1128 context-size signal: callable () -> {free_window,
        # effective_trigger} (exact tokens) for the OS-injected SP header.
        # ``None`` = no signal rendered (e.g. test stubs).
        context_window_status: Any = None,
        # FP-0037 S1: persistent MCP tools cache directory.
        # Default is Path(".reyn/state") which resolves relative to cwd
        # (= the project root in all production entry points). Tests pass
        # a tmp_path subdirectory to isolate writes.
        state_dir: Path | None = None,
        # FP-0037 S2: project root for yaml mtime watch (3-scope cascade).
        # When None, only the user-global ~/.reyn/config.yaml is watched.
        # Session passes the project root so all 3 tiers are covered.
        project_root: Path | None = None,
        # #1092 PR-F1 (chat activation): builds the shared turn_budget engine
        # the chat axis budgets against. #3789 (#1172-safe): resolves its own
        # model directly (`self._resolver.resolve(self.model).model` in
        # Session), independent of CompactionEngine — the two used to share a
        # resolution path, and no longer do; see `docs/reference/runtime/
        # session-construction.md`'s compaction section for why. Sole
        # consumer (for now) is wrap_up_output_reserve
        # — which hard-caps the force-close wrap-up call's output. `None` = no
        # engine (legacy / test paths) → no cap (== pre-PR-F behaviour).
        # ADDITIVE: chat never calls _force_close_call until the F2 handoff
        # lands, so wiring the reserve here is inert until then.
        #
        # #3671 follow-up: a FACTORY, not the built engine — see
        # `_ensure_turn_budget_engine`. `try_build_default_turn_budget_engine`
        # (the production factory Session passes) touches litellm's model
        # catalog; calling it here at construction put that touch on the TUI
        # startup path even though the value is not needed until the first
        # force-close check, which only happens mid-turn.
        turn_budget_engine_factory: "Callable[[], Any] | None" = None,
        # #1468: cooperative turn-cancel signal. Session passes
        # self._is_turn_cancel_requested; test hosts pass None (= never cancel).
        # run_loop polls via getattr(host, "_is_turn_cancel_requested", None).
        turn_cancel_fn: "Callable[[], bool] | None" = None,
        # FP-0050 / #1822: content-threat scan + fence config. None (test hosts)
        # → defaults (disabled-safe via the methods' guards).
        threat_scan: Any = None,
        # #4215①: a lazy callable → this session's OWN per-(agent, sid) state
        # dir (the parent of `Session._snapshot_path`, #2285's "4th, most-
        # specific" hook layer — see `Session._read_per_session_hooks`). A
        # CALLABLE, not a `Path`, for the same reason `session_id_fn` above
        # is one: a spawned session's real snapshot path is assigned AFTER
        # this constructor runs (registry's spawn-time fixup), so an eager
        # read here would freeze the pre-spawn value. None (test hosts / the
        # legacy if/elif tool-dispatch tree) → `hooks_add`'s write-target
        # helper falls back to its pre-#4215 global-write behavior.
        session_state_dir_fn: "Callable[[], Path] | None" = None,
    ) -> None:
        self._op_ctx_source = op_context_source
        self._mcp_gateway = mcp_gateway_inputs
        self._put_outbox_in = put_outbox_inputs
        self._live_sid_in = live_session_id_inputs
        self._threat_scan = threat_scan
        self._turn_budget_engine_factory = turn_budget_engine_factory
        self._turn_budget_engine: Any = _TURN_BUDGET_ENGINE_UNSET
        self._turn_cancel_fn = turn_cancel_fn  # #1468
        self._session_state_dir_fn = session_state_dir_fn  # #4215①
        self._agent_name = agent_name
        self._agent_role = agent_role
        self.output_language = output_language
        self._perm = permission_resolver
        self._mcp_servers = mcp_servers
        # Lazy per-session cache for MCP tools — populated by
        # ensure_mcp_tools_cached() on the first user turn; None means
        # "not yet probed". See FP-0037 issue #160.
        #
        # #3520: the value type is ``ToolsAnswered``, never a bare list, and a
        # server whose probe did not answer is ABSENT rather than mapped to an
        # empty list. Both halves matter: the type keeps "measured zero tools"
        # apart from "not measured", and absence is what makes the unmeasured
        # server re-probed on the next turn instead of being frozen as a
        # capability the model is never told about.
        self._mcp_tools_cache: dict[str, ToolsAnswered] | None = None
        # FP-0037 S1: mtime of the cache file when we last loaded from it.
        # None = never loaded from disk. Used by maybe_reload_mcp_tools_cache_from_disk
        # to detect when the CLI has written a fresher version.
        self._mcp_tools_cache_mtime: float | None = None
        # FP-0037 S1: state dir for the persistent cache file.
        # #3705: was a MODULE-LEVEL constant frozen at import time (whatever
        # cwd happened to be at first `import router_host_adapter`, not even
        # fresh per construction) — now resolved per-instance at Path.cwd()
        # call time, matching the documented "resolves relative to cwd"
        # intent, at least for callers that don't pass state_dir at all
        # (Session now does — see `_reyn_state_root` — so production no
        # longer hits this fallback).
        self._state_dir: Path = (
            Path(state_dir) if state_dir is not None else Path.cwd() / ".reyn" / "state"
        )
        # FP-0037 S2: project root for yaml scope path resolution.
        # None = no project yaml tiers (user-global only).
        self._project_root: Path | None = (
            Path(project_root) if project_root is not None else None
        )
        # FP-0037 S2: last-seen mtimes for the 3 yaml scope tier files.
        # Keyed by Path; absent = never seen. Populated on first call to
        # maybe_refresh_mcp_tools_from_yaml; used to detect changes.
        self._yaml_mtimes_seen: dict[Path, float] = {}
        self._project_context = project_context
        self._events = events
        self._resolver = resolver
        self._memory = memory
        self._journal = journal
        self._state_log = state_log  # #2259 PR-1: WAL head for config generation emit
        self._registry = agent_registry
        self._pipeline_registry = pipeline_registry  # IS-5: run_pipeline lookup source
        self._chains = chains  # #3978 P4: describe_task/list_tasks/cancel_task lookup source
        # FP-0054 PR-C: operator named-template registry (presentations.yaml). Held on
        # the adapter (like _pipeline_registry) so make_router_op_context threads the
        # CURRENT snapshot into each OpContext; the pipelines/skills-style hot-reload
        # seam (Session._reapply_presentations) SWAPS this reference (dual-write with
        # the Session's copy) so a newly-registered template is visible next turn.
        self._presentation_registry = presentation_registry
        self._record_spawned_task = record_spawned_task   # #2103 S1bc-exec
        self._workspace_dir = Path(agent_workspace_dir)
        # MCP callbacks
        self._mcp_call_tool_cb = mcp_call_tool
        self._mcp_read_resource_cb = mcp_read_resource
        self._mcp_subscribe_resource_cb = mcp_subscribe_resource
        self._mcp_unsubscribe_resource_cb = mcp_unsubscribe_resource
        self._mcp_get_prompt_cb = mcp_get_prompt
        # #3447/#3482: raw inputs for the 5 mcp_list_* methods this adapter
        # implements directly (folded off Session — see class docstring),
        # bundled into mcp_gateway_inputs (single reader: _mcp_list_via_gateway).
        # Action callbacks — the outbox put lives on its #3482 bundle
        # (``self._put_outbox_in``); the ``put_outbox`` method reads it
        # there. ``send_to_agent`` (the RouterLoopHost protocol member +
        # this adapter's own implementation) retired in #4150 — zero
        # callers after P6 (#3978) removed the sole producer of the
        # closure that used to reach it (router_loop.py's
        # _send_to_agent_bound, removed in #4144). The LIVE peer-dispatch
        # transport is InterAgentMessaging.send_to_agent, reached via
        # Session._send_to_agent directly — this adapter is not on that
        # path.
        self._append_history_cb = append_history
        # #3792
        self._peek_mid_turn_injection_cb = peek_mid_turn_injection
        self._commit_mid_turn_injection_cb = commit_mid_turn_injection
        # #4381 PR-2 stage ③
        self._mark_untrusted_in_flight_cb = mark_untrusted_in_flight
        # Proposal 0067 P1' (#3978)
        self._mark_task_pending_cb = mark_task_pending
        # FP-0034 PR-3b-iii
        self._universal_wrappers_enabled = universal_wrappers_enabled
        # B25-S5-1
        self._eager_embedding_build = eager_embedding_build
        # FP-0034 Phase 2 step 1
        self._action_embedding_index = action_embedding_index
        self._embedding_provider = embedding_provider
        self._embedding_model_class = embedding_model_class
        # (#3607: every material the chat-router OpContext is built from lives
        # on ``self._op_ctx_source`` — the Session's ONE supplier, shared not
        # copied. Nothing on this adapter mirrors it.)
        # #2548 PR-A: enabled skill registry snapshot for the ## Skills block.
        self._available_skills = available_skills
        # FP-0034 Phase 2
        self._sandbox_backend = sandbox_backend
        self._environment_backend = environment_backend
        # FP-0022 fix (#53): intervention-bus factory used by
        # make_router_op_context to populate ``ctx.intervention_bus`` so
        # web_fetch / mcp install / mcp drop handlers can run their
        # interactive (Layer 4) approval flow without crashing on
        # ``intervention_bus is None`` defensive guards.
        self._intervention_bus_factory = intervention_bus_factory
        # #2175: spawn-limit on_limit checkpoint + the shared extension dict.
        self._handle_chat_limit_checkpoint = handle_chat_limit_checkpoint
        self._safety_extensions: "dict[str, float]" = (
            safety_extensions if safety_extensions is not None else {}
        )
        # #1652: reasoning capture/continuity/display config + the section renderer.
        self._reasoning_config = reasoning_config
        self._reasoning_continuity_section_fn = reasoning_continuity_section_fn
        # #4206 slice 2: ③ preference-axis live override for `display` ONLY.
        self._reasoning_display_fn = reasoning_display_fn
        # #4206 Slice B (#4724): ③ preference-axis live override for the
        # cost.*.warn_ratio keys.
        self._warn_ratio_overrides_fn = warn_ratio_overrides_fn
        # #4206 ②: bounding-axis live composed `model` ceiling.
        self._model_class_ceiling_fn = model_class_ceiling_fn
        # Issue #383 PR-C: store the MediaStore for path-ref save/read.
        self._media_store = media_store
        # #1128 size axis: per-turn tool-result cap/offload callable (or None).
        self._cap_tool_result = cap_tool_result
        # #272 media axis: per-turn media-budget provider (or None).
        self._media_followup_budget = media_followup_budget
        # tool-result-schema-redesign §5: structured-offload gate flag.
        self._offload_enabled = offload_enabled
        self._offload_structured_inline_max_chars = offload_structured_inline_max_chars
        self._offload_structured_preview_chars = offload_structured_preview_chars
        # #272/#1128 context-size signal: live budget provider (or None).
        self._context_window_status = context_window_status

    @property
    def wrap_up_output_reserve(self) -> int | None:
        """#1092 PR-F1: the force-close wrap-up call's OUTPUT budget
        (``output_reserve``), or None when the chat axis has no turn_budget engine.
        ``RouterLoop._force_close_call`` passes it as ``max_tokens`` to HARD-CAP the
        consolidation ≤ output_reserve — the by-construction guarantee that the
        re-injected handoff stays below threshold (``assert_turn_budget_bounds``,
        run at engine construction, enforces output_reserve + offload_cap <
        threshold). (Originally mirrored the phase host's wrap-up cap; that host —
        ``PhaseRouterLoopHost`` — was deleted in #2438.)

        NOTE — chat deliberately exposes ONLY this (the wrap-up cap), not
        ``should_force_close``: chat is REACTIVE-only. Unlike a phase (task
        execution — proactively force-closing to wrap-up-and-continue is correct
        because the phase has a goal), chat is a *live conversation* where a
        proactive mid-turn force-close would truncate the user's conversation
        prematurely. So chat handles growth via the bounded ``retry_loop`` shrink
        and force-closes only at the last-resort floor-exhausted terminal (the F2
        handoff). This is a deliberate per-axis architectural choice
        (failure-mode separation), NOT a missing proactive trigger."""
        engine = self._ensure_turn_budget_engine()
        return engine.budget.output_reserve if engine is not None else None

    def _ensure_turn_budget_engine(self) -> Any:
        """The turn_budget engine, computing it via the factory on first
        reference and caching the result (single owner, computed at most
        once — #3671 follow-up).

        A cached ``None`` (a tiny-context model that genuinely cannot
        support force-close, per ``try_build_default_turn_budget_engine``'s
        own contract) is a valid, permanent answer — distinguished from
        "not computed yet" by the ``_TURN_BUDGET_ENGINE_UNSET`` sentinel, not
        by ``None`` itself."""
        if self._turn_budget_engine is _TURN_BUDGET_ENGINE_UNSET:
            factory = self._turn_budget_engine_factory
            self._turn_budget_engine = factory() if factory is not None else None
        return self._turn_budget_engine

    def set_turn_budget_engine(self, engine: Any) -> None:
        """#1752: swap the chat turn_budget engine (rebuild-on-/model-switch).

        The engine bakes derived headroom (max_input + wrap-up-SP cost) for one
        (model, config) at construction. A /model override changes the context
        window, so the session rebuilds the engine for the new resolved model
        and rewires it here; ``wrap_up_output_reserve`` reads it fresh each turn,
        so the swap takes effect on the next turn. ``None`` (small-context model
        that cannot satisfy the force-close floor) keeps force-close inert.

        Sets the CACHED value directly (not the factory) — this always wins
        over the lazy factory: once called, :meth:`_ensure_turn_budget_engine`
        finds ``self._turn_budget_engine`` no longer ``_TURN_BUDGET_ENGINE_UNSET``
        and never invokes the factory (stale or not) again.
        """
        self._turn_budget_engine = engine

    # FP-0050 / #1822 S2: content-threat guard at the tool-result chokepoint.
    # scan_tool_result runs on the FULL content (before cap_tool_result truncates,
    # so injection can't hide past the size cap); fence_tool_result wraps the
    # (capped) content AFTER cap (so truncation can't sever the end marker), and
    # only for untrusted-source results (the feedback() caller gates on the
    # dispatch-set _external_source tag). Both are no-ops when threat_scan is
    # absent/disabled, and fail-open on scanner error.
    def scan_tool_result(self, content: str) -> None:
        from reyn.security.content_guard import scan_for_threats
        for m in scan_for_threats(content, self._threat_scan):
            self._events.emit(
                "threat_scan_match",
                pattern_id=m.pattern_id,
                severity=m.severity,
                scope=m.scope,
            )

    def fence_tool_result(self, content: str) -> str:
        from reyn.security.content_guard import fence_if_enabled
        return fence_if_enabled(content, self._threat_scan)

    # #3607: ``scan_for_block`` — the agent-write (memory) leg of the same
    # FP-0050 guard — moved to ``MemoryService.scan_for_block``. It had exactly
    # one caller, ``RouterLoop._remember``, and it is a rule ABOUT a memory
    # write, not about a tool result: it belongs with the operation it rejects.

    def _is_turn_cancel_requested(self) -> bool:
        """#1468: True when the session has requested a cooperative turn cancel.

        Polled by run_loop at the top of each iteration via
        ``getattr(host, "_is_turn_cancel_requested", None)``. Returns False
        when no ``turn_cancel_fn`` was wired (= test hosts / phase sub-hosts).
        """
        return bool(self._turn_cancel_fn and self._turn_cancel_fn())

    # --- RouterLoopHost identity attributes ---

    def cap_tool_result(self, content_str: str, *, content_type: "str | None" = None) -> str:
        """#1128 size axis: cap an oversized tool-result string at the
        router_loop chokepoint. Delegates to the session-supplied callable
        (which offloads the full body via the #385 store + returns a bounded
        plain-text preview); identity when no capper was wired.

        #2425 案B: caps the canonical ``text`` body (already the clean payload) — a single string,
        no clean-payload kwargs. ``content_type`` (#2663) is the canonical's renderer-only sidecar,
        forwarded to the session capper unchanged (identity path ignores it — nothing to store)."""
        if self._cap_tool_result is None:
            return content_str
        return self._cap_tool_result(content_str, content_type=content_type)

    def media_followup_budget(self, tool_content: str) -> int | None:
        """#272 media axis: tokens left for a tool turn's media follow-up after
        its (capped) text, or None when no media bound is wired (= pre-#272
        unbounded). router_loop passes this to the media-followup builder so
        overflow media stays a small lossless ref and the turn stays ≤ cap.
        """
        if self._media_followup_budget is None:
            return None
        return self._media_followup_budget(tool_content)

    @property
    def media_store(self) -> Any:
        """Issue #383 PR-C: expose the session's MediaStore so the
        RouterLoop's media-followup builder can materialise path-ref
        blocks at the wire boundary. ``None`` when no multimodal config
        was supplied (= legacy / test paths).
        """
        return self._media_store

    @property
    def offload_enabled(self) -> bool:
        """tool-result-schema-redesign §5 / opt-in flip: whether the
        structured-offload size gate is active. ``False`` (default) = offload
        off, structured data always inline; ``True`` = ``offload.enabled: true``
        opted in, normal offload behaviour.
        """
        return self._offload_enabled

    @property
    def offload_structured_inline_max_chars(self) -> "int | None":
        """Operator-tuned size at which a STRUCTURED result goes to its own ref
        (#3580, ``offload.structured_inline_max_chars``). ``None`` = the caller keeps
        the shipped default, so a host built without the field behaves as before."""
        return self._offload_structured_inline_max_chars

    @property
    def offload_structured_preview_chars(self) -> "int | None":
        """Operator-tuned amount of a structured result kept inline beside its ref
        (#3580, ``offload.structured_preview_chars``). ``None`` = shipped default."""
        return self._offload_structured_preview_chars

    @property
    def chat_id(self) -> str:
        """chat_id — same as agent_name per protocol convention."""
        return self._agent_name

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def agent_role(self) -> str:
        return self._agent_role

    @property
    def live_session_id(self) -> "str | None":
        """The owning session's LIVE sid (#2130 pattern: the constructor's cached
        ``session_id`` is stale for a spawned session — it is stamped
        post-construction, so the live fn wins when wired). IS-2 reads this as
        the ``run_pipeline_async`` reply address; the ``spawn_session``
        result-routing path reads the same expression."""
        live_fn = self._live_sid_in.live_session_id_fn
        return live_fn() if live_fn else self._live_sid_in.session_id

    @property
    def events(self) -> Any:
        """EventLog for dispatch_tool events."""
        return self._events

    @property
    def state_log(self) -> Any:
        """The process-shared WAL (StateLog) or None — #2259 PR-1: threaded into the
        ToolContext so a recovery-core config tool records a config generation."""
        return self._state_log

    @property
    def session_state_dir(self) -> "Path | None":
        """This session's OWN per-(agent, sid) state dir, or None — #4215①:
        threaded into the ToolContext so ``hooks_add`` can write to the
        session-local hooks layer instead of the global runtime layer.
        Calls the stored ``session_state_dir_fn`` LAZILY (not read at
        construction) — same reason ``live_session_id`` above defers to a
        fn: a spawned session's real value is assigned by the registry's
        spawn-time fixup, AFTER this adapter's constructor runs."""
        fn = self._session_state_dir_fn
        return fn() if fn is not None else None

    @property
    def permission_resolver(self) -> Any:
        """PermissionResolver bound at construction (= session's resolver).

        Exposed so ``RouterLoop._invoke_via_registry`` can populate the
        ToolContext.permission_resolver universal field via getattr; without
        this property the lookup falls through to None and every Tier-1
        config-deny check (web.fetch, mcp, …) silently bypasses for the
        router-invoked path. See #53 for the original silent-bypass bug.
        """
        return self._perm

    # --- Catalogue accessors ---

    def list_available_agents(self) -> list[dict]:
        """Return topology-reachable peers (PR11/PR12)."""
        if self._registry is not None:
            return list(self._registry.iter_reachable_agents(self._agent_name))
        return []

    def get_memory_index(self) -> dict:
        """Return merged shared + agent memory index."""
        # #3705: shared_path derived from `self._workspace_dir` (already
        # anchored on the caller's real state root) instead of a bare
        # relative `Path(".reyn")`, which silently ignored it — same
        # derivation as `MemoryService.memory_dir`'s "shared" branch.
        return merge_memory_indexes(
            shared_path=self._workspace_dir.parent.parent / "memory" / "MEMORY.md",
            agent_path=self._workspace_dir / "memory" / "MEMORY.md",
            agent_name=self._agent_name,
        )

    def get_file_permissions(self) -> dict | None:
        return self._get_file_permissions_for_router()

    def get_mcp_servers(self) -> list[dict]:
        return self._get_mcp_servers_for_router()

    def get_agent_registry(self) -> Any:
        """The real AgentRegistry (or None) — IS-5: exposes the private
        ``self._registry`` (already threaded in for peer-listing/delegation)
        through a public accessor so ``RouterLoop._build_router_caller_state``
        can populate ``RouterCallerState.agent_registry`` without reaching
        into the adapter's private attribute."""
        return self._registry

    def get_pipeline_registry(self) -> Any:
        """The real PipelineRegistry (or None) — IS-5: read by
        ``RouterLoop._build_router_caller_state`` to populate
        ``RouterCallerState.pipeline_registry`` so ``run_pipeline`` resolves
        against the session's actual registered pipelines instead of the
        None landmine."""
        return self._pipeline_registry

    def get_chains(self) -> Any:
        """The session's ChainManager (or None) — proposal 0067 P4 (#3978):
        read by ``build_resource_caller_state`` to populate
        ``RouterCallerState.chains`` so ``describe_task``/``list_tasks``/
        ``cancel_task`` act against THIS session's own pending_chains
        (mirrors ``get_pipeline_registry()``, same threading shape)."""
        return self._chains

    def get_inbox_depth(self) -> "int | None":
        """THIS session's current inbox queue depth (or None if unresolvable)
        — proposal 0067 P9 (#3978), architect ruling 2026-08-10: read by
        ``build_resource_caller_state`` to populate
        ``RouterCallerState.session_inbox_depth``.

        Resolves the live Session the SAME way ``send_to_session``'s caller
        resolution does (``self._registry.get_session(self._agent_name,
        caller_sid)``) rather than threading a NEW constructor param — no new
        wiring needed, ``self._registry``/``self._agent_name`` already exist
        for exactly this shape. Instantaneous read (``asyncio.Queue.qsize()``)
        — see ``RouterCallerState.session_inbox_depth``'s own docstring for
        the staleness caveat this value's field description must also carry.

        ⚠️ ``build_resource_caller_state`` calls this on EVERY router turn,
        unconditionally, for every host — unlike ``get_chains()`` (a stored
        attribute read, no live call), this makes a LIVE call on
        ``self._registry``. Falsify-verified regression (#4127 CI): a
        narrower test double implementing only PART of the registry
        interface (e.g. ``get_or_load``/``exists`` but no ``get_session`` —
        a legitimate, deliberately-minimal stub, not a bug in it) made this
        raise ``AttributeError`` mid-turn, which propagated far enough to
        derail an UNRELATED test's dispatch flow. ``getattr`` with a
        default, not a direct method call, so an incomplete registry
        degrades to ``None`` — the same tolerance ``get_mcp_servers()`` /
        other duck-typed accessors on this class already have — rather than
        breaking a turn that never asked for this observability field."""
        if self._registry is None:
            return None
        get_session = getattr(self._registry, "get_session", None)
        if get_session is None:
            return None
        sid = self.live_session_id or "main"
        session = get_session(self._agent_name, sid)
        if session is None:
            return None
        return session.inbox.qsize()

    def get_presentation_registry(self) -> Any:
        """The adapter's captured PresentationRegistry (or None) — FP-0054 PR-C.
        ``make_router_op_context`` reads it into each router OpContext's
        ``presentation_registry`` so a `present` op resolves a named ``template``.
        The public surface the hot-reload dual-write (``_reapply_presentations``)
        swaps and tests observe (mirrors ``get_pipeline_registry``)."""
        return self._presentation_registry

    def get_web_fetch_allowed(self) -> bool:
        """Always returns True — FP-0022: web_fetch is now always in the catalog.

        The catalog-level gate has been removed; authorization is enforced at the
        handler level via PermissionResolver.require_web_fetch() (4-layer approval:
        config / approvals.yaml / session / interactive).

        Method kept for backward compatibility with RouterLoopHost protocol.
        """
        return True

    def get_project_context(self) -> str:
        """Project context text: the project-wide file (REYN.md /
        ``project_context_path``) additively composed with this agent's own
        ``.reyn/agents/<agent_name>/AGENTS.md``, when either is present.

        Threaded into the router's system prompt so casual chat queries see
        the operator's project context. Empty string when neither side is
        configured.

        #4830 (owner ruling A, #4690's root cause): NOT fenced. FP-0050/#1822
        S4b originally fenced this (structurally marked untrusted data) +
        scanned it before it reached the SP §6 — but ``fence()``'s
        ``secrets.token_hex(8)`` marker id changes on every call, so the
        rendered text was never byte-identical across turns even when
        ``self._project_context`` itself never changed. Owner's own
        measurement on #4690: that marker sat at char 5,781 of the system
        prompt, splitting the prefix there and turning the ~230k chars
        after it into a cache miss on every single turn. project_context
        is operator/agent-editable content (REYN.md/AGENTS.md) — the same
        trust class CLAUDE.md already is for Claude Code, which renders it
        into the system prompt with no fence at all; the backstop there
        (and here) is the file-write permission gate, not a per-turn
        marker. Detection telemetry (``scan_tool_result``) stays — only
        the fence-wrapping is gone.

        #3787 (owner ruling B): the project-wide file stays exactly as
        above — read ONCE at session construction (``self._project_context``
        is frozen), never reloaded mid-session (owner: "project context path
        は起動時のみで hot 対応不要"). The per-agent file is different: this
        method reads ``self._workspace_dir / "AGENTS.md"`` FRESH on every
        call — no cache, no watcher-driven update needed here — because this
        method itself is already called fresh every turn (``router_loop.py``'s
        ``build_system_prompt(project_context=host.get_project_context())``
        and ``RouterHistoryBuffer.build_system_prompt``'s T_SP estimate both
        call it live, not from a memoized value), so a plain synchronous read
        IS the hot-reload — an edit to the agent's own file is reflected the
        very next turn with no additional wiring. (``ProjectContextWatcher``,
        constructed separately per agent in ``session.py``, still runs
        alongside this — its role is now purely the audit-event signal for
        "an edit was observed", not gating whether the reload happens.)
        Same trust class, same scan-not-fence treatment as the project side
        (owner: agent can write its own file; no dedicated op — controlled by
        sandbox policy's ``allow_write_paths``/``deny_write_paths``, which
        only narrows the write floor, never widens it — #3823/``infra.py``).

        Composition is additive, never a "winner": when both sides have
        content, each gets its own sub-heading so the model can tell which
        part it may edit (`write_file` under `.reyn/agents/<agent_name>/`)
        and which it may only read. When only one side has content, that
        content is returned bare (byte-identical to the pre-#3787 shape —
        the common case today, before any agent has written its own file).
        """
        # No .strip() on either side: the pre-#3787 code's own truthiness
        # check (`if not pc`) and return value both used the raw string
        # unmodified — stripping here would silently change what an
        # operator's REYN.md (a real file read, routinely trailing a
        # newline) renders in the SP, breaking the byte-identical claim
        # this docstring makes for the single-side case.
        pc = self._project_context or ""
        if pc:
            self.scan_tool_result(pc)  # detection telemetry (scan-all parity)
        agent_pc = self._read_agent_instructions()
        if agent_pc:
            self.scan_tool_result(agent_pc)  # same telemetry, agent-authored half
        if pc and agent_pc:
            return (
                "### Project\n\n" + pc + "\n\n"
                "### Agent instructions (this agent only)\n\n" + agent_pc
            )
        return pc or agent_pc

    def _read_agent_instructions(self) -> str:
        """#3787: this agent's own ``.reyn/agents/<agent_name>/AGENTS.md`` —
        a fresh, synchronous read on every call (see :meth:`get_project_context`'s
        docstring for why no caching is needed). ``""`` when absent — never
        raises (a missing/unreadable file is "nothing configured", not an
        error, matching every other best-effort per-agent file read in this
        codebase, e.g. ``Session._read_per_agent_composers``)."""
        path = self._workspace_dir / "AGENTS.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def get_cwd(self) -> str:
        """Agent-visible working directory for the SP Environment section.

        Backend-aware: when an environment backend is configured (e.g.
        DockerEnvironmentBackend), the agent sees the in-container path
        (backend.repo_dir) rather than the host cwd — these diverge when
        the repo is mounted inside a container at a different path. Without
        this fix the SP shows the host path but the actual FS/exec ops run
        against the container repo_dir (frame mismatch + host path leak).

        Resolution order (getattr-guarded for forward compat):
        1. backend.repo_dir      — ContainerBackend (e.g. DockerEnvironmentBackend)
        2. workspace.base_dir    — HostBackend or no backend (#4204 bucket D)
        3. os.getcwd()           — defensive fallback if base_dir is unresolvable

        #4204 bucket D: step 2 used to be a bare ``os.getcwd()`` — but the
        REAL host-side exec op (``sandboxed_exec``, see
        ``op_runtime/sandboxed_exec.py``) anchors its subprocess's cwd on
        ``ctx.workspace.base_dir``, not the raw process cwd (measured by
        reading that op's own source directly). These diverge exactly when
        reyn is launched from a subdirectory of the project (this issue's
        condition ①) — the SP told the agent one cwd while every real exec
        op ran somewhere else, with no way for the agent to detect the
        mismatch. ``self._op_ctx_source``'s ``workspace_base_dir_fn``
        supplier is the SAME source ``build_router_op_context`` reads for
        the real ``OpContext.workspace.base_dir`` value (one supplier, one
        answer — see ``RouterOpContextSource``'s own docstring for why a
        supplier, not a snapshot, matters here: a spawned session's real
        base_dir is fixed up AFTER construction).
        """
        import os
        repo_dir = getattr(self._environment_backend, "repo_dir", None)
        if repo_dir:
            return str(repo_dir)
        base_dir_fn = getattr(self._op_ctx_source, "_workspace_base_dir_fn", None)
        if base_dir_fn is not None:
            base_dir = base_dir_fn()
            if base_dir:
                return str(base_dir)
        return os.getcwd()

    def get_environment_info(self) -> dict:
        """System metadata for the SP Environment section (#1479).

        Always returns:
          - ``date``       — today ISO-8601 (host-clock, universal)

        Returns additionally when the backend is absent or is a HostBackend
        (no ``repo_dir`` — same marker as #1477):
          - ``platform``   — OS family lower-cased ("linux", "darwin", …)
          - ``os_version`` — kernel/OS release string
          - ``shell``      — default shell executable
          - ``is_git_repo`` — bool; True when a .git entry exists at cwd

        When a non-host backend is present (``repo_dir`` set = container):
          - If backend implements ``get_environment_info()`` → use those values
          - If not implemented → omit platform/os_version/shell/is_git_repo
            (degrade, don't guess — returning host darwin/zsh for a linux
            container would repeat the #1477 host-value-leak pattern)

        Container probe (#1481): ``DockerEnvironmentBackend.get_environment_info``
        collects platform/os_version/shell/is_git_repo from INSIDE the container.
        The omission semantics above still hold when a probe sub-field fails.
        """
        import datetime
        import os
        import platform as _platform
        from pathlib import Path

        backend = self._environment_backend
        result: dict = {"date": datetime.date.today().isoformat()}

        # Determine whether this is a non-host (container) backend.
        # Same marker as get_cwd() (#1477): presence of repo_dir signals
        # a container backend whose agent-visible environment differs from host.
        _is_non_host_backend = bool(getattr(backend, "repo_dir", None))

        if _is_non_host_backend:
            # Non-host backend: only use values the backend explicitly provides.
            # If it doesn't implement get_environment_info(), omit all host-derived
            # fields — showing host platform/shell for a container = wrong context.
            _info_fn = getattr(backend, "get_environment_info", None)
            if callable(_info_fn):
                try:
                    backend_info = _info_fn() or {}
                except Exception:
                    backend_info = {}
                result["platform"] = backend_info.get("platform", "")
                result["os_version"] = backend_info.get("os_version", "")
                if backend_info.get("shell"):
                    result["shell"] = backend_info["shell"]
                # #1481: is_git_repo from the IN-CONTAINER probe — NOT a host-path
                # check. ``get_cwd()`` returns the container ``repo_dir``, so
                # ``(repo_dir / ".git").exists()`` on the host tests the wrong (or
                # absent) path — a #1477-class host/container frame mismatch. Use
                # the backend's value; omit when the probe didn't supply it.
                if "is_git_repo" in backend_info:
                    result["is_git_repo"] = bool(backend_info["is_git_repo"])
            # else: non-host backend without probe → omit platform/shell/git
        else:
            # Host backend or no backend: derive from local environment.
            result["platform"] = _platform.system().lower()
            result["os_version"] = _platform.release()
            _shell = os.environ.get("SHELL", "")
            if _shell:
                result["shell"] = _shell
            cwd_path = Path(self.get_cwd())
            result["is_git_repo"] = (cwd_path / ".git").exists()

        return result

    def get_universal_wrappers_enabled(self) -> bool:
        """Return whether FP-0034 universal catalog wrappers are enabled.

        Mirror of the ``tool_use.universal_wrappers_enabled`` flag from
        reyn.yaml (#4552 PR-3: moved from
        ``action_retrieval.universal_wrappers_enabled``). RouterLoop calls
        this when building tools= so the 4 wrappers (list_actions /
        describe_action / invoke_action; search_actions gated separately
        by §D14) appear in the LLM's function-calling catalog. Default
        False preserves the prior tools= shape.
        """
        return self._universal_wrappers_enabled

    def get_action_embedding_index(self) -> Any:
        """Return the ActionEmbeddingIndex instance, or None.

        FP-0034 Phase 2 step 1.  Bound by Session when the operator
        has set ``embedding.enabled: true`` (FP-0066 §7).  RouterLoop
        forwards into ``RouterCallerState.action_embedding_index`` so
        the ``search_actions`` handler can call ``query()``.
        """
        return self._action_embedding_index

    def get_embedding_provider(self) -> Any:
        """Return the session's EmbeddingProvider instance, or None.

        FP-0034 Phase 2 step 1.  Used together with
        ``get_action_embedding_index()`` to power search_actions.
        """
        return self._embedding_provider

    def get_embedding_model_class(self) -> str | None:
        """Return the configured embedding model class name, or None.

        FP-0034 Phase 2 step 1.  Mirror of
        ``embedding.default_class`` from reyn.yaml (bound only when
        ``embedding.enabled: true`` — FP-0066 §7).  Used by
        ``RouterLoop._build_router_caller_state`` to bind the
        ``embedding_model_class`` field on ``RouterCallerState``.
        """
        return self._embedding_model_class

    def get_eager_embedding_build(self) -> bool:
        """Return True if RouterLoop should await the action embedding
        index build synchronously before computing the search_actions
        visibility gate on the first turn.

        B25-S5-1 fix for the cold-start race where ``is_ready()`` is False
        on Turn 1 because the background build hasn't finished, hiding
        ``search_actions`` from the LLM and inviting tool-name
        hallucinations (= B24 dogfood evidence: 2/3 hallucinated calls).
        Default False preserves the prior lazy background-build behavior.
        """
        return self._eager_embedding_build

    def get_sandbox_backend(self) -> str | None:
        """Return the configured sandbox backend name, or None.

        FP-0034 Phase 2.  Mirror of ``sandbox.backend`` from reyn.yaml
        (resolved via ``session._sandbox_config.backend``).  RouterLoop
        forwards this into ``RouterCallerState.sandbox_backend`` so the
        exec category D14 visibility gate in
        ``universal_catalog._enumerate_category`` can decide whether to
        expose ``exec``.  ``None`` and ``"noop"`` both
        hide the exec category; any other value (``"seatbelt"`` /
        ``"landlock"`` / ``"auto"``) makes it visible.
        """
        return self._sandbox_backend

    def get_available_skills(self) -> Any:
        """Return the enabled skill registry snapshot (list[SkillEntry]) or None.

        #2548 PR-A.  Mirror of ``skills.entries`` from the config cascade,
        built by ``build_skill_registry(config.skills)`` at Session
        construction and filtered to ``enabled=True``.  RouterLoop reads this
        (via the scheme ``layer_ctx`` and ``RouterCallerState.available_skills``)
        to render the ``## Skills`` block in the system prompt.  ``None`` (or an
        empty list) → no Skills section.
        """
        return self._available_skills

    # --- Web ops ---

    async def web_search(self, *, query: str, max_results: int) -> dict:
        """Dispatch the OS-native web/search op (DuckDuckGo) from the router."""
        from reyn.core.op_runtime.web import handle_web_search
        from reyn.schemas.models import WebSearchIROp

        op = WebSearchIROp(
            kind="web_search",
            query=query,
            max_results=max_results,
            backend="duckduckgo",
        )
        ctx = self.make_router_op_context()
        return await handle_web_search(op, ctx)

    async def web_fetch(self, *, url: str) -> dict:
        """Dispatch the OS-native web/fetch op from the router.

        FP-0022: authorization is now enforced at the handler level via
        PermissionResolver.require_web_fetch() inside handle_web_fetch().
        """
        from reyn.core.op_runtime.web import handle_web_fetch
        from reyn.schemas.models import WebFetchIROp

        op = WebFetchIROp(
            kind="web_fetch",
            url=url,
            timeout=15.0,
        )
        ctx = self.make_router_op_context()
        return await handle_web_fetch(op, ctx)

    async def reyn_repo_list(self, *, path: str) -> dict:
        """List entries under ``<reyn_root>/path``.

        See :func:`_resolve_reyn_root` for root resolution and
        :func:`_safe_resolve_inside` for path-traversal protection.
        Returns ``{path, entries: [{name, type}]}`` on success or
        ``{error}`` on failure.
        """
        from reyn.runtime.reyn_repo import (
            list_entries,
            resolve_reyn_root,
            safe_resolve_inside,
        )
        try:
            root = resolve_reyn_root()
        except RuntimeError as exc:
            return {"error": str(exc)}
        try:
            target = safe_resolve_inside(root, path)
        except ValueError as exc:
            return {"error": str(exc)}
        return list_entries(root, target, path)

    async def reyn_repo_read(self, *, path: str) -> dict:
        """Read text at ``<reyn_root>/path``."""
        from reyn.runtime.reyn_repo import (
            read_text,
            resolve_reyn_root,
            safe_resolve_inside,
        )
        try:
            root = resolve_reyn_root()
        except RuntimeError as exc:
            return {"error": str(exc)}
        try:
            target = safe_resolve_inside(root, path)
        except ValueError as exc:
            return {"error": str(exc)}
        return read_text(target, path)

    # --- Memory capability ---

    @property
    def memory(self) -> Any:
        """The session's :class:`MemoryService` — handed to the router whole.

        #3607: the two path delegates that used to live here
        (``memory_path`` / ``memory_dir``) existed so RouterLoop could
        assemble memory file paths itself. It no longer assembles anything:
        it calls ``host.memory.remember`` / ``.forget`` / ``.read_body``.
        """
        return self._memory

    # --- Action callbacks ---

    def mark_task_pending(self) -> None:
        """Proposal 0067 P1' (#3978): forward to ``Session.current_task``
        when wired. None-safe (same reasoning as
        :meth:`peek_mid_turn_injection`) — an adapter built without the
        callback (pre-#3978 test construction) is a no-op, matching what a
        host that never implemented the concept would do."""
        if self._mark_task_pending_cb is None:
            return
        self._mark_task_pending_cb()

    async def spawn_session(self, *, request: str, mode: str,
                            narrowing: "dict | None", chain_id: str,
                            base_dir: "str | None" = None,
                            agent: "str | None" = None,
                            session: "str | None" = None) -> dict:
        """#2103 S1bc: spawn a fresh-context SESSION under THIS agent for a task.

        Spawns + records the session (rewind-tracked via ``session_spawned`` +
        per-session capability narrowing, the action-layer ``spawn_session_recorded``
        seam), starts its run-loop (FP-0043 4b-2 ``ensure_session_running``), and
        submits the task to it — the spawned session RUNS the task in isolation. The
        result stays in the session; routing it BACK to the spawner is the S1bc-exec
        follow-on (FP-0043 Stage-4 non-main routing), so this is async-dispatch posture
        (returns a spawn-ack).

        ``agent`` (#4556, optional): target a specific agent for the new session
        instead of always spawning under ``self._agent_name``. Restrict-only, the
        SAME forge-guard shape ``create_topology`` uses for its members
        (``is_spawn_descendant`` — self is always allowed, since the predicate
        is reflexive): the target must be the caller itself or a (transitive)
        spawn-descendant of it, never an arbitrary peer or ancestor. An unknown
        agent name or a target outside the caller's own spawn subtree is a typed
        error response, never a raised exception.

        ``session`` (#4556, optional): a caller-chosen session id, threaded
        through to ``spawn_session_recorded``'s new ``sid`` parameter instead of
        letting one auto-generate. A duplicate id for the target agent is a typed
        error response (the registry's own ``ValueError`` reshaped here — never
        propagated raw), not silently overwritten."""
        if self._registry is None:
            raise RuntimeError(
                "spawn_session requires a registry (multi-session host) — unavailable "
                "in this context."
            )
        # #4556: resolve + forge-guard the target agent BEFORE any other work below
        # (nesting-depth checkpoint, narrowing composition, base_dir validation) —
        # a bad ``agent`` argument should short-circuit cheaply, not spend an
        # operator checkpoint round-trip on a request that is going to be refused
        # anyway. ``is_spawn_descendant(x, x) == True`` (reflexive), so omitting
        # ``agent`` (target == self._agent_name) never even reaches the guard.
        target_agent = agent if agent is not None else self._agent_name
        if agent is not None and agent != self._agent_name:
            if not self._registry.exists(target_agent):
                return {
                    "status": "error", "kind": "agent_not_found",
                    "error": f"agent {target_agent!r} does not exist.",
                }
            if not self._registry.is_spawn_descendant(target_agent, self._agent_name):
                return {
                    "status": "error", "kind": "agent_outside_subtree",
                    "error": (
                        f"agent {target_agent!r} is not in your own spawn subtree — "
                        "restrict-only: spawn_session's optional agent argument can "
                        "only target yourself or an agent you (transitively) spawned "
                        "via spawn_agent."
                    ),
                }
        # #2130: the spawning session's LIVE sid — threaded as ``from_sid`` so the spawned
        # session's result routes back to THIS specific (agent, sid), not the agent's main
        # session. Reading the LIVE sid (the constructor's cached session_id is stale for a
        # spawned session, stamped post-construction). This lifts the #2103 S1bc-exec
        # non-main-spawn guard: a non-main session may now spawn — its result routes back
        # correctly by (agent, from_sid). (None / "main" → main-case, byte-identical.)
        from_sid = self.live_session_id
        # #2708 P3-item3 (co-vet must-fix): the LLM spawn_session tool spawns a background sub-agent
        # that is NOT self-attachable by a human — it is LLM-initiated, so no operator is guaranteed
        # to know the child sid to attach + drain (unlike /session new, which a human starts + attaches
        # to). A self-bound (ReviewedNA) child would hit the SAME origin-pin park/hang this PR closes:
        # its ask_user stamps "tui", no listener → InterventionCoordinator.dispatch parks forever. So
        # the child BRIDGES to its spawning PARENT (BridgeToParent): the spawner is a live session whose
        # own routing already decides where user-reaching capabilities go — a delegated sub-agent's
        # ask_user reaches the parent's operator (parent attached) by construction, exactly like the
        # attached pipeline driver ("delegated-work-can-ask"). A parent that cannot be resolved (should
        # not happen — this adapter runs FOR that session) falls back to AuditOnlyNoSurface (a typed
        # refusal), never a hang.
        from reyn.runtime.spawn_routing import AuditOnlyNoSurface, BridgeToParent
        parent_session = self._registry.get_session(self._agent_name, from_sid)
        _routing = (
            BridgeToParent(parent_session)
            if parent_session is not None
            else AuditOnlyNoSurface()
        )
        # #2737: spawn_session NESTING depth cap — PARITY with spawn_agent's max_spawn_depth
        # gate (this method's sibling ``spawn_agent`` below). The LLM spawn_session path
        # re-exposes spawn_session on EVERY spawned child's router host, so unbounded
        # grandchildren/great-grandchildren are reachable; and the compositional
        # ``SpawnBridgeInterventionListener.bus()`` walk (#2735) recurses once per nesting
        # level to resolve ask_user toward the root operator, so a deep chain risks a
        # ``RecursionError`` during ask_user resolution. The SAME operator base cap
        # (``safety.spawn.max_depth`` = ``registry.max_spawn_depth``) bounds BOTH by
        # construction (capped nesting depth ⇒ bounded bus() recursion, since the depth is
        # exactly that walk's length). Routed through the SAME on_limit checkpoint and the
        # SAME typed ``spawn_limit_exceeded`` error as spawn_agent (uniform sibling), with a
        # SEPARATE extension key — session nesting ≠ agent-tree depth, so an approved widen of
        # one must not silently widen the other (the #2175 approval-scoping principle).
        base_depth = self._registry.max_spawn_depth
        if base_depth:
            cur_depth = self._registry.session_nesting_depth(self._agent_name, from_sid)
            eff_depth = base_depth + int(
                self._safety_extensions.get(f"max_session_depth:{self._agent_name}", 0.0)
            )
            if cur_depth + 1 > eff_depth:
                decision = await self._spawn_limit_checkpoint(
                    kind=f"max_session_depth:{self._agent_name}",
                    prompt=(
                        f"Session-spawn nesting depth {cur_depth + 1} would exceed the "
                        f"session-nesting cap ({eff_depth}). Allow agent "
                        f"{self._agent_name!r} to nest spawned sessions deeper?"
                    ),
                    detail=(
                        f"agent={self._agent_name} sid={from_sid} depth={cur_depth + 1} "
                        f"cap={eff_depth}"
                    ),
                    extension_amount=1.0,
                    run_id=self._agent_name,
                )
                if not decision.allow_continue:
                    return {
                        "status": "error", "kind": "spawn_limit_exceeded",
                        "error": (
                            f"spawn-limit: session-nesting depth {cur_depth + 1} would "
                            f"exceed the session-nesting cap ({eff_depth}) (agent "
                            f"{self._agent_name!r} at session-nesting depth {cur_depth}). "
                            "→ Extend just this axis via the operator spawn-limit "
                            "checkpoint (raises max_session_depth for this agent), or "
                            "raise the shared base safety.spawn.max_depth (which lifts "
                            "BOTH the agent-tree and session-nesting caps)."
                        ),
                    }
        # #3556: the spawner's OWN sid-keyed #2103-S1a narrowing composes IN. ``narrowing``
        # arrives here as a ``spawn_session`` tool argument — i.e. whatever THIS session's
        # LLM asked for — so without this line the spawned sibling is born under the LLM's
        # requested envelope alone, and a narrowed session widens itself by spawning. The
        # tool's own parameter description promises the model the opposite ("restrict-only,
        # cannot widen your envelope", ``tools/descriptions/delegation.py``), which is the
        # contract this restores rather than a new control. Same composition as #3553 and
        # for the same reason (``narrowing`` sits where ``capabilities`` sits there: an
        # argument the spawner imposes on the child): deny keys union, allow keys intersect,
        # an absent allow key is ⊤. The name-keyed layers (the agent's ``permissions``
        # declaration, topology ``capability_profile`` bindings, the #2081 ``_delegate``
        # floor) need nothing passed — the child shares this agent's identity and
        # ``resolved_profile_for`` re-derives them; the #2285 ``/visibility`` toggle and the
        # #1827-S4b ephemeral untrusted-context narrowing are NOT carried, identically to
        # the two sibling sites (#3546 module docstring, layers 3 and 4).
        narrowing = compose_narrowing_mappings(
            self._registry.per_session_narrowing(self._agent_name, from_sid), narrowing,
        )
        # #4200 2/2: restrict-only ``base_dir`` — LLM-authored (this session's own
        # ``spawn_session`` tool argument), so validated against THIS spawner's own
        # EFFECTIVE base_dir (parent_session._workspace_base_dir — #4200 1/2's own
        # resolved value, not the Agent's bare default) BEFORE the child's
        # config.yaml is written. Same restrict-only SHAPE as ``narrowing`` above,
        # but a subtree-containment check rather than a ∩-composition (base_dir is a
        # scalar, not a set). #4179 lesson: REJECT a request outside the floor
        # (never silently clamp it in), naming the actual boundary in the message.
        #
        # ⚠️ NOT a system-wide invariant: this check gates only the LLM-authored
        # spawn_session ARGUMENT. An OPERATOR directly hand-editing a session's own
        # <session_state_dir>/config.yaml (#4200 1/2's session-layer read) never
        # passes through this method at all — that is correct (the operator owns
        # the envelope), not a gap. "base_dir never widens" is true only for the
        # LLM-driven spawn path, not for the config surface as a whole.
        resolved_base_dir: "Path | None" = None
        if base_dir is not None:
            parent_base_dir = getattr(parent_session, "_workspace_base_dir", None)
            if parent_base_dir is None:
                return {
                    "status": "error", "kind": "base_dir_floor_unknown",
                    "error": (
                        "base_dir was requested, but this session's own effective "
                        "base_dir could not be resolved to validate it against — "
                        "refusing rather than accepting an unvalidated path."
                    ),
                }
            candidate = Path(base_dir)
            if not candidate.is_absolute():
                candidate = parent_base_dir / candidate
            candidate = candidate.resolve()
            parent_resolved = Path(parent_base_dir).resolve()
            if candidate != parent_resolved and parent_resolved not in candidate.parents:
                return {
                    "status": "error", "kind": "base_dir_outside_parent",
                    "error": (
                        f"requested base_dir {str(candidate)!r} resolves outside "
                        f"your own base_dir {str(parent_resolved)!r} — restrict-only: "
                        "a spawned session's base_dir must fall under your own."
                    ),
                }
            resolved_base_dir = candidate
        try:
            sid = await self._registry.spawn_session_recorded(
                target_agent, sid=session, mode=mode, narrowing=narrowing,
                base_dir=resolved_base_dir,
                # #4193 ①: this method returns a spawn-ack and submits the task
                # below WITHOUT awaiting its completion — regardless of ``mode``.
                # A persistent spawn through this one path is exactly the gap
                # #4193 opened (fire-and-forget, but used to get the foreground
                # timeout pair as if someone were waiting). See
                # ``OpContext.attended``'s own docstring for the full picture.
                attended=False,
                presentation_consumer=_routing.presentation_consumer,
                intervention_bridge=_routing.intervention_bridge,
            )
        except ValueError as exc:
            # #4556: the registry's own duplicate-(name, sid) guard (see
            # ``registry.spawn_session``) is the only ``ValueError`` this call can
            # raise — reshape it into the same typed-error-response convention as
            # every other guard in this method, never let it reach the LLM as a
            # raw exception.
            return {
                "status": "error", "kind": "session_already_exists",
                "error": str(exc),
            }
        # #2103 S1bc-exec: record (target_agent, sid)→task BEFORE submitting, so a
        # fast result finds the trusted task on return (else it falls back to the
        # from=-only rendering — both still kind=prompt, proposal 0067 P4 #3978,
        # architect ruling 2026-08-10). #4740: agent_name included — sid alone
        # collides across agents (see SpawnTracker's own comment for the defect).
        if self._record_spawned_task is not None:
            self._record_spawned_task(target_agent, sid, request)
        spawned_session = self._registry.ensure_session_running(target_agent, sid)
        if spawned_session is not None:
            await spawned_session.submit_agent_request(
                from_agent=self._agent_name, request=request, depth=0,
                chain_id=chain_id, from_sid=from_sid,
            )
        return {
            "status": "spawned",
            "sid": sid,
            "agent": target_agent,
            "mode": mode,
            "note": (
                "Fresh session spawned + task submitted; it runs in isolation. The "
                "result stays in the spawned session — routing it back is a follow-on "
                "(FP-0043 Stage-4)."
            ),
        }

    async def send_to_session(
        self, *, agent: str, session: str, text: str, wake: bool,
    ) -> dict:
        """Proposal 0067 P5 (#3978): fire-and-forget delivery to a peer
        (agent, session) — ``TurnOrigin.PEER_SESSION``, no chain/collection.

        Resolves THIS adapter's own backing Session (same pattern
        ``spawn_session`` above uses to reach the parent for bridging:
        ``self._registry.get_session(self._agent_name, live_session_id)``)
        and delegates to its ``_deliver_cross_session_message`` — the
        substrate already proven by 4 falsify-verified tests
        (``tests/runtime/test_deliver_cross_session_message_3978_p5.py``).
        No new callable injection needed: the adapter already holds
        ``self._registry``/``self._agent_name`` for exactly this shape.

        Returns an error-shaped response when the caller session cannot be
        resolved (mis-wiring) or the target session is not LIVE — mirrors
        ``delegate_to_agent``'s B33 W5 F2 fix: a success-shaped envelope for
        a message that never arrived invites the LLM to fabricate a reply
        on the peer's behalf.
        """
        if self._registry is None:
            raise RuntimeError(
                "send_to_session requires a registry (multi-session host) — "
                "unavailable in this context."
            )
        caller_sid = self.live_session_id
        caller = self._registry.get_session(self._agent_name, caller_sid)
        if caller is None:
            return {
                "status": "error",
                "kind": "caller_session_not_found",
                "error": (
                    f"internal: this session ({self._agent_name!r}, "
                    f"{caller_sid!r}) is not resolvable via the registry"
                ),
            }
        delivered = await caller._deliver_cross_session_message(  # noqa: SLF001
            target_agent=agent, target_session_id=session,
            kind=TurnOrigin.PEER_SESSION,
            payload={
                "text": text,
                "from_agent": self._agent_name,
                "from_session": caller_sid,
                "sender": f"peer_session:{self._agent_name}/{caller_sid}",
                # architect review (#4101): without this, a wake=false
                # ride-along's flush attribution falls back to the entry's
                # own kind for its label ("[peer_session:peer_session]"),
                # naming no peer at all — the flush's fallback is correct
                # by construction (proven, but only exercises this default
                # when a producer omits `name`), it is THIS producer's job
                # to supply the identifier, same two OS-side components
                # `sender` above already computes (never LLM/text-derived,
                # so no forgery surface).
                "name": f"{self._agent_name}/{caller_sid}",
            },
            wake=wake,
        )
        if not delivered:
            return {
                "status": "error",
                "kind": "target_session_not_found",
                "error": (
                    f"no live session {session!r} for agent {agent!r} — "
                    "send_to_session delivers only, it does not spawn one"
                ),
            }
        return {"status": "delivered", "agent": agent, "session": session, "wake": wake}

    async def run_prompt_result(
        self, *, agent: str, session: str, prompt: str, timeout: "float | None" = None,
    ) -> dict:
        """Proposal 0067 P4d (#3978): ``run_prompt(collect="attached")`` —
        deliver ``prompt`` to a LIVE peer ``(agent, session)`` and collect its
        reply IN-BAND, synchronously.

        Thin wiring layer only — resolution, the double-pump refusal, and the
        ``MessageBus.request`` drive all live in ``session_api.run_prompt_result``
        (see its docstring for the full rationale). This method's only job is
        to supply the CALLER's own identity (``self._agent_name`` /
        ``self.live_session_id``), mirroring ``send_to_session`` above.

        ``timeout`` defaults to ``_RUN_PROMPT_DEFAULT_TIMEOUT_S`` when the
        LLM-facing tool call omits it — ``session_api.run_prompt_result``
        itself takes ``timeout`` as a REQUIRED, no-default kwarg (architect's
        #3978 ruling: it bounds a genuine mutual-deadlock shape, not just a
        slow reply — an unbounded default there would silently reintroduce
        the hazard the requirement exists to close). This is the one layer
        allowed to supply a convenience default, because it is the one layer
        an omitting LLM caller actually reaches.

        ⚠️ No ``schema`` param here (unlike ``run_agent_step``): 0062's
        structured-output plumbing (a ``SchemaRegistry`` populated from a
        pipeline's registered schemas) exists only on the PIPELINE executor's
        ``agent``-step path today — there is no ``SchemaRegistry`` threaded to
        the router-tool layer at all, for ANY tool, to constrain generation
        against. Adding one is a real feature, out of scope for this PR;
        ``session_api.run_prompt_result`` itself still accepts
        ``schema``/``schema_registry`` (mirrors ``run_agent_step`` exactly)
        for whenever that plumbing exists — this method just never passes
        them."""
        if self._registry is None:
            raise RuntimeError(
                "run_prompt(collect=\"result\") requires a registry "
                "(multi-session host) — unavailable in this context."
            )
        from reyn.runtime.session_api import run_prompt_result as _run_prompt_result

        return await _run_prompt_result(
            self._registry,
            caller_agent=self._agent_name,
            # "main" fallback matches registry.get_session's own default
            # (_DEFAULT_SID) — live_session_id is None only when no live-sid
            # fn is wired AND the constructor's cached session_id is also
            # unset, which the "main" single-session case never hits.
            caller_sid=self.live_session_id or "main",
            target_agent=agent,
            target_session=session,
            prompt=prompt,
            timeout=timeout if timeout is not None else _RUN_PROMPT_DEFAULT_TIMEOUT_S,
        )

    async def run_prompt_async(
        self, *, agent: str, session: str, prompt: str,
    ) -> dict:
        """Proposal 0067 P4e (#3978): ``run_prompt(collect="async")`` —
        dispatch ``prompt`` to a LIVE peer and return a ``task_id``
        immediately; the reply arrives later via ``task_settled``.

        Thin wiring layer only, mirroring ``run_prompt_result`` above — all
        the real logic (registration, dispatch, refusal) lives in
        ``session_api.run_prompt_async``. This method's only job is to
        supply the CALLER's own identity."""
        if self._registry is None:
            raise RuntimeError(
                "run_prompt(collect=\"async\") requires a registry "
                "(multi-session host) — unavailable in this context."
            )
        from reyn.runtime.session_api import run_prompt_async as _run_prompt_async

        return await _run_prompt_async(
            self._registry,
            caller_agent=self._agent_name,
            caller_sid=self.live_session_id or "main",
            target_agent=agent,
            target_session=session,
            prompt=prompt,
        )

    async def _spawn_limit_checkpoint(
        self, *, kind: str, prompt: str, detail: str,
        extension_amount: float, run_id: str,
    ) -> Any:
        """#2175: route a spawn-limit exceed through the safety.on_limit checkpoint
        (mode-driven: unattended=reject / interactive=ask / auto_extend). When no
        checkpoint is wired (headless / test stub), degrade to UNATTENDED = reject (the
        C3 hard-deny posture). On allow_continue the Session helper bumps the shared
        ``_safety_extensions[kind]`` so a same-scope re-check won't re-prompt — the
        no-self-raise invariant holds (the extension is human/operator-approved, the base
        stays config-set restart-only)."""
        if self._handle_chat_limit_checkpoint is None:
            from reyn.runtime.limits.limit_handler import LimitDecision
            return LimitDecision(
                allow_continue=False, extension=0.0, reason="no_checkpoint_unattended",
            )
        return await self._handle_chat_limit_checkpoint(
            kind=kind, prompt=prompt, detail=detail,
            extension_amount=extension_amount, run_id=run_id,
        )

    async def spawn_agent(self, *, name: str, role: str) -> dict:
        """#2103 B-tool: create a new AGENT under THIS agent (the spawner = parent).

        Routes through ``registry.create_agent(parent=<spawner>)`` — the ONE create
        seam — so the new agent's spawn LINEAGE is OS-SET + immutable (the LLM supplies
        only WHO=name/role; the parent link is set by the OS, the forge-guard) AND
        carried on ``agent_created`` for rewind-reconstruction. By B-core, the new
        agent's effective capability is then capped at ⊆ the spawner by construction
        (resolved_profile_for composes the spawner's live resolved as a conjunct).
        Narrowing the child below the spawner is via ``create_topology`` (C)."""
        if self._registry is None:
            raise RuntimeError(
                "spawn_agent requires a registry (multi-agent host) — unavailable in "
                "this context."
            )
        parent = self._agent_name
        # #2175: operator spawn-tree bounds (safety.spawn.*) routed through the
        # safety.on_limit checkpoint (mode-driven: unattended=reject / interactive=ask the
        # operator / auto_extend), exactly like inter_agent_messaging's max_agent_hops over
        # max_hop_depth + _safety_extensions. LLM-seam only (operator CLI create is
        # unbounded = authority, C1). No-self-raise: the BASE is config-set restart-only;
        # any extension is human/operator-approved, never LLM. DEPTH and FAN-OUT carry
        # SEPARATE per-spawner extension keys so an approved widen of one does not silently
        # widen the other (approval-scoping).
        base_depth = self._registry.max_spawn_depth
        if base_depth:
            cur_depth = self._registry.spawn_depth(parent)
            eff_depth = base_depth + int(
                self._safety_extensions.get(f"max_spawn_depth:{parent}", 0.0)
            )
            if cur_depth + 1 > eff_depth:
                decision = await self._spawn_limit_checkpoint(
                    kind=f"max_spawn_depth:{parent}",
                    prompt=(
                        f"Spawn depth {cur_depth + 1} would exceed max_spawn_depth "
                        f"({eff_depth}). Allow agent {parent!r} to spawn deeper?"
                    ),
                    detail=f"parent={parent} depth={cur_depth + 1} cap={eff_depth}",
                    extension_amount=1.0,
                    run_id=parent,
                )
                if not decision.allow_continue:
                    return {
                        "status": "error", "kind": "spawn_limit_exceeded",
                        "error": (
                            f"spawn-limit: max_spawn_depth={eff_depth} would be exceeded "
                            f"(parent {parent!r} at spawn-depth {cur_depth}). "
                            "→ Raise safety.spawn.max_depth to allow deeper spawn trees."
                        ),
                    }
        base_children = self._registry.max_spawn_children
        if base_children:
            cur_children = self._registry.spawn_child_count(parent)
            eff_children = base_children + int(
                self._safety_extensions.get(f"max_spawn_fanout:{parent}", 0.0)
            )
            if cur_children >= eff_children:
                decision = await self._spawn_limit_checkpoint(
                    kind=f"max_spawn_fanout:{parent}",
                    prompt=(
                        f"Spawning would give {parent!r} {cur_children + 1} children, "
                        f"exceeding max_spawn_children ({eff_children}). Allow?"
                    ),
                    detail=f"parent={parent} children={cur_children} cap={eff_children}",
                    extension_amount=1.0,
                    run_id=parent,
                )
                if not decision.allow_continue:
                    return {
                        "status": "error", "kind": "spawn_limit_exceeded",
                        "error": (
                            f"spawn-limit: max_spawn_children={eff_children} would be "
                            f"exceeded (parent {parent!r} has {cur_children} children). "
                            "→ Raise safety.spawn.max_children to allow wider fan-out."
                        ),
                    }
        try:
            await self._registry.create_agent(name, role=role, parent=parent)
        except FileExistsError:
            return {"status": "error", "kind": "agent_exists",
                    "error": f"agent {name!r} already exists."}
        except ValueError as e:  # lineage guard (self-link / cycle / immutable) or bad name
            return {"status": "error", "kind": "spawn_rejected", "error": str(e)}
        return {
            "status": "spawned",
            "name": name,
            "parent": parent,
            "note": (
                "New agent created; its capabilities are capped at ⊆ yours (the spawn "
                "lineage). Use create_topology to narrow it further / wire messaging."
            ),
        }

    async def create_topology(
        self,
        *,
        name: str,
        kind: str,
        members: "list[str]",
        leader: "str | None" = None,
        profiles: "dict[str, str] | None" = None,
    ) -> dict:
        """#2103 C1: wire/narrow agents THIS agent spawned into a topology (org-design).

        Routes through ``registry.create_topology(topo)`` — the ONE logged CREATE seam
        (#2153, add_topology + emit topology_created with the full config incl profiles),
        so the topology is fully WAL-tracked for rewind reconstruction (never the sync
        ``add_topology``).

        Forge-guard (Q1, lead-approved): every member must be in THIS agent's spawn
        SUBTREE (itself or a transitive spawn-descendant). That makes the profile
        bindings safe by construction — each bound member is already ⊆ the creator via
        the B-core lineage conjunct, so a binding can only narrow within that envelope,
        never re-grant past it. An LLM thus cannot wire a non-descendant peer it doesn't
        own (which would be a capability grant). Operator CLI/web create paths are
        unrestricted (operator-authority); this seam is the LLM-tool path only."""
        if self._registry is None:
            raise RuntimeError(
                "create_topology requires a registry (multi-agent host) — unavailable "
                "in this context."
            )
        creator = self._agent_name
        members = list(members)
        # #2175: max_children governs topology SIZE too (org fan-out), routed through the
        # on_limit checkpoint. SEPARATE extension key from spawn_agent fan-out (approving a
        # bigger org ≠ approving more direct children — different intents, Q3). A bulk
        # create extends by the exact gap so the approval covers this topology.
        base_children = self._registry.max_spawn_children
        if base_children:
            eff_members = base_children + int(
                self._safety_extensions.get(f"max_topology_members:{creator}", 0.0)
            )
            if len(members) > eff_members:
                decision = await self._spawn_limit_checkpoint(
                    kind=f"max_topology_members:{creator}",
                    prompt=(
                        f"Topology {name!r} has {len(members)} members, exceeding "
                        f"max_spawn_children ({eff_members}). Allow this org size?"
                    ),
                    detail=f"creator={creator} members={len(members)} cap={eff_members}",
                    extension_amount=float(len(members) - eff_members),
                    run_id=creator,
                )
                if not decision.allow_continue:
                    return {
                        "status": "error", "kind": "spawn_limit_exceeded",
                        "error": (
                            f"spawn-limit: max_spawn_children={eff_members} would be "
                            f"exceeded (topology has {len(members)} members). "
                            "→ Raise safety.spawn.max_children to allow larger orgs."
                        ),
                    }
        outside = [
            m for m in members if not self._registry.is_spawn_descendant(m, creator)
        ]
        if outside:
            return {
                "status": "error",
                "kind": "member_outside_subtree",
                "error": (
                    f"create_topology: {sorted(outside)} are not in your spawn subtree "
                    "— you may only wire agents you spawned (or yourself). Use "
                    "spawn_agent to create them under your authority first."
                ),
            }
        from reyn.runtime.topology import Topology
        try:
            topo = Topology.new(
                name, kind=kind, members=members, leader=leader, profiles=profiles
            )
        except (ValueError, KeyError) as e:  # bad name/kind/leader, profile-non-member
            return {"status": "error", "kind": "invalid_topology", "error": str(e)}
        try:
            await self._registry.create_topology(topo)
        except FileExistsError:
            return {"status": "error", "kind": "topology_exists",
                    "error": f"topology {name!r} already exists."}
        except ValueError as e:  # reserved/auto-managed name, unknown member agent
            return {"status": "error", "kind": "create_rejected", "error": str(e)}
        return {
            "status": "created",
            "name": name,
            "kind": kind,
            "members": members,
            "leader": leader,
            "profiles": dict(profiles or {}),
        }

    def append_history_entry(
        self,
        *,
        role: str,
        content: Any,
        meta: dict | None = None,
        tool_calls: "list[dict] | None" = None,
        tool_call_id: "str | None" = None,
        name: "str | None" = None,
    ) -> None:
        """E-full PR-E (issue #383): persist a single ChatMessage entry
        without an outbox side-effect.

        Used by ``RouterLoop.run()`` to record per-iteration assistant
        tool_call turns (= ``role="assistant"`` + ``tool_calls`` field)
        and tool response turns (= ``role="tool"`` + ``tool_call_id`` +
        ``name``). The pre-PR-E producer only persisted the LLM's final
        text reply via ``put_outbox(kind="agent")``; this method closes
        the gap so the next ``_build_history_for_router`` rebuild
        replays the full LLM message sequence.
        """
        from reyn.runtime.chat_message import ChatMessage, _now_iso
        self._append_history_cb(ChatMessage(
            role=role,
            content=content,
            ts=_now_iso(),
            meta=meta if meta is not None else {},
            tool_calls=tool_calls,
            tool_call_id=tool_call_id,
            name=name,
        ))

    def mark_untrusted_in_flight(self) -> None:
        """#4381 PR-2 stage ③: forward to ``Session._mark_untrusted_in_flight``
        when wired.

        None-safe (pre-existing hand-built adapters, most test construction)
        — behaves exactly like a host that never implemented the hook, same
        convention as ``peek_mid_turn_injection`` above. Called from
        ``router_loop.py``'s tool-result production, at the SAME line that
        stamps ``external_source`` onto the persisted ``ChatMessage.meta``
        (single update point — no second, independently-maintained taint
        signal to drift out of sync with the one ``metas_have_untrusted``
        reads once the entry lands in history).
        """
        if self._mark_untrusted_in_flight_cb is not None:
            self._mark_untrusted_in_flight_cb()

    async def peek_mid_turn_injection(self) -> "dict | None":
        """#3792: forwards to ``Session.peek_mid_turn_injection`` when wired.

        None-safe when this adapter was constructed without the
        ``peek_mid_turn_injection`` callback (pre-#3792 call sites, most
        test construction) — behaves exactly like a host that never
        implemented the hook at all (RouterLoop's own getattr-guard treats
        the two identically: no injection this round)."""
        if self._peek_mid_turn_injection_cb is None:
            return None
        return await self._peek_mid_turn_injection_cb()

    async def commit_mid_turn_injection(self, msg_id: str) -> None:
        """#3792: forwards to ``Session.commit_mid_turn_injection`` when
        wired. None-safe, same reasoning as :meth:`peek_mid_turn_injection`."""
        if self._commit_mid_turn_injection_cb is None:
            return
        await self._commit_mid_turn_injection_cb(msg_id)

    async def put_outbox(
        self, *, kind: str, text: str, meta: dict, persist: bool = True,
    ) -> None:
        from reyn.runtime.chat_message import ChatMessage, _now_iso
        from reyn.runtime.outbox import OutboxMessage
        # #1652: centralised reasoning handling for agent replies. The router
        # passes the turn's reasoning as meta["reasoning"]; this single chokepoint
        # applies the two independent gates so every agent-reply site is covered
        # by-construction:
        #   - DISPLAY (toggle2): emit a discrete kind="reasoning" OutboxMessage
        #     BEFORE the reply (the channels render reasoning ONLY from this
        #     signal — never from agent meta — so display-off = no render).
        #   - then strip reasoning from the agent OutboxMessage's meta.
        #   - PERSIST (toggle1): keep reasoning on the persisted history
        #     ChatMessage only when continuity is on (so replay can read it);
        #     the wire-shape (content+tool_calls) never carries it → no
        #     native double-inject on gemini.
        # #1652/②: reasoning is now a captured BUNDLE
        # ({reasoning_content?, thinking_blocks?, ...}) — extract the text for
        # display (legacy str entries are absorbed by reasoning_text). The bundle
        # itself is persisted (below) so the wire re-attach can replay it.
        _reasoning = meta.get("reasoning") if kind == "agent" else None
        if _reasoning and self.reasoning_display_enabled():
            from reyn.runtime.reasoning_continuity import reasoning_text
            _reasoning_display = reasoning_text(_reasoning)
            if _reasoning_display:
                await self._put_outbox_in.put_outbox(OutboxMessage(
                    kind="reasoning",
                    text=_reasoning_display,
                    meta={"chain_id": meta.get("chain_id"), "reasoning": _reasoning},
                ))
        _outbox_meta = (
            {k: v for k, v in meta.items() if k != "reasoning"}
            if "reasoning" in meta else meta
        )
        await self._put_outbox_in.put_outbox(
            OutboxMessage(kind=kind, text=text, meta=_outbox_meta)
        )
        # Persist agent (conversational) replies to history so the context
        # window stays coherent across turns.
        #
        # Note on empty-stop canned text: dogfood trace v6 showed that
        # filtering router-empty-response text out of history (= the naive
        # "don't pollute LLM context with failure notices" patch) creates
        # a worse downstream pattern — the next turn's LLM sees two
        # consecutive ``role="user"`` messages with no assistant between
        # them, which is itself an attractor (= same shape as the
        # commit 3732275 duplicate-user bug we fixed earlier). Keeping
        # the canned text in history maintains alternation; the
        # cascading-attractor mitigation needs to live elsewhere
        # (= context build / classifier-side, tracked as follow-up).
        #
        # #3633: ``persist`` (default True) makes this append an EXPLICIT
        # per-call-site choice rather than an implicit blanket rule. A caller
        # whose text is already persisted through a different path (e.g.
        # router_loop's tool-turn display bubble — the SAME text is persisted
        # a few lines later as the canonical record by
        # ``append_history_entry`` in ``RouterLoop.feedback()``, complete
        # with ``tool_calls``) passes ``persist=False`` so the display-only
        # outbox emit does not ALSO write to history.jsonl. Do not add a new
        # unconditional persist path here without checking whether the text
        # is already recorded elsewhere.
        if kind == "agent" and text and persist:
            # Issue #383: chat history now uses ``role="assistant"`` +
            # ``content=`` (= wire shape mirror); the OutboxMessage above
            # keeps ``kind="agent"`` since that's the TUI-facing
            # OutboxMessage taxonomy, independent of the LLM-side role.
            # #1652: persist reasoning on the history ChatMessage ONLY when
            # continuity is on (so _reasoning_continuity_section can replay it);
            # otherwise persist the stripped meta. Either way the wire-shape
            # builder never emits meta to the LLM (no native double-inject).
            _persist_meta = (
                meta if (_reasoning and self.reasoning_continuity_enabled())
                else _outbox_meta
            )
            self._append_history_cb(ChatMessage(
                role="assistant", content=text, ts=_now_iso(), meta=_persist_meta,
            ))
            # Capture for agent-to-agent paths that need to forward the
            # reply upstream via _send_agent_response.
            replies = self._put_outbox_in.agent_replies_tracker()
            if replies is not None:
                replies.append(text)

    # --- #1652 reasoning capture/continuity/display ---

    def reasoning_display_enabled(self) -> bool:
        """Whether the model's reasoning text should be surfaced to the UI
        (config ``chat.reasoning.display``; default False when unconfigured).

        #4206 slice 2: when ``reasoning_display_fn`` was supplied (the
        production `Session` path), calls it fresh EVERY time — the ③
        preference-axis live re-resolution (session/agent overrides), not
        the frozen ``reasoning_config.display`` this adapter was
        constructed with. ``None`` (every pre-slice-2 caller, every test
        host that doesn't pass the new callback) falls back to the
        original frozen-config read, byte-identical to before this slice."""
        if self._reasoning_display_fn is not None:
            return bool(self._reasoning_display_fn())
        return bool(getattr(self._reasoning_config, "display", False))

    def warn_ratio_overrides(self) -> "dict[str, float]":
        """#4206 Slice B (#4724): the caller-resolved ③ preference-axis
        overrides for the cost.*.warn_ratio keys, or ``{}`` when
        ``warn_ratio_overrides_fn`` was not supplied (every pre-Slice-B
        caller, every test host) — byte-identical to before this slice."""
        if self._warn_ratio_overrides_fn is not None:
            return self._warn_ratio_overrides_fn()
        return {}

    def model_class_ceiling(self) -> "str | None":
        """#4206 ②: the caller-resolved bounding-axis composed ``model``
        ceiling, or ``self._resolver.class_ceiling()`` (the project-only
        value, #4206 T1's own accessor, unchanged) when
        ``model_class_ceiling_fn`` was not supplied (every pre-② caller,
        every test host) — byte-identical to before this slice."""
        if self._model_class_ceiling_fn is not None:
            return self._model_class_ceiling_fn()
        return self._resolver.class_ceiling()

    def reasoning_continuity_enabled(self) -> bool:
        """Whether reasoning is persisted to history + replayed into the next
        turn (config ``chat.reasoning.continuity``; default False unconfigured)."""
        return bool(getattr(self._reasoning_config, "continuity", False))

    def reasoning_continuity_section(self) -> str:
        """Pre-rendered prior-reasoning text section for the next system prompt,
        or ``""`` when continuity is off / no prior reasoning. The session
        callback reads recent history + applies the bound + continuity gate."""
        if self._reasoning_continuity_section_fn is None:
            return ""
        return self._reasoning_continuity_section_fn() or ""

    # #3607: the four file-op delegates (``file_read`` / ``file_write`` /
    # ``file_delete`` / ``file_regenerate_index``) lived here only so
    # RouterLoop could re-implement `remember`/`forget`/`read_memory_body`
    # on top of them. The memory operations are now MemoryService's, and
    # they call the SAME session callbacks directly (Session wires them
    # into MemoryService, as it always did) — one hop fewer, and the router
    # host no longer offers the LLM's loop a general file primitive it had
    # no business holding. ``file_read``'s #3193 dict→string flattening
    # went with it: MemoryService.read_body returns a dict, so the
    # truncation signal rides as sibling keys instead of appended prose.

    # --- MCP ops ---

    # #3447: the five discovery-only listing methods used to be thin
    # ``self._mcp_list_*_cb(...)`` delegates onto Session (which held the
    # gateway-construction / error-catch logic in ``_mcp_list_via_gateway`` /
    # ``_mcp_resolve_server_config``). They are now the adapter's own
    # implementation — this is where Path A's fold-into-execute_op lands:
    # the gateway call RAISES ``Cancelled``/``MCPFault`` instead of catching
    # them here; the catch moved to ``tools/mcp.py``'s ``_handle_list_mcp_*``
    # handlers (the existing ``_mcp_list_error`` sentinel-check position).
    # Architect firm (#3411, 2026-07-29): no context-manager / audit-emit /
    # pool-teardown step sits between the raise site (inside the gateway
    # call, below) and either catch position, so moving the catch upward is
    # behavior-preserving, not a contract change.

    def _mcp_resolve_server_config(self, server: str) -> "list[dict] | dict":
        """Shared config-resolution step for all five ``mcp_list_*`` methods:
        look up *server* in the flattened MCP server map and ``expand_env``
        it. Returns the expanded config dict on success, or a single-error
        ``[{"error": ...}]`` list (the methods' existing early-return shape,
        NOT an exception — this is validation, not a gateway-call failure)
        when the server isn't configured / doesn't resolve to a dict."""
        servers = self._mcp_servers_flat()
        if not servers:
            return [{"error": "no MCP servers configured"}]
        server_cfg = servers.get(server)
        if not server_cfg:
            return [{"error": f"MCP server {server!r} not configured"}]

        from reyn.mcp.client import expand_env

        expanded = expand_env(server_cfg)
        if not isinstance(expanded, dict):
            return [{"error": f"MCP server {server!r} config must be a dict"}]
        if "type" not in expanded and expanded.get("url"):
            expanded = {**expanded, "type": "streamable-http"}
        return expanded

    async def _mcp_list_via_gateway(
        self,
        server: str,
        expanded: dict,
        *,
        gateway_call: "Callable[[Any], Awaitable[list[dict]]]",
        event_kind: str,
    ) -> list[dict]:
        """Shared MCP-listing seam (#3082, folded here #3447): owns gateway
        construction and the audit emit for all four gateway-backed
        ``mcp_list_*`` methods (tools / resources / resource_templates /
        prompts — ``mcp_list_servers`` never reaches this, it has no gateway
        call). Each caller has already resolved its own *server* config into
        *expanded* and passes a *gateway_call* closure naming which
        ``MCPGateway`` listing method to invoke, plus *event_kind* — the
        audit-event kind this listing emits.

        ``Cancelled``/``MCPFault`` are NOT caught here (#3447) — they
        propagate to the caller (``tools/mcp.py``'s ``_handle_list_mcp_*``),
        matching the call-family (``mcp``/``mcp_read_resource``/etc.)
        exception contract.

        ``event_kind`` is passed as a string LITERAL by every call site and is
        never assembled here (#3410): a kind the vocabulary gate cannot read
        as a constant is a kind it cannot check against the closed
        vocabulary.
        """
        from reyn.mcp.gateway import MCPGateway

        # #3482: mcp_gateway_inputs bundle — mcp_connection_service/mcp_agent_id/
        # ephemeral_fn are read ONLY here (this method), the real cluster #3447 formed.
        ephemeral_fn = self._mcp_gateway.ephemeral_fn
        ephemeral = ephemeral_fn() if ephemeral_fn is not None else False
        mcp_agent_id = self._mcp_gateway.mcp_agent_id
        # #2421: routed through the MCPGateway seam rather than a raw MCP client — the
        # seam contains the crash path, so a mid-list server death raises MCPFault
        # instead of an uncontained BaseExceptionGroup. #2597 S2a: pool only when
        # non-ephemeral — pooling a sub-second-lived session is pure churn.
        # (Wording note: keep the class name away from a following "(" — the #2813
        # completeness scanner reads `MCPGateway\s*\(` as a construction site.)
        gateway = (
            MCPGateway(
                pool=self._mcp_gateway.mcp_connection_service, agent_id=mcp_agent_id,
                cancel_event=self._op_ctx_source.cancel_event,
            )
            if not ephemeral
            else MCPGateway(
                agent_id=mcp_agent_id, cancel_event=self._op_ctx_source.cancel_event,
            )
        )
        result = await gateway_call(gateway)
        self._events.emit(event_kind, server=server, count=len(result))
        return result

    async def mcp_list_servers(self) -> list[dict]:
        """Returns the configured MCP server list with descriptions."""
        return self._get_mcp_servers_for_router()

    async def mcp_list_subscriptions(self) -> list[dict]:
        """#4686: per-CONNECTION resource-subscription state, one entry per
        HELD server that has at least one subscribed URI. Discovery-only,
        NOT permission-gated (no op-kind — same class as ``mcp_list_servers``
        above), and — unlike every other ``mcp_list_*`` method here — never
        a gateway round trip: subscription tracking is entirely session-local
        state (``MCPConnectionService``, never WAL'd — see its own module
        docstring), so there is nothing on the server side to query.

        Shape (mirrors ``Session.mcp_subscription_state``'s own docstring
        exactly, which is the sole producer):
        ``[{"server", "mode": "legacy" | "listen" | None, "uris": [...],
        "unhonored": [...] | None}, ...]``. Deliberately per-connection, not
        merged/aggregated across servers — what "subscribed" even confirms
        differs between a Legacy connection (can't report honored-ness) and
        a Listen connection (can), so collapsing to one count across
        connections of different modes would lose exactly the distinction
        this tool exists to surface (#4686 issue thread).

        Reads ``self._mcp_gateway.mcp_connection_service`` directly (the
        raw ``McpGatewayInputs`` field, same object other gateway methods on
        this class already reach via ``self._mcp_gateway`` — see e.g. the
        pool-construction call a few lines below) rather than via an
        injected async callback: unlike ``mcp_subscribe_resource``/
        ``mcp_read_resource``, this never touches the network, so there is
        no failure mode an ``_cb is None`` guard would need to degrade."""
        return self._mcp_gateway.mcp_connection_service.subscription_summary()

    async def mcp_list_tools(self, server: str) -> list[dict]:
        """Query the MCP server for its tools list. Discovery-only, NOT
        permission-gated (no op-kind). Emits ``mcp_tools_listed``."""
        expanded = self._mcp_resolve_server_config(server)
        if isinstance(expanded, list):
            return expanded
        return await self._mcp_list_via_gateway(
            server, expanded,
            gateway_call=lambda gw: gw.list_tools(server, expanded),
            event_kind="mcp_tools_listed",
        )

    async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        return await self._mcp_call_tool_cb(server, tool, args)

    # #2597 slice ②a: resources consumption.
    async def mcp_list_resources(self, server: str) -> list[dict]:
        """Mirrors ``mcp_list_tools`` exactly — discovery-only, NOT
        permission-gated. Emits ``mcp_resources_listed``."""
        expanded = self._mcp_resolve_server_config(server)
        if isinstance(expanded, list):
            return expanded
        return await self._mcp_list_via_gateway(
            server, expanded,
            gateway_call=lambda gw: gw.list_resources(server, expanded),
            event_kind="mcp_resources_listed",
        )

    async def mcp_list_resource_templates(self, server: str) -> list[dict]:
        """Mirrors ``mcp_list_resources`` (discovery-only, not
        permission-gated). Empty list is a normal result for a server that
        registers no templates. Emits ``mcp_resource_templates_listed``."""
        expanded = self._mcp_resolve_server_config(server)
        if isinstance(expanded, list):
            return expanded
        return await self._mcp_list_via_gateway(
            server, expanded,
            gateway_call=lambda gw: gw.list_resource_templates(server, expanded),
            event_kind="mcp_resource_templates_listed",
        )

    async def mcp_read_resource(self, server: str, uri: str) -> dict:
        if self._mcp_read_resource_cb is None:
            return {"status": "error", "error": "mcp resource read is not wired on this host"}
        return await self._mcp_read_resource_cb(server, uri)

    # #2597 slice ②b: resource subscriptions. Same getattr-guarded-callback
    # pattern as the ②a resources methods above.
    async def mcp_subscribe_resource(self, server: str, uri: str) -> dict:
        if self._mcp_subscribe_resource_cb is None:
            return {"status": "error", "error": "mcp resource subscribe is not wired on this host"}
        return await self._mcp_subscribe_resource_cb(server, uri)

    async def mcp_unsubscribe_resource(self, server: str, uri: str) -> dict:
        if self._mcp_unsubscribe_resource_cb is None:
            return {"status": "error", "error": "mcp resource unsubscribe is not wired on this host"}
        return await self._mcp_unsubscribe_resource_cb(server, uri)

    # #2597 slice ②c: prompts consumption.
    async def mcp_list_prompts(self, server: str) -> list[dict]:
        """Mirrors ``mcp_list_resources`` exactly (prompts are addressed by
        name, not URI, but the discovery shape is otherwise identical).
        Emits ``mcp_prompts_listed``."""
        expanded = self._mcp_resolve_server_config(server)
        if isinstance(expanded, list):
            return expanded
        return await self._mcp_list_via_gateway(
            server, expanded,
            gateway_call=lambda gw: gw.list_prompts(server, expanded),
            event_kind="mcp_prompts_listed",
        )

    async def mcp_get_prompt(self, server: str, name: str, arguments: dict | None = None) -> dict:
        if self._mcp_get_prompt_cb is None:
            return {"status": "error", "error": "mcp prompt get is not wired on this host"}
        return await self._mcp_get_prompt_cb(server, name, arguments)

    # --- Model resolution ---

    @property
    def resolver(self) -> Any:
        """The bound ``ModelResolver``.

        Exposed (#1172) so components that construct their own LLM callers —
        e.g. the planner's lazy ``CompactionEngine`` — can resolve model
        classes through the same chain as the router. ``resolve_model`` is the
        scalar convenience wrapper; this is the full resolver object.
        """
        return self._resolver

    def resolve_model(self, name: str) -> str:
        """Resolve config model name (e.g. 'router') to actual model id."""
        return self._resolver.resolve(name).model

    def resolve_model_spec(self, name: str) -> "Any":
        """#1654: resolve a config model name to the FULL ModelSpec (model +
        operator kwargs). The chat router must pass this to call_llm_tools so
        per-model kwargs (reasoning_effort, temperature, extra_body, …) reach
        litellm. ``resolve_model`` returns the bare ``.model`` string, DROPPING
        those kwargs — which left reasoning_effort (#1650/#1652) and every model
        kwarg inert on the chat-router path."""
        return self._resolver.resolve(name)

    def context_window_status(self) -> "dict | None":
        """#272/#1128: live exact-token context budget for the SP context-size
        signal, or None when no provider is wired (= signal omitted)."""
        if self._context_window_status is None:
            return None
        try:
            return self._context_window_status()
        except Exception:  # noqa: BLE001 — signal is best-effort, never break a turn
            return None

    # --- Private helpers ---

    def _get_file_permissions_for_router(self) -> dict | None:
        """Return {read: [paths], write: [paths]} or None when both axes are empty.

        #3458: a pass-through to ``PermissionResolver.advertised_file_permissions()``
        — the same resolution the runtime gate enforces (this used to be a
        second, config-only parser duplicated from ``Session``)."""
        if self._perm is None:
            return None
        return self._perm.advertised_file_permissions()

    def _mcp_servers_flat(self) -> dict:
        """Unwrap config.mcp's ``{servers: {...}}`` shape to flat ``{name: cfg}``."""
        raw = self._mcp_servers or {}
        if isinstance(raw, dict) and "servers" in raw:
            inner = raw.get("servers") or {}
            return inner if isinstance(inner, dict) else {}
        return raw if isinstance(raw, dict) else {}

    def _get_mcp_servers_for_router(self) -> list[dict]:
        """Return [{name, description, tools?}, ...] for configured MCP servers.

        ``tools`` is included when `ensure_mcp_tools_cached()` has an ANSWER
        for that server; absent otherwise (#3520 — a server whose probe did
        not answer has no cache entry at all, so the `mcp_tool_name` enum
        simply omits it and the next turn re-probes). Callers downstream
        (= `_enumerate_category("mcp.tool")` in `universal_catalog.py` and
        `router_loop.py`'s `mcp.tool__*` alias builder) iterate `tools`
        defensively so the missing-tools case is graceful.

        Issue #160 / FP-0037 context: chat startup intentionally does NOT
        probe MCP servers (= zero-startup-latency goal). The first user
        turn calls `ensure_mcp_tools_cached()` to fill the cache once per
        session; subsequent turns read it without additional probes.
        """
        servers = self._mcp_servers_flat()
        if not servers:
            return []
        tools_cache = self._mcp_tools_cache or {}
        result: list[dict] = []
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            entry: dict = {
                "name": name,
                "description": cfg.get("description", ""),
            }
            cached = tools_cache.get(name)
            if cached is not None:
                entry["tools"] = cached.tools
            result.append(entry)
        return result

    async def ensure_mcp_tools_cached(
        self, *, per_server_timeout: float = _DEFAULT_MCP_PROBE_SECONDS,
    ) -> None:
        """Probe each configured MCP server whose tool list is not yet known
        and cache the ANSWERS for the session lifetime.

        Called by `Session._handle_user_message` at the start of each
        user turn. The first call populates the cache (= lazy, post-startup,
        per FP-0037 issue #160). Later calls are no-ops **as long as every
        configured server has an answer**.

        #3520: the guard is per-server, not one-shot, and that is the whole
        fix. It used to be `if self._mcp_tools_cache is not None: return`,
        which is correct only if every entry in the cache is a measurement.
        It was not: a probe that timed out or raised was stored as `[]`, i.e.
        the *failure to measure* was recorded as the *result* of measuring,
        and the permanent cache then made that non-answer permanent too — the
        model was never told about that server's tools again, for the rest of
        the session and (once the file was written) across restarts. Now an
        unanswered probe produces `ToolsUnknown`, which `answered_only()`
        drops, so the server has no entry, so this guard sees it as still
        needing a probe and re-probes it on the next turn. Permanence is thus
        a property of ANSWERS only. This is deliberately NOT "re-probe every
        turn": a server that answered — including one that answered "zero
        tools" — is probed once and reused exactly as before.

        FP-0037 S1: before probing, checks for a pre-written cache file at
        ``<state_dir>/mcp_tools_cache.json``. If present and parseable, the
        in-memory cache is warm-started from disk (= zero probe latency on
        sessions after the operator ran ``reyn mcp refresh``). Servers the
        file does not cover are then live-probed, and the merged answers are
        written back for future warm-starts.

        Probes run in parallel via `asyncio.gather` so a single slow /
        unreachable server does not block the others. Per-server timeout caps
        each probe. #3475: degradation used to be silent — a server dropping
        out under co-located load left no trace anywhere the operator could
        see. Each such case emits an `mcp_tool_probe_degraded` audit-event
        naming the server and the reason (`timeout` / `exception`), so "this
        server is unreachable" is an observation instead of an inference from
        a missing tool the model never mentions. `per_server_timeout` (default
        `TimeoutConfig.mcp_probe_seconds`, #3475) is operator-tunable via
        `safety.timeout.mcp_probe_seconds` in reyn.yaml.

        The result feeds `_get_mcp_servers_for_router` which is consumed
        by `_enumerate_category("mcp.tool")` (= list_actions visibility)
        and the `mcp.tool__*` direct-alias builder in `router_loop.py`.
        """
        import asyncio

        from reyn.runtime.services.mcp_cache_file import (
            cache_file_path,
            file_mtime,
            read_cache,
            write_cache,
        )

        cache_path = cache_file_path(self._state_dir)

        # FP-0037 S1: warm-start from persistent cache file when available.
        # Only on the very first call — once the in-memory cache exists, the
        # disk file is the business of maybe_reload_mcp_tools_cache_from_disk.
        if self._mcp_tools_cache is None:
            disk_cache = read_cache(cache_path)
            if disk_cache is not None:
                self._mcp_tools_cache = disk_cache
                self._mcp_tools_cache_mtime = file_mtime(cache_path)
            else:
                self._mcp_tools_cache = {}

        servers = self._mcp_servers_flat()
        unanswered = [name for name in servers if name not in self._mcp_tools_cache]
        if not unanswered:
            return

        async def _probe_one(server_name: str) -> tuple[str, ProbeOutcome]:
            # ``asyncio.timeout()`` (Python 3.11+) instead of
            # ``asyncio.wait_for`` because the latter wraps the awaited
            # coroutine in a new asyncio.Task in some scenarios. When that
            # inner task is cancelled mid-``MCPClient.initialize`` (= the
            # underlying mcp SDK opens anyio cancel scopes inside an
            # AsyncExitStack), the cleanup ends up running in a different
            # task than the one that entered the scope, producing
            # ``RuntimeError: Attempted to exit cancel scope in a different
            # task than it was entered in``. ``asyncio.timeout()`` is a
            # task-local deadline (= no task wrap) and cancellation is
            # raised at the awaiter in the SAME task, so the AsyncExitStack
            # unwinds correctly.
            try:
                async with asyncio.timeout(per_server_timeout):
                    tools = await self.mcp_list_tools(server_name)
            except (TimeoutError, asyncio.TimeoutError):
                # #3475: surface the degradation — silence made the earlier
                # #3475 investigation depend on a fixture byte-diff to notice
                # it. #3520: and return UNKNOWN, not `[]` — we did not learn
                # that this server has no tools, we learned nothing.
                self._events.emit(
                    "mcp_tool_probe_degraded",
                    server=server_name,
                    reason="timeout",
                    per_server_timeout=per_server_timeout,
                )
                return server_name, ToolsUnknown(reason="timeout")
            except Exception as exc:  # noqa: BLE001 — adapter must never raise
                self._events.emit(
                    "mcp_tool_probe_degraded",
                    server=server_name,
                    reason="exception",
                    per_server_timeout=per_server_timeout,
                    detail=repr(exc),
                )
                return server_name, ToolsUnknown(reason="exception", detail=repr(exc))
            # mcp_list_tools may return [{"error": "..."}] instead of raising.
            # #3520: an error sentinel and NO usable tool is the same non-answer
            # as the two except-arms above wearing a different shape — reporting
            # an error is not reporting an empty catalog — so it maps to UNKNOWN
            # too. A response that carries real tools alongside a stray error
            # entry DID measure something, so it stays an answer with the
            # unusable entries dropped; only a wholly unusable error response is
            # a non-answer.
            raw = [t for t in (tools or []) if isinstance(t, dict)]
            cleaned = [t for t in raw if "error" not in t and t.get("name")]
            if not cleaned and any("error" in t for t in raw):
                self._events.emit(
                    "mcp_tool_probe_degraded",
                    server=server_name,
                    reason="exception",
                    per_server_timeout=per_server_timeout,
                    detail=repr(raw),
                )
                return server_name, ToolsUnknown(reason="exception", detail=repr(raw))
            return server_name, ToolsAnswered(tools=cleaned)

        results = await asyncio.gather(
            *(_probe_one(name) for name in unanswered),
            return_exceptions=False,  # _probe_one handles its own errors
        )
        new_answers = answered_only(results)
        if not new_answers:
            # Every probe came back unknown: nothing was measured, so there is
            # nothing to record. Rewriting the file here would only advance its
            # mtime and make every following turn reload an unchanged cache.
            return
        self._mcp_tools_cache = {**self._mcp_tools_cache, **new_answers}

        # FP-0037 S1: persist the live-probe result so subsequent sessions
        # and turns can warm-start from disk. Failures are opportunistic
        # and must NOT abort the session.
        try:
            write_cache(cache_path, self._mcp_tools_cache)
            self._mcp_tools_cache_mtime = file_mtime(cache_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ensure_mcp_tools_cached: could not write cache to %s: %r",
                cache_path, exc,
            )

    def invalidate_mcp_tools_cache(self, server: str) -> None:
        """Invalidate the lazy MCP tools cache (#2597 S2b) so the next
        ``ensure_mcp_tools_cached()`` re-probes every configured server.

        Called from the async notifications bridge
        (``reyn.mcp.message_handler.ReynMCPMessageHandler.on_tool_list_changed``) when
        a held MCP connection reports a server-pushed
        ``notifications/tools/list_changed`` for ``server`` — issue #160's lazy cache
        has no other way to learn the cached tool list may now be stale (it probes
        ONCE per session by design).

        Resets the WHOLE in-memory cache (not just ``server``'s entry). #3520 made
        ``ensure_mcp_tools_cached``'s guard per-server (it re-probes any configured
        server with no cached answer), so a targeted invalidation is now
        *expressible* — dropping one key would re-probe exactly that server. It is
        still not done here: a ``tools/list_changed`` notification says the sending
        server's catalog moved, and #2597 S2b's contract is the conservative one
        (full, bounded re-probe). Narrowing it is a behaviour change that belongs
        to whoever wants it, not a side effect of the #3520 type fix. ``server`` is
        accepted (not ``*args``) so the call site + a future targeted implementation
        both have a stable signature; unused beyond that today.

        Known interaction (not a regression, flagged for awareness): FP-0037 S1's
        on-disk warm-start cache (``<state_dir>/mcp_tools_cache.json``) is consulted
        BEFORE a live probe in ``ensure_mcp_tools_cached`` — if that file exists and
        wasn't itself refreshed, invalidation still warm-starts from it (stale)
        instead of forcing a live probe. This mirrors the disk-cache's own existing
        staleness window (``reyn mcp refresh`` is the operator-driven cache-buster)
        and is out of scope for #2597 S2b to close.
        """
        self._mcp_tools_cache = None
        self._mcp_tools_cache_mtime = None

    def maybe_reload_mcp_tools_cache_from_disk(self) -> None:
        """Reload the in-memory MCP tools cache if the on-disk file is newer.

        FP-0037 S1: called at each turn boundary (in Session before
        `ensure_mcp_tools_cached`). When the operator runs ``reyn mcp refresh``
        while a session is active, the cache file's mtime advances. This
        method detects that and hot-swaps the in-memory cache so the very next
        turn sees the refreshed tool list — no session restart required.

        Behaviour:
        - File absent or unreadable → no-op (silent).
        - File mtime unchanged since last load → no-op.
        - File mtime advanced → replace in-memory cache + update mtime record.
        Never raises.
        """
        from reyn.runtime.services.mcp_cache_file import (
            cache_file_path,
            file_mtime,
            read_cache,
        )

        cache_path = cache_file_path(self._state_dir)
        current_mtime = file_mtime(cache_path)
        if current_mtime is None:
            return
        if (
            self._mcp_tools_cache_mtime is not None
            and current_mtime <= self._mcp_tools_cache_mtime
        ):
            return
        fresh = read_cache(cache_path)
        if fresh is None:
            return
        self._mcp_tools_cache = fresh
        self._mcp_tools_cache_mtime = current_mtime

    @property
    def mcp_tools_cache_snapshot(self) -> dict[str, list[dict]] | None:
        """Read-only snapshot of the current in-memory MCP tools cache.

        FP-0037 S1: test-supporting public surface (per Tier policy
        [[feedback_tier_policy_strict_compliance]]). Returns a fresh dict
        so callers cannot mutate adapter internals through the returned dict.
        Returns None when the cache has not yet been populated.

        #3520: this projects the stored ``ToolsAnswered`` back to plain tool
        lists for readability, and that is safe here precisely because the
        stored type cannot hold a non-answer: every key present is a server
        that was measured, so ``[]`` in this snapshot means "measured, zero
        tools". A server that could not be measured is ABSENT — read a missing
        key as "unknown", never as "no tools".
        """
        if self._mcp_tools_cache is None:
            return None
        return {name: entry.tools for name, entry in self._mcp_tools_cache.items()}

    @property
    def yaml_mtimes_snapshot(self) -> dict[Path, float]:
        """Read-only snapshot of the last-seen yaml mtime table.

        FP-0037 S2: test-supporting public surface. Returns a shallow copy
        keyed by Path so callers can inspect which yaml files have been
        observed without touching adapter internals. Empty dict until the
        first call to maybe_refresh_mcp_tools_from_yaml.
        """
        return dict(self._yaml_mtimes_seen)

    async def maybe_refresh_mcp_tools_from_yaml(self) -> None:
        """Re-probe MCP servers and update the cache if any yaml config has changed.

        FP-0037 S2: called at each turn boundary BEFORE
        ``maybe_reload_mcp_tools_cache_from_disk`` so that yaml edits are
        caught, probed, and written to disk before the disk-reload step picks
        them up.

        Algorithm:
        1. Resolve the 3 yaml scope tier paths via ``yaml_scope_paths``.
        2. Stat each existing path and compare against ``_yaml_mtimes_seen``.
        3. If any mtime advanced (or a new yaml appeared): re-read MCP config
           from the yaml files, re-probe each server, write the cache file,
           and update ``_yaml_mtimes_seen``.
        4. On first call (``_yaml_mtimes_seen`` is empty): seed the mtime table
           without triggering a probe (= first-call "no diff" semantics).

        All failures (stat error, yaml parse error, probe error, cache write
        error) degrade silently — a warning is logged but the method never
        raises so the user-message hot path is not broken.
        """
        import asyncio

        from reyn.runtime.services.mcp_cache_file import (
            cache_file_path,
            write_cache,
            yaml_scope_paths,
        )

        try:
            yaml_paths = yaml_scope_paths(self._project_root)
        except Exception as exc:  # noqa: BLE001
            logger.warning("maybe_refresh_mcp_tools_from_yaml: yaml_scope_paths failed: %r", exc)
            return

        # --- Stat current mtimes (best-effort; missing files are silently skipped) ---
        current_mtimes: dict[Path, float] = {}
        for p in yaml_paths:
            try:
                mtime = p.stat().st_mtime
                current_mtimes[p] = mtime
            except OSError:
                # File does not exist or is unreadable — skip silently.
                pass

        # --- First call: seed the mtime table, no probe ---
        if not self._yaml_mtimes_seen:
            self._yaml_mtimes_seen = dict(current_mtimes)
            return

        # --- Detect changes: new file or advanced mtime ---
        changed = False
        for p, mtime in current_mtimes.items():
            prev = self._yaml_mtimes_seen.get(p)
            if prev is None or mtime > prev:
                changed = True
                break
        # Also detect files that appeared (= in current but not in seen)
        if not changed:
            new_paths = set(current_mtimes) - set(self._yaml_mtimes_seen)
            if new_paths:
                changed = True

        if not changed:
            return

        # --- Changed: re-read MCP server config from yaml files ---
        servers_flat: dict[str, dict] = {}
        try:
            servers_flat = self._read_mcp_servers_from_yaml(yaml_paths)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "maybe_refresh_mcp_tools_from_yaml: could not read yaml config: %r", exc,
            )
            # Still update mtime table so we don't hammer on every turn.
            self._yaml_mtimes_seen = dict(current_mtimes)
            return

        if not servers_flat:
            # No MCP servers in any yaml — write empty cache to advance mtime.
            try:
                cache_path = cache_file_path(self._state_dir)
                write_cache(cache_path, {})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "maybe_refresh_mcp_tools_from_yaml: cache write failed: %r", exc,
                )
            self._yaml_mtimes_seen = dict(current_mtimes)
            return

        # --- Re-probe servers in parallel (shared helper from CLI) ---
        from reyn.interfaces.cli.commands.mcp import _probe_server_tools

        async def _probe_all() -> dict[str, ToolsAnswered]:
            tasks = [
                _probe_server_tools(name, cfg)
                for name, cfg in servers_flat.items()
            ]
            results = await asyncio.gather(*tasks, return_exceptions=False)
            # #3520: a server whose probe did not answer is dropped, not written
            # as `[]`. It therefore has no entry after the disk-reload swap, and
            # the same turn's ensure_mcp_tools_cached() picks it up as
            # unanswered and live-probes it — self-healing instead of a
            # yaml edit permanently costing a slow server its tools.
            return answered_only(results)

        try:
            probe_results = await _probe_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "maybe_refresh_mcp_tools_from_yaml: probe failed: %r", exc,
            )
            self._yaml_mtimes_seen = dict(current_mtimes)
            return

        # --- Write updated cache to disk (= S1's disk-reload picks it up) ---
        try:
            cache_path = cache_file_path(self._state_dir)
            write_cache(cache_path, probe_results)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "maybe_refresh_mcp_tools_from_yaml: cache write failed: %r", exc,
            )

        # --- Update mtime table regardless of cache-write success ---
        self._yaml_mtimes_seen = dict(current_mtimes)

    @staticmethod
    def _read_mcp_servers_from_yaml(yaml_paths: "list[Path]") -> dict[str, dict]:
        """Read and merge MCP server configs from the given ordered yaml paths.

        Priority: later paths override earlier ones for the same server name
        (= local > project > user, following ``_all_servers_with_scope`` order).

        Returns a flat ``{server_name: cfg_dict}`` mapping.
        Never raises — yaml parse failures are logged and skipped.
        """
        merged: dict[str, dict] = {}
        for p in yaml_paths:
            if not p.exists():
                continue
            try:
                import yaml  # lazy import to avoid yaml dep at import time
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, dict):
                    continue
                servers = (raw.get("mcp") or {}).get("servers") or {}
                if not isinstance(servers, dict):
                    continue
                for name, cfg in servers.items():
                    merged[name] = cfg if isinstance(cfg, dict) else {}
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "_read_mcp_servers_from_yaml: could not parse %s: %r", p, exc,
                )
        return merged

    @property
    def hot_reloader(self) -> Any:
        """#2073 S3: this session's HotReloader, so a self-reload tool reloads
        THIS session and not a process global. RouterLoop reads it off the host
        (``getattr(host, "hot_reloader", None)``) and threads it onto the tool
        ctx. Delegates to the op-context supplier, which is the one holder."""
        return self._op_ctx_source.hot_reloader

    def make_router_op_context(self) -> Any:
        """Ask the session's op-context supplier for a chat-router OpContext.

        Public method (ADR-0026 Phase 3.5): the unified registry handlers in
        ``src/reyn/tools/`` reach op_runtime through this factory (bound as
        ``RouterCallerState.op_context_factory``), so the OpContext carries the
        operator-declared PermissionDecl and the Workspace with
        ``actor="chat_router"``. Without it, handlers would synthesize an empty
        ``PermissionDecl()`` and op_runtime's permission gates would deny.

        #3607: the adapter used to assemble the OpContext itself from 16
        injected materials, while ``Session`` assembled a DIFFERENT one (twelve
        fields apart) from the same values held as its own attributes. This is
        now the same supplier the Session uses, so the two cannot diverge.
        """
        return self._op_ctx_source.build()

    def _set_cancel_event(self, event: asyncio.Event) -> None:
        """#1470: called by RouterLoopDriver at construction to register the
        per-turn cancel event, which the op-context supplier threads into every
        OpContext it builds so sandboxed_exec backends can observe
        cancel_inflight() mid-subprocess. Registered on the SUPPLIER, not
        copied here: ``_mcp_list_via_gateway`` needs the same event, and two
        holders of one value is the drift shape #3607 removed.
        """
        self._op_ctx_source.set_cancel_event(event)

    def make_intervention_bus(self) -> "Any | None":
        """Return the current intervention bus for safety-limit checkpoints.

        Called by RouterLoop when max_iterations is reached and
        safety.on_limit.mode=interactive. Returns None when no bus is
        wired (headless / test stubs) → limit degrades to unattended.
        """
        if self._intervention_bus_factory is None:
            return None
        return self._intervention_bus_factory()


# ---------------------------------------------------------------------------
# #3482 N+1 gate — the registries below hold ONLY what a measurement cannot
# settle.
#
# The question "may this param stay a bare scalar?" has a computable answer:
# is any OTHER param carried to exactly the same set of destinations? So it is
# COMPUTED, by scripts/measure_router_host_adapter_consumers.py, and enforced
# by tests/runtime/test_router_host_adapter_param_gate_3482.py:
#
#   * bare param that acquires an exact-match partner  -> RED (bundle them)
#   * bare param with no measurable consumer           -> RED unless shelved
#                                                         below with a reason
#   * a shelved reason contradicted by measurement     -> RED
#
# The first #3482 pass instead wrote 58 per-param prose reasons, most asserting
# "no shared-consumer partner", behind a gate that only checked the reasons
# were non-empty. Six were measurably false (delegation_tracker/send_to_agent,
# put_outbox/agent_replies_tracker, session_id/live_session_id_fn — each pair
# an exact consumer-set match, now the three bundles above). A declaration's
# EXISTENCE was standing in as the witness for its TRUTH; deriving the
# predicate is the fix, and deleting the prose is part of the fix, because
# prose that restates a computable fact can only rot.
# ---------------------------------------------------------------------------

ROUTER_HOST_ADAPTER_BUNDLE_TYPES: "tuple[str, ...]" = (
    "McpGatewayInputs",
    "PutOutboxInputs",
    "LiveSessionIdInputs",
)

# Bare params for which the static measurement finds NO consumer at all.
# "Not measurable" is NOT "has no partner": a dynamic ``getattr(host, ...)``
# read, a test-only surface, or a hole in the scan all look identical from
# here, so the value of an entry is the part a scan CANNOT supply — what is
# actually known about the param. The gate checks this registry against the
# measurement in BOTH directions (a param that gains a consumer must leave,
# and a param that loses its last consumer must be added).
ROUTER_HOST_ADAPTER_CONSUMER_UNMEASURED: "dict[str, str]" = {
    "journal": (
        "Stored on self._journal and read nowhere the scan can see: no adapter "
        "member reads it, and no host-surface read of `journal`/`_journal` "
        "exists anywhere under src/reyn. Session wires a real SnapshotJournal "
        "and nothing records whether the handle is kept deliberately (a "
        "reserved surface) or was simply left behind, and code cannot "
        "distinguish 'forgotten' from 'decided' — so it is shelved as "
        "unmeasured rather than declared partnerless. Removing the param is a "
        "Session-construction decision, not a #3482 measurement one."
    ),
}

# Bare params that DO have an exact-match partner but must not be bundled, for
# a reason no measurement can produce. Empty on purpose: every cluster the
# measurement currently finds is bundled. An entry here is a claim the gate
# checks — if the named param has no measured partner, the exception marker is
# dead and goes RED (#3457's "drop the exception that never fires" arm, which
# applies here precisely because a reason is a CLAIM, not a value).
ROUTER_HOST_ADAPTER_BUNDLE_BLOCKED: "dict[str, str]" = {}
