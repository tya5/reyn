"""Audit-event schema registries — the kind vocabulary, and the fields per kind.

Two registries with two DIFFERENT responsibilities live here. Keeping them in
one module is deliberate (one place to look for "what is an audit-event"), but
they are not interchangeable and one is not derived from the other:

1. ``AUDIT_EVENT_KINDS`` — **the vocabulary**. Which audit-event kinds exist at
   all. This is the closed set (#3410): an audit-event's ``type`` is a public
   interface, because reyn is not the only consumer of ``.reyn/events``, and an
   external subscriber must be able to enumerate the kinds it may receive. An
   open namespace makes that impossible in principle, so the namespace is
   closed here and gated in ``tests/test_audit_event_kind_vocabulary_3410.py``
   in both directions (nothing emits an undeclared kind; nothing is declared
   without a producer).

2. ``EVENT_AUDIT_REQUIREMENTS`` — **the field requirements**. Given a kind, what
   its payload must carry (FP-0021). It covers a SUBSET of the vocabulary and
   says nothing about which kinds exist — the distinction that let
   ``mcp_search_invoked`` / ``mcp_tool_loaded`` sit here with required fields
   while reyn had no point at which it could ever emit them (see the decision
   record next to their former entries), and let ``mcp_resources_listed`` /
   ``mcp_prompts_listed`` ship without appearing here at all.

``KIND_EMIT_SEAMS`` and ``DYNAMIC_KIND_EMIT_SITES`` support (1): they declare
where kinds enter the log, and where the AST census that guards the vocabulary
is structurally blind.

Neither registry is enforced at ``emit()`` runtime (production overhead stays
zero) — the gates are CI-side.

P7 note: kind names here are OS-level event kinds, not domain-specific
identifiers, so this file stays within the OS layer's allowed vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── RETIRED_PHASE_FIELD — decision record (#2696) ────────────────────────────
# Several audit-events below declare a mandatory ``phase`` field. The phase
# engine that populated it was deleted (#2434 / #2438).
#
# DECISION: the field is RETAINED, and its value is ALWAYS the empty string.
#   - Retained because it is a persisted, CI-checked audit-event schema:
#     dropping it from ``EVENT_AUDIT_REQUIREMENTS`` would make every event file
#     reyn has already written fail to replay against the current schema, and
#     ``reyn events replay`` is the audit trail (P6 / band member).
#   - Always empty because there is nothing left to name: the #2696 drift-audit
#     also deleted ``OpContext.current_phase`` (which defaulted to ``""`` and
#     was never passed a non-empty value by any caller in ``src/``), so emit
#     sites now pass THIS constant literally rather than reading a field whose
#     existence implied a live phase concept.
#
# Do NOT "wire this up" — there is no producer to reconnect to. A non-empty
# value would require a NEW runtime concept, which is a design decision, not a
# repair. If such a concept ever lands, name it accurately instead of reviving
# ``phase``.
RETIRED_PHASE_FIELD = ""

# Events that must carry these audit fields (FP-0021)
EVENT_AUDIT_REQUIREMENTS: dict[str, frozenset[str]] = {
    # LLM cost events (llm.py _emit_chat_cost_events — cost-tab observability).
    # Minimal fields: llm_called carries model, llm_response_received carries the
    # usage/cost figures. agent is derived from the events file path (not an event
    # field), and run_id is not threaded on this path.
    "llm_called": frozenset({"model"}),
    # ``usage_source`` (#3351) is MANDATORY, not optional: a provider-reported
    # count and a ``litellm.token_counter`` estimate are the same int, and the
    # audit trail is where an estimated turn has to be identifiable after the
    # fact. Requiring it here means the cost events cannot report figures
    # without reporting their origin.
    "llm_response_received": frozenset(
        {"prompt_tokens", "completion_tokens", "cost_usd", "usage_source"}
    ),
    # Permission events (op_runtime/__init__.py). ``phase`` is mandatory for
    # replay compatibility and always ``RETIRED_PHASE_FIELD`` — see the decision
    # record at the top of this module before touching it.
    "permission_granted": frozenset({"run_id", "actor", "phase"}),
    "permission_denied": frozenset({"run_id", "actor", "phase"}),
    # User intervention (op_runtime/ask_user.py)
    "user_intervention_requested": frozenset({"run_id", "actor", "intervention_id"}),
    "user_intervention_received": frozenset({"run_id", "actor", "intervention_id"}),
    # (#3410) ``mcp_search_invoked`` / ``mcp_tool_loaded`` were declared here for
    # the FP-0024 tool_search meta-tool and REMOVED — not because nobody got
    # around to writing the emitter, but because **reyn has no point at which it
    # could observe either event**. Both were measured before removing:
    #
    #   - ``tool_search`` is Anthropic's SERVER-SIDE meta-tool
    #     (``tool_search_tool_20251101``). reyn hands the provider a catalog;
    #     the provider matches the query and loads the subset itself. The search
    #     call never returns to reyn as a tool call — the literal
    #     ``"tool_search"`` appears in ``src/reyn`` exactly twice, in
    #     ``router_tools.build_mcp_search_tool``'s descriptor and the comment
    #     above it. There is no dispatch arm to emit from, for either the search
    #     ("mcp_search_invoked") or the per-tool load ("mcp_tool_loaded").
    #   - Nor is the arm reachable today: it needs ``mcp_search_threshold > 0``,
    #     ``MCP_SEARCH_THRESHOLD`` is 0, the ``ReynConfig`` field was
    #     fold-removed (#3218), and neither production ``build_tools()`` caller
    #     (``router_loop`` / ``capability_visibility``) passes the parameter.
    #
    # ★ Contrast with the ``_mcp_list_*`` decision this same change made in the
    # other direction (session.py, #3410): there, reyn ITSELF performs the
    # listing, so an observation point exists and "emitting is the recoverable
    # direction" applies. Here it does not — the event happens inside the
    # provider. A declared kind reyn cannot produce is the #3357 defect, so the
    # honest move is to drop the declaration, not to invent an emit site.
    #
    # Reinstating them requires an observation point first (e.g. a provider
    # response field naming which tools a search loaded), not just an emitter.
    # FP-0034 Phase 3: Universal catalog routing decision (Self-improvement Loop)
    # Emitted by RouterLoop when invoke_action or a hot list alias is executed.
    # action_name: the resolved qualified_name (e.g. "agent.peer__alice")
    # source: how the routing happened ("invoke_action" | "hot_list_alias")
    # outcome: "success" | "error" based on the tool result status
    # chain_id: for cross-agent tracing (P6)
    "routing_decided": frozenset({"action_name", "source", "outcome", "chain_id"}),
    # FP-0034 B28-Q2 Case A: Inline LLM reply (no catalog dispatch in this turn).
    # Emitted by RouterLoop when the turn ends with a text reply and no
    # routing_decided event was emitted in the same turn.  Mutually exclusive
    # with routing_decided per turn.
    # decision: "inline_reply" — LLM answered conversationally without invoking
    #   any catalog-dispatched tool.  "clarification_asked" and "decline" are
    #   reserved in the schema for future analytics but NOT emitted by the router
    #   yet (both collapse to "inline_reply" at emit time).
    # tool_calls_attempted: count of tool_call rounds in earlier iterations of
    #   this turn where the LLM did try a non-catalog tool (e.g. an unknown tool name).
    # chain_id: for cross-agent tracing (P6)
    "chat_turn_completed_inline": frozenset({"chain_id", "decision", "tool_calls_attempted"}),
    # #1800 slice 5a: session + turn lifecycle events (P6 audit — hook dispatch
    # points added in slice 5b).
    #
    # session_started / session_completed: emitted in Session.run() alongside
    #   chat_started / chat_stopped. Marks the boundary of the session's
    #   resource scope (F in the lifecycle hook design).
    # agent_name: the session's agent identity (same field as chat_started).
    "session_started": frozenset({"agent_name"}),
    "session_completed": frozenset({"agent_name"}),
    # turn_started: emitted in Session.run_one_iteration() after the trigger
    #   is consumed from the inbox and before dispatch to _handle_*.
    # kind: the inbox message kind that triggered this turn (e.g. "user",
    #   "agent_response", "task_ready"). Lets subscribers distinguish human
    #   triggers from automated ones without parsing the payload.
    "turn_started": frozenset({"kind"}),
    # turn_completed: emitted in Session._run_router_loop() immediately after
    #   RouterLoopDriver.run_turn() returns — the router loop has reached a
    #   terminal condition (the turn's response is complete). One emit per
    #   turn, independent of routing path. This is the hook point for
    #   the turn_end lifecycle hook (slice 5b). chain_id matches the turn's
    #   chain_id for cross-agent tracing.
    "turn_completed": frozenset({"chain_id"}),
    # turn_settled: emitted in Session.run_one_iteration()'s finally for EVERY
    #   turn kind (including slash / intervention short-circuits that return
    #   before the router). Unlike turn_completed (router path only), this is the
    #   reliable "the turn is done" signal for UI working-indicators. kind mirrors
    #   turn_started; chain_id may be absent for non-user triggers.
    "turn_settled": frozenset({"kind"}),
}


# ═══════════════════════════════════════════════════════════════════════════
# 1. THE VOCABULARY — which audit-event kinds exist (#3410)
# ═══════════════════════════════════════════════════════════════════════════
#
# CLOSED SET. An audit-event's ``type`` is a public interface: ``.reyn/events``
# is consumed outside reyn, and an external subscriber has to be able to
# enumerate every kind it might receive. That is impossible against an open
# namespace, so the namespace is closed here.
#
# Adding an audit-event kind is a three-line change: emit it, add it here, add
# it to the enumeration in ``docs/reference/runtime/events.md``. The gate
# (``tests/test_audit_event_kind_vocabulary_3410.py``) fails on any two of the
# three without the third — in BOTH directions, so a kind cannot be emitted
# without being declared, and cannot be declared without a producer.
#
# This list is NOT a hand-maintained mirror of the source: the gate derives the
# emitted set by AST census and asserts equality, so the only way to edit this
# list correctly is to make the code agree with it (or vice versa). What it adds
# over the census is reviewability — a diff here is a change to a public
# interface, visible as such in a PR.
AUDIT_EVENT_KINDS: frozenset[str] = frozenset({
    "agent_delta",
    "agent_message_refused",
    "agent_message_sent",
    "agent_request_received",
    "agent_response_received",
    "asyncio_unhandled_exception",
    "body_summary_hard_truncated",
    "budget_reset",
    "bus_subscriber_dropped",
    "canonical_degraded",
    "canonical_fallback_used",
    "chain_peer_discarded",
    "chain_timeout",
    "chain_timeout_extended",
    "chat_started",
    "chat_stopped",
    "chat_turn_completed_inline",
    "client_attached",
    "client_detached",
    "client_seized",
    "compact_op_completed",
    "compact_op_failed",
    "compact_op_requested",
    "compact_op_unavailable",
    "compaction_check",
    "compaction_completed",
    "compaction_failed",
    "compaction_started",
    "composer_dropped",
    "composer_fired",
    "config_reload_rejected",
    "config_reloaded",
    "control_ir_failed",
    "control_ir_skipped",
    "direct_alias_call_salvaged",
    "elide_evaluated",
    "embed_attempts",
    "embed_cancelled",
    "embed_secret_redacted",
    "embedding_index_build_complete",
    "embedding_index_build_error",
    "embedding_index_build_progress",
    "embedding_index_build_started",
    "exec_threat_blocked",
    "exec_threat_match",
    "file_read_media_denied",
    "force_close_triggered",
    "hook_event_emitted",
    "hook_push_fired",
    "hook_shell_executed",
    "hot_list_updated",
    "inbox_cancel",
    "index_dropped",
    "index_update_cost_warning",
    "index_updated",
    "intervention_answer_submitted",
    "intervention_denied",
    "intervention_routed",
    "limit_denied",
    "llm_call_retry",
    "llm_call_retry_exhausted",
    "llm_called",
    "llm_request",
    "llm_request_error",
    "llm_response_received",
    "mcp_called",
    "mcp_cancelled",
    "mcp_completed",
    "mcp_elicitation_answered",
    "mcp_elicitation_auto_declined",
    "mcp_elicitation_requested",
    "mcp_elicitation_timed_out",
    "mcp_failed",
    "mcp_initialized",
    "mcp_install_cancelled",
    "mcp_install_probe_failed",
    "mcp_install_threat_blocked",
    "mcp_install_threat_match",
    "mcp_progress",
    "mcp_prompt_get",
    "mcp_prompt_get_cancelled",
    "mcp_prompt_get_completed",
    "mcp_prompt_get_failed",
    "mcp_prompt_list_changed",
    "mcp_prompts_listed",
    "mcp_resource_read",
    "mcp_resource_read_cancelled",
    "mcp_resource_read_completed",
    "mcp_resource_read_failed",
    "mcp_resource_subscribe",
    "mcp_resource_subscribe_cancelled",
    "mcp_resource_subscribe_failed",
    "mcp_resource_subscribed",
    "mcp_resource_templates_listed",
    "mcp_resource_unsubscribe",
    "mcp_resource_unsubscribe_cancelled",
    "mcp_resource_unsubscribe_failed",
    "mcp_resource_unsubscribed",
    "mcp_resource_updated",
    "mcp_resources_listed",
    "mcp_server_installed",
    "mcp_server_removed",
    "mcp_tool_list_changed",
    "mcp_tools_listed",
    "memory_deleted",
    "memory_saved",
    "model_budget_fallback",
    "model_cost_block",
    "model_cost_warn",
    "network_ssl_verify_disabled",
    "new_msg_exceeds_budget",
    "oauth_login_completed",
    "oauth_login_started",
    "peer_reply_failed_surfaced",
    "pending_intervention_claimed",
    "pending_intervention_discarded",
    "permission_denied",
    "permission_granted",
    "pipeline_install_threat_blocked",
    "pipeline_install_threat_match",
    "pipeline_installed",
    "pipeline_load_failed",
    "pipeline_run_attached",
    "pipeline_step_completed",
    "pipeline_step_started",
    "plan_step_llm_memoized",
    "plugin_install_completed",
    "plugin_install_copied",
    "plugin_install_reconciled",
    "plugin_install_registered",
    "plugin_install_started",
    "plugin_uninstall_completed",
    "plugin_uninstall_registry_dropped",
    "plugin_uninstall_started",
    "presentation_install_blocked",
    "presentation_installed",
    "presentation_load_failed",
    "presented",
    "router_context_overflow_detected",
    "router_context_overflow_unrecovered",
    "router_empty_response_detected",
    "router_empty_response_retry_injected",
    "router_force_close_handoff",
    "router_loop_terminated_by_exception",
    "router_represent_round",
    "router_retry_exhausted",
    "routing_decided",
    "safety_limit_checkpoint",
    "sandbox_policy_narrowed",
    "sandbox_policy_not_applied",
    "sandboxed_exec_cancelled",
    "sandboxed_exec_completed",
    "sandboxed_exec_started",
    "secret_cleared",
    "secret_rotated",
    "secret_set",
    "semantic_search_complete",
    "semantic_search_embed_failed",
    "semantic_search_started",
    "session_completed",
    "session_halted",
    "session_restored",
    "session_started",
    "skill_body_loaded",
    "skill_install_threat_blocked",
    "skill_install_threat_match",
    "skill_installed",
    "skill_invoke_body_loaded",
    "skill_invoke_collision",
    "state_change_notified",
    "summary_resummarize_failed",
    "summary_resummarized",
    "threat_block",
    "threat_scan_match",
    "token_refresh_failed",
    "token_refreshed",
    "tool_call_cap_exceeded",
    "tool_call_deduped",
    "tool_called",
    "tool_cycle_kept_whole_over_budget",
    "tool_executed",
    "tool_failed",
    "tool_result_offloaded",
    "tool_returned",
    "turn_cancelled",
    "turn_completed",
    "turn_settled",
    "turn_started",
    "turn_too_large_truncated",
    "untrusted_narrowing_engaged",
    "user_answered_intervention",
    "user_intervention_received",
    "user_intervention_requested",
    "user_message_received",
    "user_submitted",
    "web_fetch_completed",
    "web_fetch_failed",
    "web_fetch_media_denied",
    "web_fetch_ssrf_blocked",
    "web_fetch_started",
    "web_fetch_too_large",
    "web_fetch_too_many_redirects",
    "web_search_completed",
    "web_search_failed",
    "web_search_started",
    "workspace_updated",
})


# ── Where kinds enter the log ────────────────────────────────────────────────
# The call shapes the vocabulary census reads. Name → the KEYWORD carrying the
# kind, or ``None`` when the kind is the first positional argument.
#
# Most entries are ``EventLog.emit``-shaped ``(kind, **data)`` callables; the
# ``_emit*`` / ``*_sink`` names are injected sinks and thin forwarders that take
# the kind from their own caller. Registering a name is what makes the kinds
# flowing through it visible to the census — which is why the gate does not
# trust this list to be complete on its own: it also derives, from the AST,
# every function that forwards a parameter into a seam's kind slot, and fails if
# such a function is not registered here. A hand-written registry covers the
# seams someone remembered; the derivation covers the rest.
KIND_EMIT_SEAMS: dict[str, str | None] = {
    # The primary seam: ``EventLog.emit(kind, **data)``.
    "emit": None,
    # ``emit_cli_event(kind, **payload)`` — audit emit from a CLI entry point
    # that has no session (writes to ``.reyn/events/direct/cli``).
    "emit_cli_event": None,
    # ``Session.emit_audit_event(event_type, **data)`` — the narrow public seam
    # the AG-UI transport records surface-lifecycle attribution through.
    "emit_audit_event": None,
    # Injected ``(kind, **data)`` sinks: the hook bus / dispatcher / composer and
    # the MCP connection service + message handler are handed a callable rather
    # than an EventLog, so their emits never name ``emit`` at the call site.
    "emit_event": None,
    "_emit_event": None,
    "emit_sink": None,
    "_emit_sink": None,
    # Local best-effort wrappers around one of the above (index coordinator, MCP
    # elicitation / message handler, hook composer). ``_emit`` is also the name
    # of an unrelated tree-walk helper in ``interfaces/common/branch_tree.py``;
    # that collision is declared in DYNAMIC_KIND_EMIT_SITES rather than special-
    # cased here, so it stays visible.
    "_emit": None,
    "_audit": None,
    # ``Session._mcp_list_via_gateway(..., event_kind=...)`` — the shared MCP
    # listing seam. The kind rides a keyword and every call site passes a
    # literal (#3410); before that it was assembled as ``f"mcp_{noun}_listed"``,
    # which no census could read.
    "_mcp_list_via_gateway": "event_kind",
}


@dataclass(frozen=True)
class DynamicEmitSite:
    """One audit-emit call site whose kind the vocabulary census cannot read.

    A gate cannot close a namespace through a call site that builds its kind at
    runtime. Declaring each such site — with a classification and a reason —
    turns "the census might be blind somewhere" into an enumerable list that
    cannot grow without a decision.

    Classifications:

    - ``FORWARDER`` — the enclosing function is itself a registered seam; the
      kind came from a caller the census already reads. No kind is minted here.
    - ``SINK_BINDING`` — a lambda/closure binding a registered sink parameter to
      a real ``EventLog``. Same reasoning as FORWARDER, one level of injection
      further out: the kinds are censused at the sink's own call sites.
    - ``KIND_FAMILY`` — the kind is genuinely assembled at runtime, so the
      census cannot expand it. These are the real holes in the closed vocabulary
      and each one needs a reason it still exists.
    - ``NOT_AN_AUDIT_EMIT`` — a name collision with a seam name; the call does
      not touch the audit log at all.
    """

    module: str
    function: str
    seam: str
    classification: str
    reason: str

    CLASSIFICATIONS = ("FORWARDER", "SINK_BINDING", "KIND_FAMILY", "NOT_AN_AUDIT_EMIT")


# ── The census's blind spots, enumerated ─────────────────────────────────────
# Keyed by (module, enclosing function, seam) — not by line number, which is not
# an identifier across a moving ``main``.
DYNAMIC_KIND_EMIT_SITES: tuple[DynamicEmitSite, ...] = (
    DynamicEmitSite(
        module="src/reyn/core/events/events.py",
        function="emit_cli_event",
        seam="emit",
        classification="FORWARDER",
        reason=(
            "``emit_cli_event`` is itself a registered seam; its own call sites "
            "pass literal kinds and are censused there."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/data/index/coordinator.py",
        function="_emit",
        seam="emit",
        classification="FORWARDER",
        reason=(
            "IndexCoordinator's best-effort wrapper (never raises, so a broken "
            "sink cannot fail a build). Registered seam; its five call sites "
            "pass literal kinds."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/hooks/composer.py",
        function="_audit",
        seam="_emit_event",
        classification="FORWARDER",
        reason=(
            "Composer's metadata-only audit wrapper over the injected hook "
            "sink. Registered seam; its call sites pass literal kinds."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/mcp/elicitation.py",
        function="_emit",
        seam="emit_sink",
        classification="FORWARDER",
        reason=(
            "The elicitation handler's local emit closure over the injected "
            "sink. Registered seam; its call sites pass literal kinds."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/mcp/message_handler.py",
        function="_emit",
        seam="_emit_sink",
        classification="FORWARDER",
        reason=(
            "MCP message handler's guarded wrapper over the injected sink. "
            "Registered seam; its call sites pass literal kinds."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/runtime/session.py",
        function="emit_audit_event",
        seam="emit",
        classification="FORWARDER",
        reason=(
            "The public Session seam the AG-UI transport emits through. "
            "Registered seam; its call sites pass literal kinds."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/runtime/session.py",
        function="_mcp_list_via_gateway",
        seam="emit",
        classification="FORWARDER",
        reason=(
            "The shared MCP listing seam. Registered with ``event_kind`` as its "
            "kind keyword; all four ``_mcp_list_*`` call sites pass a literal "
            "(#3410)."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/runtime/session.py",
        function="_build_hook_event_bundle",
        seam="emit",
        classification="SINK_BINDING",
        reason=(
            "``lambda et, **d: self._chat_events.emit(et, **d)`` handed to the "
            "HookBus / HookDispatcher / Composer as their ``emit_event`` sink. "
            "The kinds are censused at those components' own emit sites."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/runtime/session.py",
        function="_build_mcp_connection_service",
        seam="emit",
        classification="SINK_BINDING",
        reason=(
            "The same lambda shape handed to MCPConnectionService as its "
            "``emit_sink``; censused at that component's own emit sites."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/runtime/session.py",
        function="_embedding_event_sink",
        seam="emit",
        classification="KIND_FAMILY",
        reason=(
            "``f\"embedding_{kind}\"`` — a real hole, and currently a hole with "
            "nothing behind it. The sink is only invoked by an embedding "
            "provider that accepts an ``event_sink`` kwarg, and no provider in "
            "the repo does: ``reyn.data.embedding.get_provider`` passes it only "
            "when the class's signature accepts it, and the sole implementation "
            "(``LiteLLMEmbeddingProvider``) documents that it does not. So no "
            "``embedding_*`` kind is emitted today, and none is declared in "
            "AUDIT_EVENT_KINDS. Wiring a provider that DOES report lifecycle "
            "means deciding the kind names first — pass them as literals then. "
            "Tracked in #3438: the threading is either removed, or made real "
            "with literal kind names. This registry entry should not outlive "
            "that decision — a permanently-registered hole in a closed "
            "vocabulary is a contradiction."
        ),
    ),
    # Two entries for one helper: the recursion inside it, and the call that
    # starts the recursion — different enclosing functions, so different keys.
    DynamicEmitSite(
        module="src/reyn/interfaces/common/branch_tree.py",
        function="_emit",
        seam="_emit",
        classification="NOT_AN_AUDIT_EMIT",
        reason=(
            "A local recursive tree-walk helper that happens to be named "
            "``_emit``; it appends rendered rows and never touches an event "
            "log. Declared rather than filtered out by receiver-name "
            "heuristics, so the collision stays visible to the next reader."
        ),
    ),
    DynamicEmitSite(
        module="src/reyn/interfaces/common/branch_tree.py",
        function="build_branch_tree_rows",
        seam="_emit",
        classification="NOT_AN_AUDIT_EMIT",
        reason=(
            "The call that seeds the tree-walk helper above. Same collision, "
            "same non-audit call."
        ),
    ),
)
