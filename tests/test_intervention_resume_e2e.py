"""Tier 2/3: PR-intervention-link L6+L7 — skill resume picks up the user's answer.

The user-facing guarantee: after a crash that interrupts an ask_user
mid-await, the user can answer via /answer post-restart and the resuming
skill receives that answer (not a fresh duplicate prompt).

Scenario:
  Run 1:
    - Skill enters phase "ask" → ask_user op dispatches an intervention
    - Snapshot persists outstanding_interventions[iv_id]
    - Process crash mid-await (skill task dies, future never delivered)

  Run 2 (restart):
    - ChatSession.restore_state re-enqueues the intervention from snapshot
    - User answers via _maybe_answer_oldest_intervention
    - Watcher buffers the answer keyed by run_id
    - intervention_resolved emitted to WAL (snapshot pruned)

  Skill resume:
    - bus.request(iv) — at the same run_id — finds the buffered answer
      and returns it WITHOUT dispatching a new intervention
    - Skill continues with the recovered answer

Persistence note: the buffered answer lives in ChatSession in-memory
state. If the process crashes between the user's answer and the skill's
resume, the buffer is lost — that's R-D12 (durable answer buffering).
For most practical scenarios, restart → answer → skill resume happens
in one process lifetime so this race is acceptable.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.chat.session import ChatInterventionBus, ChatSession
from reyn.chat.services.snapshot_journal import SnapshotJournal
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionChoice,
    UserIntervention,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> ChatSession:
    wal_path = tmp_path / "state.wal"
    session = ChatSession(
        agent_name=agent_name,
        state_log=StateLog(wal_path),
    )
    session._snapshot_path = tmp_path / f"{agent_name}_snapshot.json"
    session._journal = SnapshotJournal(
        agent_name=agent_name,
        snapshot_path=session._snapshot_path,
        state_log=session._journal._state_log,
    )
    return session


def _snapshot_with_intervention(
    *, agent_name: str, iv_id: str, run_id: str, prompt: str = "Q?",
) -> AgentSnapshot:
    snap = AgentSnapshot.empty(agent_name)
    snap.outstanding_interventions[iv_id] = {
        "kind": "ask_user",
        "prompt": prompt,
        "detail": "",
        "choices": [],
        "suggestions": [],
        "run_id": run_id,
        "skill_name": "demo",
        "id": iv_id,
    }
    snap.applied_seq = 5
    return snap


# ---------------------------------------------------------------------------
# L6: bus checks buffer first
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_returns_buffered_answer_without_dispatching(tmp_path, monkeypatch):
    """Tier 2: ChatInterventionBus.request returns buffer hit if present.

    When a previous (crashed) run's intervention was answered post-restart,
    the answer is buffered keyed by run_id. The next bus.request from the
    resuming skill (same run_id) must short-circuit the dispatch path.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    # Pre-populate buffer (simulating what the L5 watcher does after the
    # user answers a restored intervention)
    session._buffered_intervention_answers["rResume"] = InterventionAnswer(
        text="Charlie", choice_id=None,
    )

    bus = ChatInterventionBus(session, run_id="rResume", skill_name="demo")
    iv = UserIntervention(kind="ask_user", prompt="Fresh Q?")
    iv.future = asyncio.get_running_loop().create_future()
    answer = await bus.request(iv)

    assert answer.text == "Charlie"
    # No dispatch happened — outbox should not have an intervention message
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    assert all(m.kind != "intervention" for m in msgs), (
        "buffered answer must NOT trigger announce/dispatch path"
    )


@pytest.mark.asyncio
async def test_bus_falls_through_to_dispatch_when_no_buffer(tmp_path, monkeypatch):
    """Tier 2: backward compat — empty buffer → normal dispatch path."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    bus = ChatInterventionBus(session, run_id="rFresh", skill_name="demo")
    iv = UserIntervention(kind="ask_user", prompt="What's up?")
    iv.future = asyncio.get_running_loop().create_future()

    # Resolve the future via deliver path so dispatch returns
    task = asyncio.ensure_future(bus.request(iv))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Now answer it
    consumed = await session._maybe_answer_oldest_intervention("Bob")
    assert consumed is True
    answer = await task
    assert answer.text == "Bob"


@pytest.mark.asyncio
async def test_buffer_is_single_use(tmp_path, monkeypatch):
    """Tier 2: a buffered answer is consumed once; second request gets fresh dispatch."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    session._buffered_intervention_answers["rOnce"] = InterventionAnswer(
        text="first", choice_id=None,
    )

    bus = ChatInterventionBus(session, run_id="rOnce", skill_name="demo")
    iv1 = UserIntervention(kind="ask_user", prompt="Q1?")
    iv1.future = asyncio.get_running_loop().create_future()
    a1 = await bus.request(iv1)
    assert a1.text == "first"

    # Buffer cleared — second request goes through dispatch
    iv2 = UserIntervention(kind="ask_user", prompt="Q2?")
    iv2.future = asyncio.get_running_loop().create_future()
    task = asyncio.ensure_future(bus.request(iv2))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await session._maybe_answer_oldest_intervention("second")
    a2 = await task
    assert a2.text == "second"


# ---------------------------------------------------------------------------
# L6: watcher buffers answer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_buffers_answer_when_restored_iv_resolves(tmp_path, monkeypatch):
    """Tier 2: restoring + answering populates the buffer for skill resume."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    snap = _snapshot_with_intervention(
        agent_name="alpha", iv_id="iv_restored", run_id="rW",
        prompt="Restored Q?",
    )
    session.restore_state(snap)
    for _ in range(3):
        await asyncio.sleep(0)

    # User answers
    consumed = await session._maybe_answer_oldest_intervention("hello world")
    assert consumed is True
    for _ in range(3):
        await asyncio.sleep(0)

    # Buffer populated
    buffered = session._buffered_intervention_answers.get("rW")
    assert buffered is not None
    assert buffered.text == "hello world"


# ---------------------------------------------------------------------------
# L7: full e2e (restore + answer + bus.request retrieves)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_skill_resume_picks_up_user_answer(tmp_path, monkeypatch):
    """Tier 3 e2e: restore → user answer → resuming skill's bus.request gets it.

    The headline guarantee for PR-intervention-link.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    # Phase 1: snapshot has an outstanding intervention from the crashed run
    snap = _snapshot_with_intervention(
        agent_name="alpha", iv_id="iv_crashed", run_id="rE2E",
        prompt="What's your name?",
    )
    session.restore_state(snap)
    for _ in range(3):
        await asyncio.sleep(0)

    # Phase 2: user answers
    consumed = await session._maybe_answer_oldest_intervention("Reyn")
    assert consumed is True
    for _ in range(3):
        await asyncio.sleep(0)

    # Verify intervention_resolved fired (snapshot pruned)
    log = StateLog(tmp_path / "state.wal")
    events = list(log.iter_from(0))
    resolved_ids = {
        e["intervention_id"] for e in events
        if e["kind"] == "intervention_resolved"
    }
    assert "iv_crashed" in resolved_ids

    # Phase 3: skill resumes — bus.request with the same run_id finds buffer
    bus = ChatInterventionBus(session, run_id="rE2E", skill_name="demo")
    fresh_iv = UserIntervention(kind="ask_user", prompt="What's your name?")
    fresh_iv.future = asyncio.get_running_loop().create_future()
    answer = await bus.request(fresh_iv)

    assert answer.text == "Reyn", (
        f"resuming skill must receive the user's previous answer; got {answer}"
    )
    # Buffer cleared after consumption
    assert "rE2E" not in session._buffered_intervention_answers


@pytest.mark.asyncio
async def test_e2e_choice_intervention_round_trip(tmp_path, monkeypatch):
    """Tier 3 e2e: choice-based intervention answer flows through buffer.

    Verifies that the choice_id on the InterventionAnswer survives the
    restore → buffer → consume path.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    snap = AgentSnapshot.empty("alpha")
    snap.outstanding_interventions["iv_choice"] = {
        "kind": "permission.generic",
        "prompt": "Allow?",
        "detail": "",
        "choices": [
            {"id": "yes", "label": "[Y]es", "hotkey": "y"},
            {"id": "no", "label": "[N]o", "hotkey": "n"},
        ],
        "suggestions": [],
        "run_id": "rChoice",
        "skill_name": "demo",
        "id": "iv_choice",
    }
    snap.applied_seq = 1
    session.restore_state(snap)
    for _ in range(3):
        await asyncio.sleep(0)

    consumed = await session._maybe_answer_oldest_intervention("y")
    assert consumed is True
    for _ in range(3):
        await asyncio.sleep(0)

    bus = ChatInterventionBus(session, run_id="rChoice", skill_name="demo")
    fresh_iv = UserIntervention(
        kind="permission.generic", prompt="Allow?",
        choices=[
            InterventionChoice(id="yes", label="[Y]es", hotkey="y"),
            InterventionChoice(id="no", label="[N]o", hotkey="n"),
        ],
    )
    fresh_iv.future = asyncio.get_running_loop().create_future()
    answer = await bus.request(fresh_iv)
    assert answer.choice_id == "yes"
