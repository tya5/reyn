"""Tier 2: #5179 fix-design discriminator — does "capture the connect-time
status synchronously, immediately after ``session_backlog_page`` returns"
(the fix this investigation first proposed) actually close the race, or does
it merely relocate it?

Architect's review (issuecomment-5434642964, relayed by lead-coder) caught a
real defect in that first design BEFORE any implementation: ``session_
backlog_page``'s own loop (endpoint.py) has two exits —

    while True:
        history = list(target.history)
        frames, has_more, next_cursor = page_restored_history(history, ...)
        if has_more:
            return ...                       # exit A -- no await at all
        extended = await extend_history_backward_async()
        if extended <= 0:
            return frames, False, None        # exit B -- returns the PRE-await
                                               #           `frames`, computed
                                               #           BEFORE this await
                                               #           even started

Exit B is the DEFAULT path for a fresh/short session (nothing left on disk,
``extended == 0``) -- exactly #5179's own reported scenario. It returns
``frames`` computed strictly BEFORE ``extend_history_backward_async``'s own
``asyncio.to_thread`` disk-read await ever ran. A dispatch that commits
WHILE that await is in flight is therefore invisible to the returned
backlog (correct -- the backlog value itself is fine). But "capture status
right after `session_backlog_page` returns" reads status AFTER that same
await has already completed -- i.e. AFTER the concurrent dispatch had every
chance to land. That status capture already reflects the dispatch while the
backlog it is paired with does not: the exact #5179 mismatch (status ahead
of backlog), just moved from the ASGI-scheduling gap into the extend-await.

This is what my own design's ② ("fresh/short is a non-issue") and ④ ("(a) is
widest for fresh/short") directly contradicted -- ② reasoned about the
wrong direction (backlog racing AHEAD of status; the real risk is status
racing ahead of backlog, exit B's own default path).

This test DETERMINISTICALLY drives exactly that interleaving (not raced --
a ``threading.Event`` pair pins the real ``asyncio.to_thread`` disk-read
call mid-flight, matching this repo's own "external drive, not a sleep"
discipline for a collaborator gated by its own timer) and checks what a
"capture right after `session_backlog_page` returns" implementation would
observe. It is deliberately written against TODAY's real code paths (no
src/ change yet) -- it exercises the diagnostic, not a fix -- so it
currently DEMONSTRATES the flaw (asserts the mismatch is real), rather than
asserting non-mismatch. Once the "same tick" fix (architect's prescription:
read status in ``_status_provider``'s own synchronous body at the SAME
point ``history`` is read, before the extend-await even starts, for both
exit A and exit B) lands, this test's own final assertion should be
rewritten to assert the pairing IS consistent -- tracked as part of the
same fix PR, not a follow-up.

Real ``AgentRegistry``/``Session`` (``tests._support.agent_session.
make_session``) + the real ``session_backlog_page``/``extend_history_
backward_async`` production functions -- only ``read_history_before`` (a
pure disk-read helper, not a collaborator of the code under test) is
monkeypatched, and only to control ITS OWN timing deterministically, not to
change its logical return value (still "nothing on disk", the real
fresh-session shape)."""
from __future__ import annotations

import asyncio
import threading

import pytest

from reyn.interfaces.repl.status import _snapshot_for_session
from reyn.interfaces.transport.agui.endpoint import session_backlog_page
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_OWN_TEXT = "hello during the stalled extend-await"
_AGENT_NAME = "default"


def _make_registry(tmp_path) -> AgentRegistry:
    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    return AgentRegistry(project_root=tmp_path, session_factory=factory)


def _own_text_committed(session) -> bool:
    """Public SSoT read -- mirrors test_5179_backlog_gap_end_to_end.py's own
    ``_own_text_committed``: whether the real dispatch-commit
    (``Session._append_history``) has appended the operator's turn yet."""
    return any(
        getattr(m, "role", None) == "user" and _OWN_TEXT in str(getattr(m, "content", ""))
        for m in session.history
    )


@pytest.mark.asyncio
async def test_capture_right_after_return_still_mismatches_on_exit_b(tmp_path, monkeypatch) -> None:
    """Tier 2: demonstrates the architect-caught flaw directly, not just by
    argument -- pins the real ``extend_history_backward_async`` disk-read
    await mid-flight (exit B's own gate), commits a real dispatch while it
    is stalled, then releases it and shows: the returned backlog is (and
    must stay) empty, while a status read taken immediately afterward
    already reflects the dispatch. That pairing is exactly what #5179
    reports -- reproduced here at exit B specifically, discriminating the
    "capture right after return" design candidate (mismatches -- this test)
    from the "same tick" one (architect's fix -- would not)."""
    registry = _make_registry(tmp_path)
    AgentProfile.new(_AGENT_NAME, role="").save(tmp_path / ".reyn" / "agents" / _AGENT_NAME)
    session = await registry.ensure_running(_AGENT_NAME)

    hit_extend_read = threading.Event()
    release_extend_read = threading.Event()

    def _stalling_read_history_before(*args, **kwargs):
        # Runs on the REAL background thread asyncio.to_thread schedules
        # this onto (extend_history_backward_async's own step ① split) --
        # signals it has been reached, then blocks that thread until told
        # to continue. Still returns "nothing on disk" (empty), the same
        # logical result a fresh session's real read would give -- only
        # the TIMING is controlled, not the outcome.
        hit_extend_read.set()
        release_extend_read.wait()
        return []

    monkeypatch.setattr(
        "reyn.runtime.history_tail_reader.read_history_before",
        _stalling_read_history_before,
    )

    backlog_task = asyncio.create_task(
        session_backlog_page(registry, _AGENT_NAME, _DEFAULT_SID)
    )
    # Ceiling rule: poll the real threading.Event unboundedly, no sleep/count.
    while not hit_extend_read.is_set():
        await asyncio.sleep(0)

    # The extend-await is now provably stalled inside its own disk read.
    # Commit a REAL dispatch while it sits there.
    turn_task: "asyncio.Task | None" = None
    try:
        async def _drive_turn() -> None:
            try:
                await session.run_one_iteration()
            except Exception:
                # Expected: no @pytest.mark.replay fixture, so the real
                # litellm boundary raises once the router reaches it --
                # strictly after the history append this test cares about.
                pass

        await session.submit_user_text(_OWN_TEXT)  # real user_submitted
        turn_task = asyncio.create_task(_drive_turn())
        while not _own_text_committed(session):
            await asyncio.sleep(0)

        # NOW release the stalled disk read so extend_history_backward_async
        # can finish (real result: nothing found -- extended == 0 -- exit B).
        release_extend_read.set()
        initial_backlog, initial_has_more, initial_next_cursor = await backlog_task

        assert initial_backlog == [], (
            "test construction error: exit B must return the PRE-await "
            "(empty) frames -- a non-empty backlog here means this run did "
            "not exercise exit B, or session_backlog_page's own loop "
            "shape changed underneath this test"
        )

        # This is what "capture status right after session_backlog_page
        # returns" (the first design's own proposed fix) would read at
        # THIS exact point -- a real, non-fabricated status read.
        status_after_return = _snapshot_for_session(registry, session)

        assert session.queue_seq == 2, (
            f"test construction error: expected queue_seq==2 (user_submitted "
            f"+ turn_started already dispatched during the stall), got "
            f"{session.queue_seq!r}"
        )
        assert status_after_return["queue_seq"] == 2, (
            "test construction error: _snapshot_for_session did not reflect "
            "the real dispatch that landed during the stalled extend-await"
        )

        # THE FINDING: backlog (empty) and the "right after return" status
        # (queue_seq=2) disagree about whether this turn happened -- the
        # same mismatch #5179 reports, reproduced specifically at exit B.
        # A "same tick" fix (read status paired with the SAME history read
        # that produced `frames`, before the extend-await starts) would
        # instead see queue_seq at its PRE-dispatch value here, matching
        # the empty backlog it is paired with -- consistent, not mismatched.
        assert status_after_return["queue_seq"] != 0 and initial_backlog == [], (
            "ARCHITECT FINDING CONFIRMED: 'capture right after "
            "session_backlog_page returns' pairs an EMPTY backlog with a "
            f"status already reporting queue_seq={status_after_return['queue_seq']!r} "
            "-- exit B's own default (fresh/short session) path still "
            "reproduces #5179's mismatch under that design; only a 'same "
            "tick' read (paired with the pre-extend-await history read) "
            "closes this."
        )
    finally:
        release_extend_read.set()  # in case an assertion above fired first
        if turn_task is not None:
            turn_task.cancel()
            try:
                await turn_task
            except (Exception, asyncio.CancelledError):
                pass
