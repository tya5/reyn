"""Tier 2: #2242 — hard-cancel for turn interrupt (mid-flight LLM call).

Pre-#2242, ``cancel_inflight()`` only set a COOPERATIVE flag
(``RouterLoopDriver._turn_cancel_requested``), checked at the TOP of each
router-loop iteration — i.e. BEFORE the next LLM call, never during one. A
turn stuck mid-generation could not be interrupted; the spinner sat for the
full duration of the in-flight LLM call (~20s UX gap, see
``tests/llm/test_turn_cancel_1468.py`` for that pre-existing cooperative layer,
unchanged by this PR).

#2242 makes the turn body a per-turn CANCELLABLE SUB-TASK
(``Session._turn_owner_task = asyncio.create_task(self._run_turn_body(...))``,
awaited by ``run_one_iteration``) and has ``cancel_inflight()`` call
``_turn_owner_task.cancel()`` directly — injecting ``CancelledError`` at
whatever await point the sub-task is CURRENTLY suspended on (mid-generation:
the LLM call itself), aborting it immediately instead of waiting for the next
iteration boundary.

WAL-invariants pinned here (ADR-0038 Stage 1c / architect's #2242 design
comment):

  1. A cancelled turn's result is NEVER appended — CancelledError unwinds the
     turn-body task out of the in-flight await, so every statement AFTER that
     await (parsing the response, appending it to history) never executes.
     Proven here by RELEASING the hung LLM call AFTER the cancel: if the
     cancellation were merely cooperative (or simply delayed), the awaited
     call would resume and the reply WOULD land — this test asserts it never
     does.
  2. A fire-and-forget WAL-append task tracked BEFORE the cancelled turn's LLM
     await (``Session._track_wal_task`` — e.g. a buffered-intervention-answer
     consume) is NOT touched by cancelling ``_turn_owner_task`` (a distinct
     task) and is JOINED by ``await_quiescent()`` on the cancel path before
     ``run_one_iteration`` returns — it survives.
  3. The session (driver task) survives a hard-cancel: ``cancel_inflight()``
     swallows only its OWN cancellation (tracked via
     ``_turn_cancel_self_initiated``); ``run_one_iteration`` returns normally
     and a SUBSEQUENT turn runs to completion — the agent is not torn down.

Real ``Session`` / ``StateLog`` / ``AgentSnapshot`` (no mocks). #5450
migration: the LLM boundary is no longer a private ``_loop_driver.
run_turn`` replacement — ``@pytest.mark.llm_stub(control="gated")``
hangs the REAL turn at the REAL ``litellm.acompletion`` await (the
architect's own #5450 design cited THIS file's docstring — "simulating
the moment RouterLoop would be suspended inside its litellm.acompletion
await" — as proof the private replacement was standing in for a
boundary the stub already patches). ``RouterLoopDriver``/``RouterLoop``/
the driver now run for real; ``turn_started`` (the #5454 audit-event)
is asserted per test as witness ② — "the real loop ran", not merely
"the stub was called" (architect: "これがこの issue の存在理由").
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.router_loop import _EMPTY_RESPONSE_MSG
from reyn.runtime.session import Session
from reyn.user_intervention import InterventionAnswer
from tests._support.agent_session import make_session
from tests._support.events import settle

AGENT = "hard-cancel-agent"
# #5450: the real stub's canned reply for an empty/no-tool-call completion
# (router_loop.py's own constant) — the LITERAL content invariant 1 asserts
# never lands for a hard-cancelled turn, replacing the old private helper's
# own hand-picked sentinel string.
_STUB_REPLY = _EMPTY_RESPONSE_MSG["en"]


def _make_session(wal: Path, snapshot_path: Path) -> tuple[Session, StateLog]:
    state_log = StateLog(wal)
    session = make_session(agent_name=AGENT, state_log=state_log, snapshot_path=snapshot_path)
    return session, state_log


def _collect(session: Session) -> list:
    """Subscribe through the public seam (Session.subscribe_audit_events,
    #5260) — for witness ②: turn_started proves the REAL driver ran."""
    collected: list = []
    session.subscribe_audit_events(collected.append)
    return collected


async def _seed_prior_fire_and_forget_wal_task(session: Session, run_id: str) -> None:
    """Seed + consume a buffered intervention answer — the production
    fire-and-forget WAL-append seam ``Session.consume_buffered_intervention_answer``
    drives via ``_track_wal_task`` (see that method's #2242 docstring note and
    ``consume_buffered_intervention_answer``'s R-D12 comment). This is the
    concrete stand-in for WAL-invariant 2's "a fire-and-forget append task
    spawned before the cancelled turn's LLM await" — tracked BEFORE the hung
    turn is even started here, mirroring a real prior-turn append still
    settling when the NEXT turn gets hard-cancelled."""
    session.buffered_intervention_answers[run_id] = InterventionAnswer(text="prior answer")
    answer = session.consume_buffered_intervention_answer(run_id)
    assert answer is not None and answer.text == "prior answer"  # sanity: seeded + popped


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_hard_cancel_mid_generation_no_result_append_and_agent_survives(
    tmp_path, _llm_stub,
):
    """Tier 2: #2242 cancel-falsify. Cancelling DURING a hung "LLM call"
    (a) never lands the reply (invariant 1), (b) leaves the active branch
    clean — no partial/cancelled turn markers accumulate in history beyond
    the pre-cancel user message, (c) the agent survives — a subsequent
    ordinary turn completes normally, and (d) a fire-and-forget WAL-append
    task tracked before the hang settles via await_quiescent (invariant 2).

    Witness ② (#5450, architect: "this issue's own reason to exist"):
    turn_started fires for BOTH chain_ids — proof the REAL RouterLoopDriver/
    RouterLoop dispatched, not merely that the stub was called (a stub
    calling itself is not, by construction, evidence the real driver ran —
    see LLMStub's own module docstring).

    STRIP-RED: reverting ``Session.run_one_iteration``'s per-turn sub-task
    (back to running the dispatch inline on the driver task, as before #2242)
    makes ``cancel_inflight()``'s ``_turn_owner_task.cancel()`` cancel the
    OUTER (run_one_iteration) task itself instead of an isolated sub-task —
    the test's ``asyncio.wait_for(task, ...)`` then raises CancelledError
    instead of completing, and the fire-and-forget join never runs (RED).
    """
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path)
    events = _collect(session)

    prior_run_id = "prior-answer-run"
    await _seed_prior_fire_and_forget_wal_task(session, prior_run_id)

    await session._put_inbox("user", {"text": "hello", "chain_id": "c-hard-cancel"})
    turn_task = asyncio.create_task(session.run_one_iteration())

    await _llm_stub.call_started.wait()
    # The "LLM call" is now in flight (suspended on `release.wait()`).
    result = await session.cancel_inflight()
    assert "cancel" in result.lower()

    # Release the hang AFTER the cancel: if the sub-task were only
    # cooperatively (or not truly) cancelled, it would resume here and the
    # reply WOULD land — proving the difference between hard-cancel and a
    # merely-delayed completion.
    _llm_stub.release.set()

    # (c) agent survives: run_one_iteration returns normally (True), not an
    # exception — cancel_inflight() swallowed its own CancelledError.
    completed = await turn_task
    assert completed is True

    # (a) the cancelled turn's result never landed.
    assert not any(m.content == _STUB_REPLY for m in session.history), (
        "a hard-cancelled turn's LLM reply must never be appended, even after "
        "the underlying hung call is released post-cancel"
    )
    # (b) branch clean: only the pre-cancel user message plus the #3694
    # cancelled-outcome marker are present (no partial assistant/tool
    # entries from the aborted turn). The marker's role="system" (no new
    # role, mirrors notify_state_change) is durable proof the hard-cancel
    # WAS observed and recorded, not silent.
    roles = [m.role for m in session.history]
    assert roles == ["user", "system"], (
        f"expected the user message + the #3694 cancelled-outcome marker "
        f"(role=system); got {roles}"
    )
    cancelled_marker = session.history[-1]
    assert cancelled_marker.meta.get("kind") == "turn_cancelled"
    assert cancelled_marker.meta.get("chain_id") == "c-hard-cancel"

    # (d) the prior fire-and-forget WAL-append task survived (joined by
    # await_quiescent on the cancel path) — its durable effect is visible in
    # the WAL, not lost/orphaned by the sub-task cancellation.
    await session.journal.flush()
    wal_lines = [line for line in wal.read_text().splitlines() if line.strip()]
    assert any(
        '"intervention_answer_consumed"' in line and prior_run_id in line
        for line in wal_lines
    ), "the prior fire-and-forget intervention_answer_consumed append must survive the hard-cancel"

    # (c) continued: a SUBSEQUENT ordinary turn completes normally — the
    # session/driver was not torn down by the hard-cancel. The SAME stub
    # instance is still installed; its release Event is already set, so
    # this turn's own litellm.acompletion call passes straight through.
    await session._put_inbox("user", {"text": "again", "chain_id": "c-after-cancel"})
    next_completed = await session.run_one_iteration()
    assert next_completed is True
    assert any(
        m.role == "assistant" and m.content == _STUB_REPLY for m in session.history
    ), (
        "a normal turn after a hard-cancel must complete and append its reply — "
        "the agent must survive to serve the next turn"
    )

    # witness ②: the REAL driver dispatched for both chain_ids — turn_started
    # is emitted in run_one_iteration BEFORE the turn's own task starts, so
    # its presence for c-hard-cancel is unaffected by that turn's later
    # cancellation.
    await settle(session)
    started_chain_ids = {
        e.data.get("chain_id") for e in events if e.type == "turn_started"
    }
    assert {"c-hard-cancel", "c-after-cancel"} <= started_chain_ids, (
        f"expected turn_started for both chain_ids, got: {started_chain_ids!r}"
    )

    await state_log.aclose()


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_hard_cancel_prior_append_survives_wal_truncation(tmp_path, _llm_stub):
    """Tier 2: #2242 truncate-falsify (CLAUDE.md recovery-feature PR gate).

    Repeats the hard-cancel scenario, then pushes filler WAL events past the
    surviving fire-and-forget append's source events and truncates below
    them (the same set-truncate-reconstruct-assert shape every
    truncate-falsify test in this repo uses).
    Reconstructing (fresh Session + StateLog: load snapshot, replay the WAL
    tail) must still show the buffered-answer-consumed state as durable —
    proving the hard-cancel path does not leave the fire-and-forget append in
    a state that a subsequent truncation+reconstruction cycle would corrupt
    or lose. RED if the snapshot-side bookkeeping (``buffered_intervention_
    answers`` popped on consume, backed by ``AgentSnapshot``) were skipped or
    raced by the cancel path: reconstruction would still show the answer as
    OUTSTANDING (not consumed) or missing the consumed marker in the WAL.
    """
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path)
    events = _collect(session)

    prior_run_id = "prior-answer-run-truncate"
    await _seed_prior_fire_and_forget_wal_task(session, prior_run_id)

    await session._put_inbox("user", {"text": "hello", "chain_id": "c-truncate"})
    turn_task = asyncio.create_task(session.run_one_iteration())
    await _llm_stub.call_started.wait()
    await session.cancel_inflight()
    _llm_stub.release.set()
    await turn_task
    await session.journal.flush()
    # witness ②: the real driver dispatched for this turn.
    await settle(session)
    assert any(e.type == "turn_started" for e in events)

    # sanity: the consumed marker's source event is durable pre-truncation.
    pre_truncate_lines = [line for line in wal.read_text().splitlines() if line.strip()]
    assert any(
        '"intervention_answer_consumed"' in line and prior_run_id in line
        for line in pre_truncate_lines
    ), "sanity: the consumed-answer source event must be durable pre-truncation"
    assert prior_run_id not in session.buffered_intervention_answers, (
        "sanity: the answer must already be popped from the live buffer"
    )

    # push filler events far past the source events, then truncate below them.
    for i in range(150):
        await state_log.append("inbox_put", n=i)
    floor = state_log.current_seq - 5
    await state_log.truncate_below(floor)
    await state_log.flush()
    stats = state_log.last_truncate_stats
    assert stats["dropped"] >= 2, (
        f"the buffered/consumed source events must be truncated below the floor; "
        f"dropped={stats['dropped']}"
    )
    post_truncate_lines = [line for line in wal.read_text().splitlines() if line.strip()]
    assert not any(
        '"intervention_answer_consumed"' in line and prior_run_id in line
        for line in post_truncate_lines
    ), "the consumed-answer source event must actually be gone post-truncation"

    await state_log.aclose()  # simulate the crash: tear down run1's WAL worker

    # reconstruct (simulates a restart): a FRESH StateLog + Session over the
    # SAME wal/snapshot (mirrors AgentRegistry.restore_all).
    session2, state_log2 = _make_session(wal, snapshot_path)
    snap = AgentSnapshot.load(AGENT, snapshot_path)
    events = list(state_log2.iter_from(snap.applied_seq))
    snap.apply_events(events)
    session2.restore_state(snap)

    assert prior_run_id not in session2.buffered_intervention_answers, (
        "the answer must stay CONSUMED after reconstruction — the hard-cancel "
        "path must not leave it re-appearing as outstanding post-truncation"
    )

    await state_log2.aclose()


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_external_cancel_of_driver_task_propagates(tmp_path, _llm_stub):
    """Tier 2: #2242 Finding 2 — an EXTERNAL cancellation of the task running
    ``run_one_iteration`` (i.e. NOT via ``cancel_inflight()``, so
    ``_turn_cancel_self_initiated`` stays False) must PROPAGATE, not be
    swallowed.

    This is the FP-0013 §ADR-A path: the MCP/A2A request-handler task pumps
    ``run_one_iteration`` directly and lives inside an anyio task group; an
    anyio scope teardown cancels that handler task, and the cancellation must
    reach it (structured concurrency requires the cancelled task to actually
    end). #2242 only swallows OUR OWN ``cancel_inflight()`` cancel; anything
    else re-raises. Plain asyncio reproduces this — cancelling the driver task
    directly is exactly what an outer scope teardown does.

    STRIP-RED: dropping the ``if self._turn_cancel_self_initiated: ... else:
    raise`` discrimination (swallowing ALL CancelledError) makes the driver
    task complete normally instead of ending cancelled — ``pytest.raises``
    then sees no exception (RED)."""
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path)
    events = _collect(session)

    await session._put_inbox("user", {"text": "hello", "chain_id": "c-external"})
    turn_task = asyncio.create_task(session.run_one_iteration())
    await _llm_stub.call_started.wait()

    # External cancel: cancel the driver task DIRECTLY (an outer scope teardown),
    # NOT through cancel_inflight() — so this is NOT self-initiated.
    turn_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await turn_task

    # sanity: the hung reply never landed (the turn was torn down, not completed).
    assert not any(m.content == _STUB_REPLY for m in session.history)
    # witness ②: the real driver dispatched before the external cancel hit it.
    await settle(session)
    assert any(e.type == "turn_started" for e in events)
    await state_log.aclose()


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_self_initiated_flag_does_not_leak_to_next_turn(tmp_path, _llm_stub):
    """Tier 2: #2242 Finding 1 — the ``_turn_cancel_self_initiated`` flag must
    NOT leak past the turn that set it.

    Turn 1: ``cancel_inflight()`` cancels the sub-task, and #2242's own
    discrimination (``_turn_cancel_self_initiated``) swallows exactly that
    cancellation — ``run_one_iteration`` returns True normally, matching
    ``test_hard_cancel_mid_generation_...`` above (#5450: the private
    swallowing-run_turn closure this test used before is GONE — see module
    docstring; reyn's own self-swallow, discriminated via the flag, was
    always the real mechanism producing "turn 1 completes normally", never
    a third party swallowing inside run_turn's own body). Turn 2 is then
    cancelled EXTERNALLY (not via ``cancel_inflight()``); its cancellation
    must PROPAGATE.

    If the flag leaked True from turn 1, turn 2's external cancel would be
    mis-read as self-initiated and swallowed — the driver task would
    complete normally instead of ending cancelled, breaking the FP-0013
    external-cancel contract.

    STRIP-RED: moving the ``_turn_cancel_self_initiated = False`` reset back
    out of the unconditional ``finally`` and onto the swallow branch leaks
    the flag → turn 2's external cancel is swallowed → ``pytest.raises``
    sees no exception (RED). Reproduced live during review."""
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path)
    events = _collect(session)

    # ── Turn 1: self-initiated cancel via cancel_inflight() (flag set, reset
    #    happens in the unconditional finally). ────────────────────────────
    await session._put_inbox("user", {"text": "one", "chain_id": "c-leak-1"})
    turn1 = asyncio.create_task(session.run_one_iteration())
    await _llm_stub.call_started.wait()
    await session.cancel_inflight()  # sets _turn_cancel_self_initiated True
    _llm_stub.release.set()
    completed1 = await turn1
    assert completed1 is True

    # ── Turn 2: EXTERNAL cancel — must propagate (flag must have been reset).
    #    Re-arm the shared stub for a second hang: release stays set forever
    #    once set() (asyncio.Event has no auto-reset), so it must be
    #    cleared before this turn can suspend again. ───────────────────────
    _llm_stub.call_started.clear()
    _llm_stub.release.clear()
    await session._put_inbox("user", {"text": "two", "chain_id": "c-leak-2"})
    turn2 = asyncio.create_task(session.run_one_iteration())
    await _llm_stub.call_started.wait()
    turn2.cancel()  # external — NOT via cancel_inflight()

    with pytest.raises(asyncio.CancelledError):
        await turn2

    # witness ②: both turns really dispatched (turn2's is emitted before its
    # own external cancel, in run_one_iteration, same ordering as witness ①
    # test above).
    await settle(session)
    started_chain_ids = {
        e.data.get("chain_id") for e in events if e.type == "turn_started"
    }
    assert {"c-leak-1", "c-leak-2"} <= started_chain_ids

    await state_log.aclose()


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_await_quiescent_join_is_load_bearing_on_cancel(tmp_path, _llm_stub):
    """Tier 2: #2242 Finding 3 — the ``await self.await_quiescent()`` call on
    the hard-cancel path is load-bearing: it settles a tracked fire-and-forget
    WAL-append task so no straggler outlives the reported-idle turn.

    A tracked task that awaits indefinitely (the canonical shape —
    ``_dispatch_intervention`` awaits the user-answer future indefinitely, per
    ``_track_wal_task``'s docstring) is registered before the turn. On the
    cancel path ``await_quiescent()`` cancels + joins it, so it is SETTLED
    (``done()``) by the time ``run_one_iteration`` returns. We witness this via
    the PUBLIC ``asyncio.Task`` surface of a handle the TEST holds (``.done()``),
    not any session-private state.

    STRIP-RED: removing ``await self.await_quiescent()`` from the cancel branch
    leaves the tracked task still pending (awaiting ``never``) when
    ``run_one_iteration`` returns → ``prior_task.done()`` is False → RED. The
    straggler would then be free to land a WAL append after the session is
    reported idle — the contamination ``await_quiescent`` exists to prevent."""
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path)
    events = _collect(session)

    never = asyncio.Event()  # never set → the task settles ONLY via cancellation

    async def _indefinite_prior_wal_task() -> None:
        await never.wait()

    # Register through the real convention seam (a tracked fire-and-forget
    # WAL-append task); hold the handle so we can witness settling publicly.
    prior_task = session._track_wal_task(asyncio.ensure_future(_indefinite_prior_wal_task()))
    try:
        await session._put_inbox("user", {"text": "hello", "chain_id": "c-quiescent"})
        turn_task = asyncio.create_task(session.run_one_iteration())
        await _llm_stub.call_started.wait()
        await session.cancel_inflight()
        _llm_stub.release.set()
        completed = await turn_task
        assert completed is True

        # witness ②: the real driver dispatched.
        await settle(session)
        assert any(e.type == "turn_started" for e in events)

        # The join happened: the tracked straggler is settled (cancelled+joined)
        # before run_one_iteration returned — no un-joined WAL-append task
        # outlives the idle turn.
        assert prior_task.done(), (
            "await_quiescent() on the cancel path must settle the tracked "
            "fire-and-forget WAL task before run_one_iteration returns — a "
            "still-pending straggler could append after the session is idle"
        )
    finally:
        if not prior_task.done():
            prior_task.cancel()
        never.set()
        await state_log.aclose()
