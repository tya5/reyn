"""Tier 2: #4405 — the ``REYN_STALL_TRACE`` wiring in
``Session._run_router_loop`` actually calls ``reyn.runtime.stall_trace``'s
real ``arm``/``disarm``, bracketing a REAL turn dispatch.

#5103 ④ migration: the first two tests below used to replace
``Session._loop_driver.run_turn`` with a private ``_noop``/recorder
closure — the LLM-avoidance seam AND the ordering-observation seam
tangled into one private monkeypatch (#5103's own triage of this file).
architect's ruling (#5103 ③④ design): a public, append-only ordering
seam already exists — ``stall_trace_armed``/``stall_trace_disarmed``
audit-events (new, this PR), ordered after ``turn_started`` and around
the real turn dispatch inside it. Real
``RouterLoopDriver.run_turn`` now runs for real (only
``litellm.acompletion`` is stubbed, ``@pytest.mark.llm_stub``, #5103
"C2"); the two tests below observe the real armed/turn_started/disarmed
sequence through the public ``Session.subscribe_audit_events`` seam, no
patch/mock, no private attribute assignment.

The THIRD test (disarm-on-exception) keeps the private ``run_turn``
replacement — it forces a genuine exception mid-turn, which
``LLMStub`` cannot do (it always returns a fixed non-erroring
completion) and the new audit-events cannot inject either (they only
OBSERVE, they do not CONTROL). This is NOT #5103's ② (turn-cancel
control) boundary, corrected by architect on this PR's own review:
production genuinely raises out of ``run_turn`` — a real reyn-self
measurement found ``router_loop_terminated_by_exception`` firing across
11 files, ``cause`` values ``RateLimitError``/``InternalServerError`` —
so this is not an unreachable branch being pinned. The real receiving
mechanism is #5382 (``LLMReplay`` replaying exception fixtures, a
closed cause vocabulary seeded from those same two measured causes) —
merged after this file's own last revision. Migrating THIS test onto
that fixture-driven replay is left for whoever picks up #5382's own
consumer sweep; comment policy §8 obligates that PR to update this
paragraph.

No waiting, no sleeping, no threshold crossing: the point under test is
WIRING (does the env var reaching a turn cause arm-then-disarm to be
called with the right value, and does that bracket the turn in the
right order), not the N-second stall-detection behavior itself, which
stall_trace.py's own docstring already explains cannot be tested
without violating the testing-policy time-limit ban.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime import stall_trace
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path) -> Session:
    return make_session(
        agent_name="test-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snapshot.json",
    )


def _collect(session: Session) -> list:
    """Subscribe through the public seam (``Session.subscribe_audit_events``,
    #5260) — never ``session._audit_events`` directly."""
    collected: list = []
    session.subscribe_audit_events(collected.append)
    return collected


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_stall_trace_brackets_a_real_turn_when_env_set(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: with REYN_STALL_TRACE set, a REAL turn (RouterLoopDriver.
    run_turn actually dispatches; only litellm.acompletion is stubbed)
    emits turn_started -> stall_trace_armed -> ... -> stall_trace_disarmed
    (turn_started fires in run_one_iteration before the turn's own task
    is even created; stall_trace_armed is the first statement inside
    that task, before run_turn dispatch) -- all carrying the SAME
    chain_id the test chose."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REYN_STALL_TRACE", "5")
    session = _make_session(tmp_path)
    collected = _collect(session)

    chain_id = "c-stall-armed"
    await session._put_inbox("user", {"text": "hi", "chain_id": chain_id})
    result = await session.run_one_iteration()

    assert result is True
    kinds = [e.type for e in collected]

    (armed_ev,) = [e for e in collected if e.type == "stall_trace_armed"]
    (started_ev,) = [e for e in collected if e.type == "turn_started"]
    (disarmed_ev,) = [e for e in collected if e.type == "stall_trace_disarmed"]

    assert armed_ev.data["seconds"] == 5.0
    for ev in (armed_ev, started_ev, disarmed_ev):
        assert ev.data["chain_id"] == chain_id, (
            f"{ev.type} carried the wrong chain_id: {ev.data!r}"
        )

    idx_armed = kinds.index("stall_trace_armed")
    idx_started = kinds.index("turn_started")
    idx_disarmed = kinds.index("stall_trace_disarmed")
    assert idx_started < idx_armed < idx_disarmed, (
        "expected turn_started -> stall_trace_armed -> stall_trace_disarmed, "
        f"got: {kinds!r}"
    )
    # The essential witness (architect finding on this PR's own review):
    # armed/disarmed both happening BEFORE/AFTER stall_trace_disarmed is
    # not enough — disarm is always last regardless of where arm actually
    # sits, so that alone cannot catch arm() being moved to AFTER
    # run_turn's real dispatch work. `llm_request` fires from INSIDE
    # run_turn's own real dispatch (the litellm.acompletion boundary
    # LLMStub stands in for) — bracketing THAT is what proves arm/disarm
    # genuinely wrap run_turn's work, not merely "somewhere before the
    # end of the turn". Strip-verified by hand: moving the arm()+emit
    # call to AFTER `await self._loop_driver.run_turn(...)` left the
    # idx_started < idx_armed < idx_disarmed assertion above GREEN
    # (disarm is still last either way) but turns THIS assertion RED.
    idx_llm_request = kinds.index("llm_request")
    assert idx_armed < idx_llm_request < idx_disarmed, (
        "stall_trace_armed must fire BEFORE run_turn's own dispatch work "
        "(llm_request) and stall_trace_disarmed AFTER it — "
        f"got: {kinds!r}"
    )


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_stall_trace_not_touched_when_env_unset(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: accept-side — with REYN_STALL_TRACE unset (the default),
    neither stall_trace_armed nor stall_trace_disarmed fires. Proves the
    wiring costs nothing (not even an event) for the overwhelming
    majority of turns that never opt in."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("REYN_STALL_TRACE", raising=False)
    session = _make_session(tmp_path)
    collected = _collect(session)

    await session._put_inbox("user", {"text": "hi", "chain_id": "c1"})
    result = await session.run_one_iteration()

    assert result is True
    kinds = {e.type for e in collected}
    assert "stall_trace_armed" not in kinds and "stall_trace_disarmed" not in kinds, (
        f"stall_trace_* must not fire when REYN_STALL_TRACE is unset: {kinds!r}"
    )


@pytest.mark.asyncio
async def test_stall_trace_disarmed_even_when_the_turn_raises(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: disarm() fires on the EXCEPTION path too, not just the happy
    path — the ``finally`` block's own reason for existing. Without this,
    a turn that raises leaves the background timer armed past turn end:
    it later dumps an UNRELATED stack into reyn.log, a false lead in
    exactly the shape #4403's investigation was fighting.

    Not migrated to @pytest.mark.llm_stub (see module docstring): this
    test needs run_turn to genuinely RAISE, which LLMStub cannot do (it
    always completes normally) and the audit-event seam cannot inject
    either (an observation seam, not a control seam). Production
    genuinely raises here (measured: router_loop_terminated_by_exception,
    11 files, RateLimitError/InternalServerError) — #5382's LLMReplay
    exception-fixture replay is the real receiving mechanism for
    migrating this test off the private replacement; see the module
    docstring."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REYN_STALL_TRACE", "5")
    session = _make_session(tmp_path)

    calls: list[str] = []
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: calls.append("arm"))
    monkeypatch.setattr(stall_trace, "disarm", lambda: calls.append("disarm"))

    async def _raising_run_turn(user_text: str, chain_id: str) -> None:
        raise RuntimeError("simulated turn failure")

    session._loop_driver.run_turn = _raising_run_turn  # type: ignore[method-assign]

    await session._put_inbox("user", {"text": "hi", "chain_id": "c1"})
    # The router loop's own top-level handler logs-and-swallows an
    # exception no inner handler took (session.py's "router loop caught
    # an exception no inner handler took" path, #5332) rather than
    # propagating it out of run_one_iteration — so this await completes
    # normally; the turn's FAILURE is not what this test is about, only
    # whether disarm() ran.
    await session.run_one_iteration()

    assert calls == ["arm", "disarm"], (
        "disarm() must still fire after a turn that raised — an armed "
        "timer left running past turn end dumps an unrelated stack later"
    )
