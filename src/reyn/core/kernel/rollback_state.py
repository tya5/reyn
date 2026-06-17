"""RollbackState — rollback-specific bookkeeping for one OSRuntime run.

Extracted from runtime.py (FP-0020 Component A prerequisite).  All
rollback machinery that previously lived as a nested dataclass inside
runtime.py is now a standalone module so it can be imported by RunState
without creating a circular dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RollbackState:
    """All rollback-specific bookkeeping for a single OSRuntime run.

    OSRuntime owns the run, this owns the rollback machinery — kept here so
    the four-or-five fields that exist purely to support rollback don't
    pollute OSRuntime's instance namespace.

    Field semantics:
      phase_inputs[phase]  — the artifact the phase was entered with
                             (used to restore on rollback into that phase)
      phase_outputs[phase] — the artifact the phase last produced
                             (used as `rejected_artifact` for the next iteration)
      phase_prev[phase]    — the phase that was the predecessor when this phase
                             was last entered (used to walk back on rollback)
      pending_ctx          — single-shot rollback_ctx for the next _execute_phase
      no_progress_check    — single-shot sentinel: if the rolled-back-into phase
                             re-produces the rejected output, abort
    """

    phase_inputs: dict[str, dict] = field(default_factory=dict)
    phase_outputs: dict[str, dict] = field(default_factory=dict)
    phase_prev: dict[str, str | None] = field(default_factory=dict)
    pending_ctx: dict | None = None
    no_progress_check: dict | None = None
    # PR-N5: snapshot of each phase's final control_ir_results, keyed by
    # phase name.  Saved at A → B transition so B → A rollback can restore
    # A's prior act-loop progress instead of starting fresh.
    _phase_history_snapshots: dict[str, list[dict]] = field(default_factory=dict)

    # ── recording (called by OSRuntime as it advances) ──

    def record_input(self, phase: str, artifact: dict) -> None:
        self.phase_inputs[phase] = artifact

    def record_output(self, phase: str, artifact: dict) -> None:
        self.phase_outputs[phase] = artifact

    def record_predecessor(self, phase: str, prev: str | None) -> None:
        self.phase_prev[phase] = prev

    # ── reading (used to restore on rollback) ──

    def get_input(self, phase: str) -> dict:
        return self.phase_inputs[phase]

    def get_predecessor(self, phase: str) -> str | None:
        return self.phase_prev.get(phase)

    # ── rollback transition ──

    def begin_rollback(self, from_phase: str, to_phase: str, reason: str) -> dict:
        """Set up state for the upcoming re-run of `to_phase`.

        Captures the rollback context (rejected output + reason + caller phase)
        and arms the no-progress sentinel. Returns the rollback context for
        callers that want to log or inspect it; OSRuntime normally consumes it
        via `take_pending_ctx()` on the next iteration.
        """
        rejected = self.phase_outputs.get(to_phase, {})
        ctx = {
            "rejected_artifact": rejected,
            "reason": reason,
            "rollback_from": from_phase,
        }
        self.pending_ctx = ctx
        self.no_progress_check = {
            "phase": to_phase,
            "prev_output_data": rejected.get("data"),
            "rollback_from": from_phase,
        }
        return ctx

    def take_pending_ctx(self) -> dict | None:
        """One-shot read+clear of the rollback context."""
        ctx = self.pending_ctx
        self.pending_ctx = None
        return ctx

    def arm_force_close_reentry(self, checkpoint_results: list[dict]) -> None:
        """#1092 PR-D2: arm a force-close SELF re-entry of the current phase.

        Sets the pending ctx so the next loop iteration's ``take_pending_ctx()``
        hands ``checkpoint_results`` to PhaseExecutor as
        ``previous_control_ir_results`` (the SAME injection slot rollback uses →
        restored into the seed frame). This is NOT a rollback (no phase change, no
        no-progress sentinel) — it is the OS-internal re-entry of the same phase
        with the consolidated checkpoint injected, bounded by the existing
        ``max_phase_visits`` loop limit (the re-entry goes through enter_phase).
        """
        self.pending_ctx = {"previous_control_ir_results": list(checkpoint_results)}

    def consume_no_progress(self, phase: str, output_data: Any) -> str | None:
        """Check & clear the no-progress sentinel for `phase`.

        If `phase` is the one we just rolled into and `output_data` matches the
        previously-rejected output, returns the original rollback_from (the
        caller should abort with a no-progress error).

        If `phase` doesn't match the sentinel, leaves it alone — a different
        phase may yet visit this check. If `phase` matches but the output
        differs, clears the sentinel (rollback succeeded; the check has
        served its purpose).
        """
        if self.no_progress_check is None:
            return None
        if self.no_progress_check["phase"] != phase:
            return None
        if output_data == self.no_progress_check["prev_output_data"]:
            rollback_from = self.no_progress_check.get("rollback_from", "?")
            self.no_progress_check = None
            return rollback_from
        self.no_progress_check = None
        return None

    # ── PR-N5: phase history snapshots ──

    def snapshot_phase_history(self, phase: str, results: list[dict]) -> None:
        """Save a snapshot of phase's final control_ir_results, keyed by phase.

        Called when transitioning A → B so that a later B → A rollback can
        restore A's prior act-loop progress (grep/file_read observations)
        instead of re-running the same ops from scratch.

        A shallow copy of `results` is stored so that subsequent mutations to
        the caller's list do not corrupt the snapshot.
        """
        self._phase_history_snapshots[phase] = list(results)

    def get_snapshot(self, phase: str) -> list[dict] | None:
        """Return the saved control_ir_results snapshot for `phase`, or None.

        Returns None when no snapshot has been saved for this phase (= first
        entry, or the phase was never followed by a transition-then-rollback).
        The caller falls through to an empty list in that case (= current
        unmodified behavior).
        """
        return self._phase_history_snapshots.get(phase)
