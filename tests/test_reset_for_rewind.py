"""Tier 2: Session.reset_for_rewind — pre-rewind in-memory residue clearing.

ADR-0038 Stage 1c-2. Real `Session` + `StateLog` (no mocks). The global
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

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.user_intervention import InterventionAnswer, UserIntervention


def _session(tmp_path: Path, log: StateLog, *, agent: str = "alpha") -> Session:
    session = Session(
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
    await asyncio.sleep(0)

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
    # inbox: OLD drained by reset; NEW re-queued by restore_state from the snapshot.
    drained = []
    while not session.inbox.empty():
        drained.append(session.inbox.get_nowait())
    assert drained == [("user", {"text": "NEW"})]


def test_reset_for_rewind_clear_scope_covers_all_agentsnapshot_fields():
    """Tier 2: by-construction drift guard — clear-scope covers EVERY snapshot field.

    reset_for_rewind clears the in-memory mirror of each AgentSnapshot field that
    restore_state repopulates. This pin maps every field to its reset disposition;
    adding/removing an AgentSnapshot field breaks it, forcing the author to make
    the new field's disposition explicit. A missed mirror would be silent stale
    residue on the rewound branch — so drift must not pass silently.
    """
    disposition = {
        "agent_name": "identity — unchanged by rewind (same agent)",
        # FP-0043 Stage 5: a rewind operates within ONE session's timeline, so the
        # session id is identity-like — unchanged by rewind (same session), exactly
        # like agent_name. reset_for_rewind must NOT clear it.
        "session_id": "identity — unchanged by rewind (same session)",
        "applied_seq": "replaced wholesale by journal.install (no separate holder)",
        "inbox": "session.inbox drained",
        "pending_chains": "session._chains.reset()",
        "outstanding_interventions": "session._interventions.clear() + restore tasks",
        "buffered_intervention_answers": "session._buffered_intervention_answers cleared",
        "next_turn_context": "session._next_turn_context cleared",
        # #2884: the loop-valve counter mirror is reset to 0 (restore_state re-assigns
        # it wholesale from the reconstructed snapshot; the reset keeps zero-residue robust).
        "hook_driven_turns": "session._hook_driven_turns reset to 0",
    }
    assert set(disposition) == set(AgentSnapshot.__dataclass_fields__), (
        "AgentSnapshot fields changed — update reset_for_rewind (and this map) so "
        "the new/removed field's in-memory-mirror disposition is explicit; a "
        "missed holder is silent stale residue on the rewound branch."
    )


@pytest.mark.asyncio
async def test_reset_for_rewind_zeroes_hook_driven_turns_counter(tmp_path):
    """Tier 2: #2884 — reset_for_rewind zeroes the loop-valve counter mirror.

    Behavioral pin for the reset line (session.py reset_for_rewind → the
    ``_hook_driven_turns = 0`` step). Populate a NONZERO counter pre-rewind,
    call reset_for_rewind, assert the public counter is 0 post-reset. Stripping
    the reset line makes this go RED — closing the green-on-strip vector the
    field-coverage drift-guard alone leaves open.

    Scope note: for THIS counter the reset is belt-and-suspenders (the sole
    rewind call site, registry.py, runs ``restore_state`` on the immediately-
    following unconditional line, overwriting the counter wholesale from the
    reconstructed snapshot). This test pins the reset's own zero-residue
    behaviour regardless, so a future change that silently drops it is caught.
    """
    log = StateLog(tmp_path / "wal")
    session = _session(tmp_path, log)

    # arrange: a nonzero loop-valve counter (the pre-rewind residue to clear).
    session._hook_driven_turns = 5

    await session.reset_for_rewind()

    # assert via the public read-only accessor (no private-state assertion).
    assert session.hook_driven_turns == 0, (
        "reset_for_rewind must zero the hook-driven-turns loop-valve counter "
        "(stale nonzero residue would hand the post-rewind session a wrong "
        "max_hook_driven_turns valve budget)"
    )


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
    assert session.inbox.empty()
