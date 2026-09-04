"""Tier 2: #5729 — AgentRegistry projects per-session turn_active/iv_waiting
for every LOADED session in this process, live (no stored copy) for the pull
side (:meth:`AgentRegistry.all_sessions_status`) and via a per-session
monotonic-seq-gated push channel (:meth:`AgentRegistry.add_status_listener`)
for the delta side.

Real ``AgentRegistry``/``Session``/``InterventionRegistry`` throughout —
never a mock. The one thing genuinely hard to drive without the LLM boundary
is a real mid-flight ``turn_active=True`` read: ``@pytest.mark.llm_stub
(control="gated")`` (this repo's own established seam, e.g.
test_3694_persist_cancelled_turn_outcome.py / test_5248_turn_finally.py) is
used for that, holding a real turn open at the LLM call so the test can
observe the transition rather than only its settled endpoints.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.user_intervention import UserIntervention
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


# ── pull side: all_sessions_status() ────────────────────────────────────────


def test_all_sessions_status_skips_a_declared_but_unloaded_agent(tmp_path):
    """Tier 2: a DECLARED agent with no live Session yet (never get_or_load'd)
    contributes zero rows — "nothing to report" is not the same claim as
    "not running", so it must never be fabricated as a False row (see
    all_sessions_status's own docstring)."""
    reg = _make_registry(tmp_path)
    assert reg.all_sessions_status() == []


def test_all_sessions_status_computed_fresh_no_stored_copy(tmp_path, monkeypatch):
    """Tier 2: two successive calls, with a real Session state change between
    them, must both reflect the CURRENT state — a stored copy would show the
    first call's value forever (architect's "no stored copy of STATUS"
    ruling). Driven via a real 2-intervention enqueue/resolve, the cheapest
    real state transition available without the LLM boundary."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    session = reg.get_session("alice")
    assert reg.all_sessions_status() == [
        {"agent": "alice", "sid": "main", "turn_active": False, "iv_waiting": False},
    ]

    async def _run():
        # Constructed INSIDE the coroutine, not at test-function scope: a
        # ``UserIntervention``'s ``future`` binds to whatever event loop is
        # current AT CONSTRUCTION (``__post_init__``) — building it outside
        # ``asyncio.run()``'s own fresh loop attaches the future to a stale
        # default loop and ``await``ing it later raises "different loop".
        iv = UserIntervention(kind="ask_user", prompt="Q?")
        task = asyncio.ensure_future(session.interventions.dispatch(iv))
        await asyncio.sleep(0)
        assert reg.all_sessions_status()[0]["iv_waiting"] is True
        await session.interventions.deliver_answer(iv, "ok")
        await task
        assert reg.all_sessions_status()[0]["iv_waiting"] is False

    asyncio.run(_run())


# ── push side: status listener, N>=2 sessions, identifiers never cross ──────


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_status_push_never_reports_the_wrong_sessions_identifier(
    tmp_path, monkeypatch, _llm_stub,
):
    """Tier 2: the N>=2 witness architect's ruling specifically demanded —
    "N=1 is always green" for the classic loop-closure late-binding bug, so
    this drives TWO real sessions of the SAME agent (same name, different
    sid — the harder case, since only ``sid`` distinguishes them) and drives
    a real turn on exactly ONE of them. Every push recorded during that turn
    must carry THAT session's own (name, sid) — never the other one's,
    even though ``_subscribe_session_status`` was called twice against the
    same closure-building code.

    Falsification (performed for real): temporarily changing
    ``_subscribe_session_status``'s closure to capture a shared outer
    variable instead of its own call-scoped ``name``/``sid`` parameters
    made this test fail — pushes for session A's turn started reporting
    session B's sid once B was the LAST one subscribed. Reverted after
    confirming red.
    """
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid_a = "main"
    sid_b = reg.spawn_session("alice", presentation_consumer=None, intervention_bridge=None)
    assert sid_b != sid_a
    session_a = reg.get_session("alice", sid_a)

    pushes: list[tuple[str, str, bool, bool, int]] = []
    reg.add_status_listener(lambda name, sid, ta, iw, seq: pushes.append((name, sid, ta, iw, seq)))

    await session_a._put_inbox("user", {"text": "hello", "chain_id": "c-5729"})
    turn_task = asyncio.create_task(session_a.run_one_iteration())
    try:
        await _llm_stub.call_started.wait()
        # Mid-flight: this session's own turn_active must read True right now.
        assert session_a.turn_active is True
    finally:
        _llm_stub.release.set()
        await turn_task

    assert pushes, "expected at least one status push for the real turn just driven"
    for name, sid, *_rest in pushes:
        assert name == "alice"
        assert sid == sid_a, f"a push for session A leaked session B's sid: {pushes!r}"
    assert pushes[0][2] is True, "the first push (turn_started) must show turn_active True"
    assert pushes[-1][2] is False, "the last push (turn_settled) must show turn_active False"

    # seq is strictly increasing per (name, sid) — the caller's stale-delta gate.
    seqs = [p[4] for p in pushes]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

    final = {(row["agent"], row["sid"]): row for row in reg.all_sessions_status()}
    assert final[("alice", sid_a)] == {
        "agent": "alice", "sid": sid_a, "turn_active": False, "iv_waiting": False,
    }
    assert final[("alice", sid_b)] == {
        "agent": "alice", "sid": sid_b, "turn_active": False, "iv_waiting": False,
    }


# ── IV: head-limited announce is enough for a bool (architect's own point) ──


def test_iv_waiting_stays_true_while_head_resolves_and_second_still_queued(tmp_path, monkeypatch):
    """Tier 2: architect's own worked table — enqueueing a SECOND
    intervention while the first is still head does not re-announce (head-
    limited), and resolving the head while a second is still queued must
    NOT flip iv_waiting back to False. Both are real ``InterventionRegistry``
    transitions (dispatch/deliver_answer), not a synthetic flag."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    session = reg.get_session("alice")

    async def _run():
        # Constructed INSIDE the coroutine — see the sibling test's comment
        # on why (a UserIntervention's future binds to whatever loop is
        # current at construction).
        iv1 = UserIntervention(kind="ask_user", prompt="Q1?")
        iv2 = UserIntervention(kind="ask_user", prompt="Q2?")
        t1 = asyncio.ensure_future(session.interventions.dispatch(iv1))
        await asyncio.sleep(0)
        t2 = asyncio.ensure_future(session.interventions.dispatch(iv2))
        await asyncio.sleep(0)
        assert reg.all_sessions_status()[0]["iv_waiting"] is True
        assert session.interventions.head() is iv1

        consumed = await session.interventions.deliver_answer(iv1, "ok")
        assert consumed is True
        await t1
        # ★ the load-bearing assertion: head resolved, a second is STILL
        # queued — iv_waiting must stay True, not flip False.
        assert reg.all_sessions_status()[0]["iv_waiting"] is True
        assert session.interventions.head() is iv2

        consumed2 = await session.interventions.deliver_answer(iv2, "ok")
        assert consumed2 is True
        await t2
        assert reg.all_sessions_status()[0]["iv_waiting"] is False

    asyncio.run(_run())


# ── the combination architect ruled MUST be observable, never collapsed ─────


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_turn_active_and_iv_waiting_are_independent_and_both_true_is_observable(
    tmp_path, monkeypatch, _llm_stub,
):
    """Tier 2: architect's central ruling — turn_active and iv_waiting are 2
    INDEPENDENT booleans, never collapsed into a status enum, because "turn
    dispatched AND waiting on an answer" is real and is the single state an
    operator most needs to see. Constructs it directly: hold a real turn
    open at the LLM boundary (turn_active True) and, while it is still open,
    separately enqueue a real intervention on the SAME session (iv_waiting
    True) — both bools true at once, read off one all_sessions_status() row."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    session = reg.get_session("alice")

    iv = UserIntervention(kind="ask_user", prompt="Q?")
    await session._put_inbox("user", {"text": "hello", "chain_id": "c-5729-both"})
    turn_task = asyncio.create_task(session.run_one_iteration())
    try:
        await _llm_stub.call_started.wait()
        iv_task = asyncio.ensure_future(session.interventions.dispatch(iv))
        await asyncio.sleep(0)

        row = reg.all_sessions_status()[0]
        assert row["turn_active"] is True
        assert row["iv_waiting"] is True
    finally:
        await session.interventions.deliver_answer(iv, "ok")
        await iv_task
        _llm_stub.release.set()
        await turn_task

    row = reg.all_sessions_status()[0]
    assert row["turn_active"] is False
    assert row["iv_waiting"] is False
