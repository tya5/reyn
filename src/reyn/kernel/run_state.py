"""RunState — mutable state for one OSRuntime.run() invocation.

Extracted from OSRuntime (FP-0020 Component A). All run-scope mutable
state lives here as a dataclass, threaded through PhaseExecutor /
LLMCallRecorder / RunOrchestrator in subsequent components (B/C/D).

Following the RollbackState pattern (runtime.py L125-221):
- Dataclass with default_factory for collections.
- ~12 small methods that mutate state explicitly (no events emitted).
- Callers thread ``state`` parameter; no global state.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from reyn.kernel.rollback_state import RollbackState
from reyn.llm.pricing import TokenUsage

if TYPE_CHECKING:
    pass


@dataclass
class RunState:
    """Mutable state for one OSRuntime.run() invocation.

    Fields map 1-to-1 to the former ``OSRuntime`` instance variables they
    replaced. No events are emitted here — the OS layer retains that
    responsibility.
    """

    # Navigation (RunOrchestrator owns)
    visit_counts: dict[str, int] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    prev_phase: str | None = None
    rollback: RollbackState = field(default_factory=RollbackState)

    # Per-phase lifecycle (reset by begin_phase())
    phase_started_at: float | None = None
    llm_call_idx_in_phase: int = 0

    # Run-level accumulators
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    total_cost_usd: float = 0.0

    # Safety extensions (FP-0005)
    safety_extensions: dict[str, float] = field(default_factory=dict)

    # Trusted input (PR33)
    skill_input: dict | None = None

    # ── Navigation / phase lifecycle ────────────────────────────────────────

    def begin_phase(self, phase: str) -> int:
        """Increment visit_counts[phase], reset per-phase counters.

        Returns the new visit count for the phase (after increment).
        Mirrors the three-statement block at OSRuntime._enter_phase
        (original L438, L441, L446).
        """
        count = self.visit_counts.get(phase, 0) + 1
        self.visit_counts[phase] = count
        self.phase_started_at = time.monotonic()
        self.llm_call_idx_in_phase = 0
        return count

    def next_llm_invocation_id(self, phase: str) -> str:
        """Return the next LLM op_invocation_id string and increment the counter.

        Pattern: ``"{phase}.llm.{idx}"`` — mirrors original L744-L745.
        """
        idx = self.llm_call_idx_in_phase
        self.llm_call_idx_in_phase += 1
        return f"{phase}.llm.{idx}"

    def reset_phase_clock(self) -> None:
        """Set phase_started_at to now (used on budget extension approval)."""
        self.phase_started_at = time.monotonic()

    def elapsed_phase_seconds(self) -> float:
        """Seconds elapsed since phase_started_at, or 0.0 if not set."""
        if self.phase_started_at is None:
            return 0.0
        return time.monotonic() - self.phase_started_at

    def record_transition(self, from_phase: str, to_phase: str) -> None:
        """Append a history entry and update prev_phase."""
        self.history.append(f"{from_phase} → {to_phase}")
        self.prev_phase = from_phase

    # ── Cost / usage accumulators ────────────────────────────────────────────

    def add_usage(self, usage: TokenUsage, cost_usd: float | None) -> None:
        """Accumulate LLM token usage and cost into run totals.

        Mirrors the two-statement pattern repeated across _call_llm_and_record,
        _credit_budget_from_memo, _run_skill_node, _finish_workflow, etc.
        """
        self.token_usage += usage
        if cost_usd is not None:
            self.total_cost_usd += cost_usd

    # ── Safety extensions (FP-0005) ──────────────────────────────────────────

    def grant_extension(self, kind: str, amount: float) -> None:
        """Record a safety-limit extension grant (FP-0005).

        Mirrors original L390-L391 inside _handle_limit_checkpoint.
        """
        self.safety_extensions[kind] = (
            self.safety_extensions.get(kind, 0.0) + amount
        )

    def effective_visit_cap(self, base_cap: int) -> int:
        """Apply safety_extensions['max_phase_visits'] to base cap.

        Returns 0 (= unlimited) when base_cap is 0.
        Mirrors original L407-L410.
        """
        if not base_cap:
            return 0
        return int(base_cap + self.safety_extensions.get("max_phase_visits", 0.0))

    def effective_phase_budget(self, base_seconds: float) -> float:
        """Apply safety_extensions['phase_seconds'] to base budget.

        Mirrors original L469-L471.
        """
        return base_seconds + self.safety_extensions.get("phase_seconds", 0.0)

    def effective_act_turn_cap(self, phase: str, base_cap: int) -> int:
        """Apply safety_extensions[f'max_act_turns:{phase}'] to base cap.

        Mirrors original L1121-L1124.
        """
        return int(
            base_cap + self.safety_extensions.get(f"max_act_turns:{phase}", 0.0)
        )

    # ── Resume support (R-D2) ────────────────────────────────────────────────

    def restore_from_resume(self, plan: object, default_phase: str) -> None:
        """R-D2 pre-decrement pattern: restore visit_counts / history from plan
        and pre-decrement the current phase's count.

        ``plan`` must have ``visit_counts`` (dict) and ``phases_visited``
        (list[str]) attributes, matching the ResumePlan contract.

        The pre-decrement ensures that the upcoming ``begin_phase()`` call
        lands on the **same** visit count the original run had when the LLM
        was called — so ``args_hash`` matches the recorded step and the memo
        lookup hits. Without this, the resumed phase's first LLM call sees
        ``recorded_count + 1`` → hash mismatch → silent cost duplication.

        Mirrors original L1529-L1539.
        """
        self.visit_counts = dict(plan.visit_counts)
        self.history = list(plan.phases_visited)
        current_phase = (
            getattr(plan, "current_phase", None) or default_phase
        )
        if current_phase in self.visit_counts and self.visit_counts[current_phase] > 0:
            self.visit_counts[current_phase] -= 1
