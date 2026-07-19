"""Tier 2: #2946 item 2 — ``restore_all`` (agent, session_id) bucketing, truncate-falsify.

Prior shape: ``restore_all`` computed ONE ``min_seq`` across every discovered
(agent, session) snapshot, materialised the shared WAL tail once, then handed
the SAME full tail list to EVERY snapshot's ``apply_events`` — which walks the
WHOLE list and calls ``_matches_agent`` per entry regardless of whether the
entry belongs to that snapshot. One lagging/idle agent's low ``applied_seq``
widens the tail for every OTHER agent's O(tail) walk too: O(agents × tail).

Fix: bucket the shared tail by ``AgentSnapshot.event_route_key`` (agent,
session_id) ONCE, then hand each snapshot only its own bucket — O(tail) total.

This test is a structural correctness proof for the bucketing (not a timing
benchmark): it proves reconstruction is IDENTICAL to the pre-fix behavior —
each (agent, session) restores exactly its own state, no cross-contamination
— AND is the CLAUDE.md recovery-feature PR gate truncate-falsify: the
architect-specified regression point is ``_matches_agent``'s ``session_id``
defaulting to "main" for entries written WITHOUT a session_id field (legacy
pre-FP-0043-Stage-5 WAL entries). A bucketing implementation that keys on
``event.get("session_id")`` WITHOUT the "main" default would silently drop
such an entry into a bucket no snapshot claims (key ``(agent, None)``) —
this test's session_id-less entry would then vanish from EVERY reconstruction,
not just survive-across-truncation. Real StateLog / AgentRegistry / Session
throughout (no mocks).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

AGENT = "alpha"


def _make_registry(tmp_path: Path, wal: Path) -> AgentRegistry:
    """A fresh registry over the SAME project_root + WAL file simulates a
    process restart: it re-discovers on-disk snapshots and replays the WAL
    tail (mirrors ``tests/test_multi_session_restore.py``)."""
    state_log = StateLog(wal)

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        return s

    return AgentRegistry(
        project_root=tmp_path,
        session_factory=_factory,
        state_log=state_log,
    )


def _iv_dict(iv_id: str, run_id: str) -> dict:
    return {
        "kind": "ask_user",
        "prompt": f"Q-{iv_id}?",
        "detail": "",
        "choices": [],
        "suggestions": [],
        "run_id": run_id,
        "actor": "demo",
        "id": iv_id,
    }


def _active_iv_ids(session: "Session") -> list[str]:
    return [iv.id for iv in session.interventions.list_active()]




@pytest.mark.asyncio
async def test_restore_all_buckets_by_agent_session_and_survives_truncation(tmp_path, monkeypatch):
    """Tier 2: #2946 item 2 truncate-falsify (CLAUDE.md recovery-feature PR gate).

    Set up main + a spawned session under one agent, each with distinct state
    plus ONE session_id-less (legacy pre-S5) WAL entry that must default into
    the MAIN bucket. Reconstruct once (bakes everything into durable
    per-session snapshots via the bucketed replay), truncate the WAL BELOW
    every source event, then reconstruct again from a FRESH registry over the
    SAME (now-truncated) WAL + on-disk snapshots. All three pieces of state —
    main's intervention, the spawned session's intervention, AND the legacy
    session_id-less inbox message — must survive, each still attached to
    exactly its own (agent, session) bucket (no cross-contamination).
    """
    monkeypatch.chdir(tmp_path)

    agent_dir = tmp_path / ".reyn" / "agents" / AGENT
    agent_dir.mkdir(parents=True)
    AgentProfile.new(AGENT, role="").save(agent_dir)
    wal = tmp_path / ".reyn" / "wal.jsonl"

    # ── run 1: assign distinct state to main + a spawned session ──
    reg1 = _make_registry(tmp_path, wal)
    main1 = reg1.get_or_load(AGENT)
    sid = reg1.spawn_session(AGENT, presentation_consumer=None, intervention_bridge=None)
    spawned1 = reg1.get_session(AGENT, sid)
    assert spawned1 is not None

    await main1.journal.record_intervention_dispatched(
        intervention_id="iv_main", iv_dict=_iv_dict("iv_main", "rM"),
    )
    await spawned1.journal.record_intervention_dispatched(
        intervention_id="iv_spawn", iv_dict=_iv_dict("iv_spawn", "rS"),
    )
    await main1.journal.flush()

    # a session_id-LESS entry (bypasses SnapshotJournal's funnel, which always
    # injects session_id=) — simulates a legacy pre-FP-0043-Stage-5 WAL entry.
    # `event_route_key`'s "main" default must route this into main's bucket.
    # Uses `intervention_dispatched` (like iv_main/iv_spawn above) rather than
    # `inbox_put`, hermetically — a restored `outstanding_intervention` just
    # re-enqueues for the user to answer, unlike a restored inbox message
    # (which can trigger a LIVE LLM turn on `ensure_running`, per
    # `test_multi_session_restore.py`'s docstring on this same hazard).
    state_log1 = reg1._state_log
    assert state_log1 is not None
    await state_log1.append(
        "intervention_dispatched", target=AGENT,
        intervention_id="iv_legacy", iv_dict=_iv_dict("iv_legacy", "rL"),
    )
    await state_log1.flush()
    await state_log1.aclose()

    # ── reconstruct #1: bakes the legacy entry's effect into the durable
    # per-session snapshots via the bucketed replay (this is what makes it
    # survive a LATER truncation — snapshot-backed, not WAL-derived). ──
    reg2 = _make_registry(tmp_path, wal)
    await reg2.restore_all()
    for _ in range(3):
        await asyncio.sleep(0)

    main2 = reg2.get_session(AGENT)
    spawned2 = reg2.get_session(AGENT, sid)
    assert main2 is not None and spawned2 is not None
    assert sorted(_active_iv_ids(main2)) == ["iv_legacy", "iv_main"], (
        "sanity: the legacy session_id-less entry must bucket into MAIN alongside iv_main"
    )
    assert _active_iv_ids(spawned2) == ["iv_spawn"], (
        "sanity: the legacy entry must NOT leak into the spawned bucket"
    )

    state_log2 = reg2._state_log
    assert state_log2 is not None

    # the source events (iv_main / iv_spawn / iv_legacy dispatch) are durable
    # below this point pre-truncation.
    pre_truncate_lines = [ln for ln in wal.read_text().splitlines() if ln.strip()]
    assert any('"iv_legacy"' in ln for ln in pre_truncate_lines), (
        "sanity: the iv_legacy source event must be durable pre-truncation"
    )

    # push filler far past every source event, then truncate BELOW them all.
    for i in range(150):
        await state_log2.append("inbox_put", target="filler-agent", n=i)
    floor = state_log2.current_seq - 5
    await state_log2.truncate_below(floor)
    await state_log2.flush()
    stats = state_log2.last_truncate_stats
    assert stats["dropped"] >= 140, f"filler + source events must be truncated; dropped={stats['dropped']}"

    post_truncate_lines = [ln for ln in wal.read_text().splitlines() if ln.strip()]
    assert not any('"iv_legacy"' in ln for ln in post_truncate_lines), (
        "the iv_legacy source event must actually be GONE from the WAL post-truncation "
        "(not just counted) — else this test would pass vacuously even for a "
        "WAL-derived (non-snapshot-backed) design"
    )
    assert not any('"iv_main"' in ln for ln in post_truncate_lines)
    assert not any('"iv_spawn"' in ln for ln in post_truncate_lines)

    await state_log2.aclose()  # simulate the crash: tear down run2's WAL worker

    # ── reconstruct #2: fresh registry over the SAME (now-truncated) WAL +
    # on-disk snapshots. All three pieces of state must survive AND stay
    # correctly bucketed — this is what would go RED if the bucketing
    # implementation dropped the session_id "main" default (iv_legacy would
    # vanish entirely — its post-#2946 bucket key would be (AGENT, None),
    # which no snapshot's (AGENT, "main") key matches) or cross-routed
    # entries between (agent, session) buckets. ──
    reg3 = _make_registry(tmp_path, wal)
    await reg3.restore_all()
    for _ in range(3):
        await asyncio.sleep(0)

    main3 = reg3.get_session(AGENT)
    spawned3 = reg3.get_session(AGENT, sid)
    assert main3 is not None, "main session must survive truncation below its source events"
    assert spawned3 is not None, "spawned session must survive truncation below its source events"

    assert sorted(_active_iv_ids(main3)) == ["iv_legacy", "iv_main"], (
        "main's interventions (INCLUDING the session_id-less legacy one) must survive WAL "
        "truncation, snapshot-backed rather than WAL-derived, and still default into MAIN's "
        "bucket post-reconstruction (the architect-specified regression point)"
    )
    assert _active_iv_ids(spawned3) == ["iv_spawn"], (
        "spawned session's intervention must survive WAL truncation, isolated from main, "
        "with no leakage of the legacy entry into the spawned bucket"
    )
    # cross-contamination guard, both directions (mirrors test_multi_session_restore.py).
    assert "iv_spawn" not in _active_iv_ids(main3)
    assert "iv_main" not in _active_iv_ids(spawned3)
    assert "iv_legacy" not in _active_iv_ids(spawned3)

    reg3_state_log = reg3._state_log
    assert reg3_state_log is not None
    await reg3_state_log.aclose()
