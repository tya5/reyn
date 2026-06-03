"""turn_budget — cumulative-axis (current-turn) context bound for chat/plan/phase.

The third growth axis (#1092): compaction bounds the PAST (finished turns) and
per-term cap/offload bounds each SINGLE item; this service bounds the
CUMULATIVE size of the CURRENT turn. When the working context approaches its
limit, the OS force-closes the current turn (elicits a clean ``finish``, not a
truncate) and hands off a consolidated checkpoint to a fresh continuation.

This package is the sibling of ``services/compaction/`` and ``services/offload/``
(the other two axes). PR-A (this foundation) provides only the axis-independent
wrap-up system prompt and the headroom computation; the per-turn trigger hook,
the force-close call, and the handoff persist/re-entry land in later PRs and are
wired through the shared ``RouterLoop`` (chat/plan/phase all route through it).
"""
from reyn.services.turn_budget.engine import (
    TurnBudget,
    TurnBudgetEngine,
    assert_turn_budget_bounds,
    compute_turn_budget,
    wrap_up_system_prompt,
)

__all__ = [
    "TurnBudget",
    "TurnBudgetEngine",
    "assert_turn_budget_bounds",
    "compute_turn_budget",
    "wrap_up_system_prompt",
]
