"""Tier 2: ChatSession.reset_for_rewind — pre-rewind in-memory residue clearing.

ADR-0038 Stage 1c-2. Real `ChatSession` + `StateLog` (no mocks). The global
rewind path calls ``reset_for_rewind()`` after ``await_quiescent`` and before
``restore_state(reconstructed)``; its clear-scope must EXACTLY mirror
``restore_state``'s set-scope so re-adopting the reconstructed snapshot leaves
ZERO pre-rewind residue (a single missed holder = stale state on the rewound
branch).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.session import ChatSession
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog
from reyn.user_intervention import InterventionAnswer, UserIntervention


def _session(tmp_path: Path, log: StateLog, *, agent: str = "alpha") -> ChatSession:
    session = ChatSession(
        agent_name=agent, state_log=log, snapshot_path=tmp_path / "snap.json",
    )
    session.register_intervention_listener("test")
    return session


@pytest.mark.asyncio
async def test_reset_for_rewind_then_restore_state_zero_residue(tmp_path):
    """Tier 2: reset_for_rewind + restore_state leaves ONLY the new snapshot.

    Populate every in-memory holder restore_state writes into with OLD markers,
    then reset_for_rewind + restore_state(a snapshot carrying only NEW state).
    The session's public views must reflect only the new snapshot — no OLD
    residue in inbox / chains / interventions / buffered answers / running tasks.
    """
    log = StateLog(tmp_path / "wal")
    session = _session(tmp_path, log)

    # ── populate pre-rewind live state (OLD markers) ──
    session.inbox.put_nowait(("user", {"text": "OLD"}))
    await session._chains.register(
        chain_id="OLD-chain", from_user=True, depth=0,
        original_text="old", sender=None,
    )
    session._buffered_intervention_answers["OLD-run"] = InterventionAnswer(text="OLD")
    old_iv = UserIntervention(kind="ask_user", prompt="OLD?")
    session._interventions._stalled[old_iv.id] = old_iv
    done_skill = asyncio.create_task(asyncio.sleep(0))
    session.running_skills["OLD-skill"] = done_skill
    await asyncio.sleep(0)  # let the dummy skill task settle (post-quiescent state)

    # ── reset, then adopt a reconstructed snapshot carrying only NEW state ──
    await session.reset_for_rewind()

    new_snap = AgentSnapshot.empty("alpha")
    new_snap.inbox = [{"id": "NEW", "kind": "user", "payload": {"text": "NEW"}}]
    session.restore_state(new_snap)

    # ── public views reflect ONLY the new snapshot — zero OLD residue ──
    chain_ids = session._chains.all_chain_ids()
    assert chain_ids == []                                  # OLD-chain cleared
    assert session.list_stalled_interventions() == []       # OLD iv cleared
    assert session.buffered_intervention_answers == {}       # OLD buffered cleared
    assert session.running_skills == {}                      # OLD skill handle dropped
    # inbox: OLD drained by reset; NEW re-queued by restore_state from the snapshot.
    drained = []
    while not session.inbox.empty():
        drained.append(session.inbox.get_nowait())
    assert drained == [("user", {"text": "NEW"})]


@pytest.mark.asyncio
async def test_reset_for_rewind_is_idempotent_on_clean_session(tmp_path):
    """Tier 2: reset_for_rewind on an already-empty session is a safe no-op."""
    log = StateLog(tmp_path / "wal")
    session = _session(tmp_path, log)

    await session.reset_for_rewind()  # nothing populated — must not raise

    chain_ids = session._chains.all_chain_ids()
    assert chain_ids == []
    assert session.list_stalled_interventions() == []
    assert session.buffered_intervention_answers == {}
    assert session.running_skills == {}
    assert session.inbox.empty()
