"""Tier 2: #4768 — an ephemeral session vanishing DURING a global rewind's own
quiesce sweep.

#4759 gave ``SpawnTracker._vanish_task`` (the ephemeral auto-vanish teardown
task, which itself appends ``session_vanished`` to the WAL via
``remove_session``) ``appends_wal=True`` — a NEW conclusion, not a restatement
of pre-#4759 behaviour (before #4759 this task was never tracked by
``await_quiescent`` at all). The reasoning: ``AgentRegistry.checkout``'s own
docstring names its invariant as "no straggler [append] past the reset-record
seq", and the global WAL (``StateLog``) is the SAME physical stream a session's
own ``session_vanished`` append lands in (verified at #4759/#4765 review:
``SnapshotJournal`` wraps the SAME ``StateLog`` instance the registry holds).
The implementation's own comment self-disclosed the gap this file closes:
"no existing test exercises an ephemeral session vanishing DURING a global
rewind ... a reasoned extension of the invariant" — this was read-verified
(termination proven by inspecting ``TrackedTaskSet.aclose``'s own reentrancy
exclusion), never EXECUTED. This test executes it.

Two things this test verifies, both by REAL execution, not reasoning:

① No straggler — the vanish task's own ``session_vanished`` WAL append must
  land at or before the rewind's own reset-record seq, never past it (the
  literal invariant ``checkout``'s docstring names, applied to a producer
  #4759 newly brought into ``await_quiescent``'s scope).
② Termination — the rewind itself (``registry.checkout``) must actually
  RETURN. Per lead-coder's explicit instruction: no ``asyncio.wait_for`` or
  any other time bound wraps this await — the whole point is that
  termination was previously ONLY established by reading the code (no
  cancel-then-await-self cycle in ``TrackedTaskSet.aclose``'s reentrancy
  exclusion); if that reading is wrong, this test must actually hang and
  let CI's own ``--timeout=120`` kill switch catch it, not a test-owned
  budget that would silently mask the same class of unverified-termination
  claim testing.md's own § Time section exists to rule out.

Population this combination is constructible by (Q3, "who'd miss it"):
ANY operator invoking ``/rewind`` while ANY OTHER loaded session — a
spawned pipeline driver-session (``session_api.py``'s
``_spawn_pipeline_driver_session``) is the one live producer of an ephemeral
session in production — is between its own last turn and its own
auto-vanish (``_maybe_schedule_ephemeral_vanish``, fired from that
session's own idle-inbox check, entirely independent of any rewind).
``AgentRegistry._iter_sessions()`` (what ``checkout`` sweeps) does not filter
by ephemeral status, so a driver-session mid-vanish is swept exactly like
any other loaded session — this is not a configuration only this test
itself assembles.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """Real AgentRegistry whose factory passes ``registry=reg`` to each Session
    (mirrors the production scoped_session_factory) — mirrors
    test_2103_A_ephemeral_auto_vanish_1953.py's own helper."""
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log,
                    registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


@pytest.mark.asyncio
async def test_ephemeral_vanish_racing_a_global_rewind_no_straggler_and_terminates(tmp_path):
    """Tier 2: race a REAL ephemeral-vanish task against a REAL global rewind's
    quiesce sweep. See module docstring for ①/② and the no-time-bound
    rationale for ②."""
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")  # the live main session — checkout needs something to cut

    # a real WAL entry so there is a valid rewind target.
    put_seq = await reg.state_log.append(
        "inbox_put", target="alice", msg_id="m1", msg_kind="user",
        payload={"text": "hi"},
    )

    eph_sid = await reg.spawn_session_recorded(
        "alice", mode="ephemeral", presentation_consumer=None, intervention_bridge=None,
    )
    eph = reg._peek_session("alice", eph_sid)
    assert eph is not None

    # Trigger the REAL ephemeral auto-vanish -- creates the real _vanish_task
    # via #4759's task funnel (disposition="await", appends_wal=True),
    # genuinely in-flight (not started, not done) at this point:
    # asyncio.create_task schedules but does not run until this coroutine's
    # own next suspension point, which is the checkout() call below.
    eph._maybe_schedule_ephemeral_vanish()
    vanish_task = eph._spawn_tracker._vanish_task
    assert vanish_task is not None, "the ephemeral auto-vanish guard did not fire -- test setup is wrong"
    assert not vanish_task.done(), (
        "the vanish task already completed before checkout() -- the race this "
        "test exists to exercise did not happen; the test needs re-deriving, "
        "not a sleep inserted to 'fix' the timing"
    )

    # ② termination -- no asyncio.wait_for, no timeout, per lead-coder's
    # explicit instruction: this await is unbounded on purpose. If
    # TrackedTaskSet.aclose's reentrancy exclusion (or anything else in the
    # #4759/#4765 chain) has a real cycle, THIS is where it hangs, and CI's
    # own --timeout=120 is the only thing that catches it.
    result = await reg.checkout(put_seq)
    R = result["reset_seq"]

    # The vanish task must have actually run to completion by now (checkout's
    # own await_quiescent(appends_wal=True) awaits disposition="await" tasks
    # to completion, not just cancels them) — confirms the race was real, not
    # a no-op where the vanish task happened to already be done.
    assert vanish_task.done()
    assert eph_sid not in reg.session_ids("alice"), "the ephemeral session never actually vanished"

    # ① no straggler -- session_vanished's own WAL append must land AT OR
    # BEFORE R, never past it.
    vanished_entries = [
        e for e in reg.state_log.iter_from(1)
        if e.get("kind") == "session_vanished" and e.get("sid") == eph_sid
    ]
    assert vanished_entries, (
        "no session_vanished entry was ever appended for the ephemeral "
        "session -- the vanish task ran but its own WAL append never landed"
    )
    straggler_seqs = [e["seq"] for e in vanished_entries if e["seq"] > R]
    assert not straggler_seqs, (
        f"session_vanished landed PAST the reset-record (a straggler append, "
        f"the exact #1533/#2115 bug class): entries at seq {[e['seq'] for e in vanished_entries]}, "
        f"reset-record R={R}"
    )
