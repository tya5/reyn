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
}
