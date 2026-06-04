"""Tier 2: OS invariant — force-close re-entry injection (#1092 PR-D2).

D2 makes the OS re-enter the SAME phase after a force-close, injecting the
consolidated checkpoint via the rollback seam (``previous_control_ir_results``),
current_phase HELD (no graph edge), bounded by the EXISTING ``max_phase_visits``
loop limit (the re-entry goes through enter_phase → begin_phase visit++). This
pins the injection primitive:

- ``RollbackState.arm_force_close_reentry`` arms the next iteration's pending ctx
  with ``previous_control_ir_results`` = the checkpoint (the same slot
  PhaseExecutor restores into the seed frame);
- it is NOT a rollback — it does NOT arm the no-progress sentinel (which would
  abort a re-entry that re-produces output).

The full fires→re-enters→converges / →visit-cap-abort integration lands with
PR-E (which adds the force-close firing test-infra for its by-construction
floor-fits + monotonic-progress empirical guarantee).
"""
from __future__ import annotations

from reyn.kernel.rollback_state import RollbackState


def test_arm_force_close_reentry_sets_previous_control_ir_results() -> None:
    """Tier 2: arming a force-close re-entry makes the next take_pending_ctx
    hand the checkpoint back as previous_control_ir_results (the injection slot
    PhaseExecutor restores into the seed frame)."""
    rs = RollbackState()
    checkpoint = [{"result": "[prior work consolidated] did X, Y"}]
    rs.arm_force_close_reentry(checkpoint)
    ctx = rs.take_pending_ctx()
    assert ctx == {"previous_control_ir_results": checkpoint}
    # one-shot: cleared after the read.
    assert rs.take_pending_ctx() is None


def test_arm_force_close_reentry_is_not_a_rollback() -> None:
    """Tier 2: a force-close re-entry is a SELF re-entry, not a rollback — it must
    NOT arm the no-progress sentinel (that would abort a re-entry whose phase
    output happens to match a prior one; force-close convergence is governed by
    the visit cap + PR-E's monotonic-progress, not the rollback no-progress check)."""
    rs = RollbackState()
    rs.arm_force_close_reentry([{"result": "ckpt"}])
    assert rs.no_progress_check is None
    # consume_no_progress is inert (no sentinel armed).
    assert rs.consume_no_progress("draft", {"any": "data"}) is None


def test_arm_force_close_reentry_copies_the_list() -> None:
    """Tier 2: the armed checkpoint is a copy — mutating the caller's list after
    arming does not change the pending ctx (no aliasing of OS state)."""
    rs = RollbackState()
    src = [{"result": "ckpt"}]
    rs.arm_force_close_reentry(src)
    src.append({"result": "mutated after arm"})
    ctx = rs.take_pending_ctx()
    assert ctx is not None
    assert ctx["previous_control_ir_results"] == [{"result": "ckpt"}]
