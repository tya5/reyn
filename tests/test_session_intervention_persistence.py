"""Tier 2: Session invariant — intervention dispatch/resolve hits the WAL.

PR-intervention-link L3. The session-level wrappers
``_dispatch_intervention`` / ``_deliver_answer_to`` /
``_drop_interventions_for_run`` must route through the SnapshotJournal so
WAL ``intervention_dispatched`` / ``intervention_resolved`` events are
emitted. Without these, an in-flight intervention can't survive a crash.

Invariants:
  - dispatch fires ``intervention_dispatched`` with a serialized iv_dict
    BEFORE the future await blocks (so a crash mid-await leaves the WAL
    with the dispatch on disk).
  - successful answer fires ``intervention_resolved``.
  - unknown-choice answer does NOT fire resolve (intervention still pending).
  - drop_for_run fires resolve for each dropped iv (snapshot prune).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.chat.session import Session
from reyn.core.events.state_log import StateLog
from reyn.user_intervention import (
    InterventionChoice,
    UserIntervention,
)

# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_session_invariants.py pattern)
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> Session:
    """Build a Session redirected to ``tmp_path`` via public kwargs.

    issue #254 Phase 1: register a placeholder listener so the registry's
    ``enforce_listener_presence=True`` short-circuit does not fire — these
    tests dispatch interventions and verify WAL persistence, treating the
    test itself as the listener that will resolve via ``deliver_answer``.
    """
    session = Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )
    session.register_intervention_listener("test")
    return session


def _iv(*, run_id: str | None = None, choices: list[InterventionChoice] | None = None,
        prompt: str = "Q?", kind: str = "ask_user") -> UserIntervention:
    iv = UserIntervention(
        kind=kind, prompt=prompt, run_id=run_id, choices=choices or [],
    )
    iv.future = asyncio.get_running_loop().create_future()
    return iv


def _wal_events(tmp_path: Path) -> list[dict]:
    log = StateLog(tmp_path / "state.wal")
    return list(log.iter_from(0))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_intervention_appends_wal_before_await(tmp_path, monkeypatch):
    """Tier 2: ``intervention_dispatched`` lands on disk before the future awaits.

    Crash-safety invariant: a crash mid-await must leave the WAL with the
    dispatch event so resume can re-enqueue. We verify by inspecting the
    WAL file after the dispatch coroutine has yielded (= the await is
    blocking on the future).
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    iv = _iv(run_id="rA", prompt="What's your name?")
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    # Yield so the dispatch coroutine reaches `await iv.future`
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    events = _wal_events(tmp_path)
    dispatched = [e for e in events if e["kind"] == "intervention_dispatched"]
    assert dispatched, (
        f"expected intervention_dispatched in WAL while awaiting; "
        f"got {[e['kind'] for e in events]}"
    )
    ev = dispatched[0]
    assert ev["intervention_id"] == iv.id
    assert ev["target"] == "alpha"
    iv_dict = ev["iv_dict"]
    assert iv_dict["kind"] == "ask_user"
    assert iv_dict["prompt"] == "What's your name?"
    assert iv_dict["run_id"] == "rA"
    assert "future" not in iv_dict

    # Resolve and clean up
    iv.future.set_result(None)
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_deliver_answer_appends_intervention_resolved(tmp_path, monkeypatch):
    """Tier 2: successful answer → ``intervention_resolved`` in WAL."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    iv = _iv(prompt="Free text?")
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    consumed = await session._maybe_answer_oldest_intervention("Alice")
    assert consumed is True
    # Let the dispatch coroutine resume past `await iv.future` and run its
    # finally clause where ``record_intervention_resolved`` fires.
    await asyncio.gather(task, return_exceptions=True)

    events = _wal_events(tmp_path)
    resolved = [e for e in events if e["kind"] == "intervention_resolved"]
    assert resolved, "intervention_resolved must be emitted after successful answer"
    assert resolved[0]["intervention_id"] == iv.id
    assert resolved[0]["target"] == "alpha"


@pytest.mark.asyncio
async def test_unknown_choice_does_not_emit_resolved(tmp_path, monkeypatch):
    """Tier 2: unknown-choice answer leaves the intervention pending — no resolve."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    choices = [
        InterventionChoice(id="yes", label="[Y]es", hotkey="y"),
        InterventionChoice(id="no", label="[N]o", hotkey="n"),
    ]
    iv = _iv(choices=choices, prompt="Confirm?")
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    consumed = await session._maybe_answer_oldest_intervention("invalid")
    assert consumed is True  # consumed but not resolved

    events = _wal_events(tmp_path)
    dispatched = [e for e in events if e["kind"] == "intervention_dispatched"]
    resolved = [e for e in events if e["kind"] == "intervention_resolved"]
    assert dispatched, "intervention_dispatched must be present in WAL"
    assert resolved == [], (
        f"unknown-choice answer must NOT emit resolve; got {resolved}"
    )

    # Now resolve correctly to clean up
    await session._maybe_answer_oldest_intervention("y")
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_drop_for_run_emits_resolved_for_each_dropped(tmp_path, monkeypatch):
    """Tier 2: cancelling a skill run emits resolve for each dropped intervention.

    Without this, the snapshot would still show outstanding entries that
    are no longer awaitable — restore would re-enqueue dead interventions.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    iv1 = _iv(run_id="rA", prompt="Q1")
    iv2 = _iv(run_id="rA", prompt="Q2")
    iv3 = _iv(run_id="rB", prompt="Q3")

    t1 = asyncio.ensure_future(session._dispatch_intervention(iv1))
    t2 = asyncio.ensure_future(session._dispatch_intervention(iv2))
    t3 = asyncio.ensure_future(session._dispatch_intervention(iv3))
    for _ in range(4):
        await asyncio.sleep(0)

    session._drop_interventions_for_run("rA")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    events = _wal_events(tmp_path)
    resolved = [e for e in events if e["kind"] == "intervention_resolved"]
    resolved_ids = {e["intervention_id"] for e in resolved}
    assert resolved_ids == {iv1.id, iv2.id}, (
        f"drop_for_run must emit resolve for rA's iv1+iv2; got {resolved_ids}"
    )

    # Clean up
    iv3.future.set_result(None)
    await asyncio.gather(t1, t2, t3, return_exceptions=True)


@pytest.mark.asyncio
async def test_outstanding_interventions_in_snapshot_after_dispatch(tmp_path, monkeypatch):
    """Tier 2: snapshot file on disk reflects the dispatched intervention.

    Crash-recovery invariant: a process restart reads the snapshot file
    and sees outstanding_interventions populated, ready to be re-enqueued.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    iv = _iv(run_id="rZ", prompt="Persisted?")
    task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Snapshot path is the one passed via the public kwarg in _make_session
    snap_path = tmp_path / "alpha_snapshot.json"
    assert snap_path.is_file(), "snapshot must be persisted to disk"
    raw = json.loads(snap_path.read_text())
    outstanding = raw.get("outstanding_interventions", {})
    assert iv.id in outstanding
    assert outstanding[iv.id]["prompt"] == "Persisted?"

    iv.future.set_result(None)
    await asyncio.gather(task, return_exceptions=True)
