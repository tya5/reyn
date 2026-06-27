"""Event schema registry — declares required fields per event kind.

Used by Tier 2 invariant tests to enforce audit completeness (FP-0021).
NOT enforced at emit() runtime (to keep production overhead zero); the
test in tests/test_event_audit_invariants.py validates that all listed
events carry the declared required fields.

P7 note: kind names here are OS-level event kinds, not skill-specific
identifiers, so this file stays within the OS layer's allowed vocabulary.
"""

from __future__ import annotations

# Events that must carry these audit fields (FP-0021)
EVENT_AUDIT_REQUIREMENTS: dict[str, frozenset[str]] = {
    # Workflow lifecycle (runtime.py)
    "workflow_started": frozenset({"run_id", "skill"}),
    "workflow_finished": frozenset({"run_id", "skill"}),
    # LLM call lifecycle (runtime.py)
    "llm_called": frozenset({"run_id", "skill"}),
    "llm_response_received": frozenset({"run_id", "skill"}),
    # Permission events (op_runtime/__init__.py)
    "permission_granted": frozenset({"run_id", "skill", "phase"}),
    "permission_denied": frozenset({"run_id", "skill", "phase"}),
    # User intervention (op_runtime/ask_user.py)
    "user_intervention_requested": frozenset({"run_id", "skill", "intervention_id"}),
    "user_intervention_received": frozenset({"run_id", "skill", "intervention_id"}),
    # MCP tool-search deferred loading (chat/router_tools.py — FP-0024 Component D)
    # Emitted by the router when the LLM invokes the tool_search_tool meta-tool.
    # mcp_search_invoked: LLM called tool_search; query + result count recorded.
    # mcp_tool_loaded: a specific MCP tool was loaded from a search result.
    "mcp_search_invoked": frozenset({"query", "result_count"}),
    "mcp_tool_loaded": frozenset({"tool_name", "server_name"}),
    # FP-0034 Phase 3: Universal catalog routing decision (Self-improvement Loop)
    # Emitted by RouterLoop when invoke_action or a hot list alias is executed.
    # action_name: the resolved qualified_name (e.g. "skill__code_review")
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
    #   this turn where the LLM did try a non-catalog tool (e.g. list_skills).
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
    #   "skill_completed", "task_ready"). Lets subscribers distinguish human
    #   triggers from automated ones without parsing the payload.
    "turn_started": frozenset({"kind"}),
    # turn_completed: emitted in Session._run_router_loop() immediately after
    #   RouterLoopDriver.run_turn() returns — the router loop has reached a
    #   terminal condition (the turn's response is complete). One emit per
    #   turn, independent of skill/routing path. This is the hook point for
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
