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
}
