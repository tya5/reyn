"""Tier 2: OS invariant — nested skill path via parent_run_id (R-D13).

When a parent skill spawns a child via ``run_skill``, the child's
SkillSnapshot must record the parent's run_id so:
  - ``/skill list`` can display the lineage (parent / child)
  - Future cascade-discard semantics can walk the tree
  - Forensic / debug logs show the spawn relationship

Pinned invariants:
  - SkillSnapshot persists parent_run_id (save/load round-trip).
  - Backward compat: snapshots without the field load with None.
  - SkillRegistry.start records parent_run_id on the snapshot AND in
    the WAL skill_started event.
  - /skill list lineage walk produces ``A / B / C`` for a 3-level chain.

Reference: PR-nested-skill-path (R-D13) in the active plan.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.chat.session import ChatSession
from reyn.events.state_log import StateLog
from reyn.skill.skill_registry import SkillRegistry
from reyn.skill.skill_snapshot import SkillSnapshot

# ---------------------------------------------------------------------------
# SkillSnapshot persistence
# ---------------------------------------------------------------------------


def test_snapshot_round_trips_parent_run_id(tmp_path: Path):
    """Tier 2: parent_run_id survives save → load."""
    snap = SkillSnapshot.empty("run_child", "child_skill", {"x": 1})
    snap.parent_run_id = "run_parent"
    p = tmp_path / "snap.json"
    snap.save(p)

    loaded = SkillSnapshot.load("run_child", p)
    assert loaded.parent_run_id == "run_parent"


def test_snapshot_default_parent_run_id_is_none(tmp_path: Path):
    """Tier 2: brand-new snapshot has parent_run_id=None (= top-level)."""
    snap = SkillSnapshot.empty("run_root", "root_skill", {})
    assert snap.parent_run_id is None
    p = tmp_path / "snap.json"
    snap.save(p)
    loaded = SkillSnapshot.load("run_root", p)
    assert loaded.parent_run_id is None


def test_snapshot_load_handles_legacy_no_parent_field(tmp_path: Path):
    """Tier 2: backward compat — old snapshot without parent_run_id field loads as None.

    Forward-compat is built on additive optional fields. Old runs that
    were checkpointed before R-D13 must continue to load (treated as
    top-level, since we don't have lineage info).
    """
    snap = SkillSnapshot.empty("run_legacy", "old_skill", {})
    p = tmp_path / "snap.json"
    snap.save(p)
    # Strip the field to simulate an old snapshot
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw.pop("parent_run_id", None)
    p.write_text(json.dumps(raw), encoding="utf-8")

    loaded = SkillSnapshot.load("run_legacy", p)
    assert loaded.parent_run_id is None


# ---------------------------------------------------------------------------
# SkillRegistry.start records parent_run_id
# ---------------------------------------------------------------------------


def _make_registry(tmp_path: Path) -> tuple[SkillRegistry, StateLog]:
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / "wal.jsonl")
    return SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
    ), log


def test_registry_start_persists_parent_run_id_on_snapshot(tmp_path: Path):
    """Tier 2: start(parent_run_id=...) writes the field on the snapshot file."""
    registry, _ = _make_registry(tmp_path)

    async def go():
        await registry.start(
            run_id="child_001", skill_name="child_skill",
            skill_input={}, parent_run_id="parent_001",
        )

    asyncio.run(go())
    snap = registry.get("child_001")
    assert snap is not None
    assert snap.parent_run_id == "parent_001"


def test_registry_start_no_parent_is_root(tmp_path: Path):
    """Tier 2: omitting parent_run_id leaves it None (= top-level)."""
    registry, _ = _make_registry(tmp_path)

    async def go():
        await registry.start(
            run_id="root_001", skill_name="root_skill", skill_input={},
        )

    asyncio.run(go())
    assert registry.get("root_001").parent_run_id is None


def test_registry_skill_started_wal_includes_parent_run_id(tmp_path: Path):
    """Tier 2: skill_started WAL event carries parent_run_id."""
    registry, log = _make_registry(tmp_path)

    async def go():
        await registry.start(
            run_id="child_002", skill_name="child", skill_input={},
            parent_run_id="parent_002",
        )

    asyncio.run(go())
    started = [e for e in log.iter_from(0) if e["kind"] == "skill_started"]
    assert started, "expected at least one skill_started WAL event"
    assert started[0]["parent_run_id"] == "parent_002"
    assert started[0]["run_id"] == "child_002"


# ---------------------------------------------------------------------------
# /skill list lineage walk
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path) -> ChatSession:
    return ChatSession(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "wal.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )


def _drain_outbox(session: ChatSession) -> list:
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


def test_skill_list_shows_root_skill_flat(tmp_path: Path, monkeypatch):
    """Tier 2: /skill list shows ``skill_name`` only for root runs."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True
    reg = session._get_skill_registry()
    assert reg is not None

    async def go():
        await reg.start(
            run_id="root_run", skill_name="solo_skill",
            skill_input={"type": "input", "data": {}},
        )
        await session._maybe_handle_slash("/skill list")

    asyncio.run(go())
    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs)
    # Lineage label is just the skill name (no slash)
    assert "solo_skill" in combined
    # No stray "/" formatting around it (the "/" appears only in nested chains)
    # Look for the line with our run_id
    line = next(
        (l for l in combined.splitlines() if "root_run" in l), ""
    )
    assert "solo_skill" in line
    # Nested separator " / " should not show for a root
    assert " / " not in line


def test_skill_list_shows_parent_child_lineage(tmp_path: Path, monkeypatch):
    """Tier 2: /skill list walks parent_run_id and renders ``parent / child``."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True
    reg = session._get_skill_registry()
    assert reg is not None

    async def go():
        await reg.start(
            run_id="parent_run", skill_name="parent_skill",
            skill_input={"type": "input", "data": {}},
        )
        await reg.start(
            run_id="child_run", skill_name="child_skill",
            skill_input={"type": "input", "data": {}},
            parent_run_id="parent_run",
        )
        await session._maybe_handle_slash("/skill list")

    asyncio.run(go())
    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs)
    # Find the child run's line
    child_line = next(
        (l for l in combined.splitlines() if "child_run" in l), ""
    )
    assert "parent_skill / child_skill" in child_line


def test_skill_list_handles_three_level_chain(tmp_path: Path, monkeypatch):
    """Tier 2: /skill list renders a grandparent / parent / child chain."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True
    reg = session._get_skill_registry()
    assert reg is not None

    async def go():
        await reg.start(
            run_id="g_run", skill_name="grandparent",
            skill_input={"type": "input", "data": {}},
        )
        await reg.start(
            run_id="p_run", skill_name="parent",
            skill_input={"type": "input", "data": {}},
            parent_run_id="g_run",
        )
        await reg.start(
            run_id="c_run", skill_name="child",
            skill_input={"type": "input", "data": {}},
            parent_run_id="p_run",
        )
        await session._maybe_handle_slash("/skill list")

    asyncio.run(go())
    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs)
    child_line = next(
        (l for l in combined.splitlines() if "c_run" in l), ""
    )
    assert "grandparent / parent / child" in child_line


def test_skill_list_handles_orphaned_child(tmp_path: Path, monkeypatch):
    """Tier 2: child whose parent is gone shows as flat (= no infinite walk).

    If the parent has completed (snapshot removed) but the child run is
    still active (e.g. the parent was a short-lived dispatcher), the
    /skill list display falls back to showing only the child's
    skill_name. No "/  child_skill" with a leading missing-parent gap.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True
    reg = session._get_skill_registry()
    assert reg is not None

    async def go():
        # Child references a parent that was never started here
        await reg.start(
            run_id="orphan_run", skill_name="lonely_child",
            skill_input={"type": "input", "data": {}},
            parent_run_id="ghost_parent",
        )
        await session._maybe_handle_slash("/skill list")

    asyncio.run(go())
    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs)
    line = next(
        (l for l in combined.splitlines() if "orphan_run" in l), ""
    )
    assert "lonely_child" in line
    assert " / " not in line, (
        f"orphan should display flat (parent unresolvable); got {line!r}"
    )
