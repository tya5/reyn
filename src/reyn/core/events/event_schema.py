"""Event schema registry — declares required fields per event kind.

Used by Tier 2 invariant tests to enforce audit completeness (FP-0021).
NOT enforced at emit() runtime (to keep production overhead zero); each
feature's invariant test asserts that its event kinds are declared here
with the required fields.

P7 note: kind names here are OS-level event kinds, not domain-specific
identifiers, so this file stays within the OS layer's allowed vocabulary.
"""

from __future__ import annotations

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
    # MCP tool-search deferred loading (chat/router_tools.py — FP-0024 Component D)
    # Emitted by the router when the LLM invokes the tool_search_tool meta-tool.
    # mcp_search_invoked: LLM called tool_search; query + result count recorded.
    # mcp_tool_loaded: a specific MCP tool was loaded from a search result.
    "mcp_search_invoked": frozenset({"query", "result_count"}),
    "mcp_tool_loaded": frozenset({"tool_name", "server_name"}),
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
