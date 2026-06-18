"""Tier 2: OS invariant — per-checkpoint anchor-text preview (#1547).

Real AnchorStore + SnapshotJournal + AgentRegistry + RewindMenuWidget (no mocks).
The anchor (truncated last user message) is captured at cut_generation, keyed by
WAL seq, surfaced additively by list_rewind_points, and rendered as a 2nd dim line.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.anchor_store import AnchorStore, truncate_anchor
from reyn.core.events.snapshot_generations import SnapshotGenerationStore
from reyn.core.events.state_log import StateLog
from reyn.interfaces.tui.widgets.branch_tree import build_branch_tree_rows
from reyn.interfaces.tui.widgets.rewind_menu import RewindMenuWidget
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.snapshot_journal import SnapshotJournal

# ── truncate_anchor ──────────────────────────────────────────────────────────


def test_truncate_anchor_collapses_and_truncates():
    """Tier 2: anchor collapses whitespace to one line + truncates with ellipsis."""
    assert truncate_anchor("  hello   world\n") == "hello world"
    long = "x" * 200
    out = truncate_anchor(long, limit=80)
    assert out.endswith("…")            # truncation marker appended
    assert len(out) < len(long)         # actually shortened (behavioral, not a size pin)


# ── AnchorStore ──────────────────────────────────────────────────────────────


def test_anchor_store_capture_get_prune(tmp_path):
    """Tier 2: capture/get round-trip; unknown seq = ""; prune drops below floor."""
    store = AnchorStore(tmp_path / "anchors.json")
    store.capture(10, "first")
    store.capture(20, "second")
    assert store.get(10) == "first"
    assert store.get(20) == "second"
    assert store.get(99) == ""              # unknown → empty
    store.capture(30, "")                    # empty text → no-op
    assert store.get(30) == ""

    removed = store.prune_below(20)
    assert removed == 1                      # seq 10 dropped
    assert store.get(10) == "" and store.get(20) == "second"


def test_anchor_store_persists_across_instances(tmp_path):
    """Tier 2: anchors are durable — a fresh store over the same path reads them."""
    AnchorStore(tmp_path / "a.json").capture(5, "kept")
    assert AnchorStore(tmp_path / "a.json").get(5) == "kept"


# ── cut_generation capture ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cut_generation_captures_anchor(tmp_path):
    """Tier 2: cut_generation(anchor=...) records it under the boundary seq; no anchor = no capture."""
    log = StateLog(tmp_path / "wal")
    seq = await log.append("inbox_put", target="a", msg_id="m", msg_kind="user", payload={})
    journal = SnapshotJournal(
        agent_name="a", snapshot_path=tmp_path / "snap.json", state_log=log,
        generation_store=SnapshotGenerationStore("a", tmp_path / "gens"),
    )
    anchors = AnchorStore(tmp_path / "anchors.json")
    journal.set_anchor_store(anchors)
    journal.snapshot.applied_seq = seq

    await journal.cut_generation(anchor="rewind me here")
    assert anchors.get(seq) == "rewind me here"

    # a later cut with no anchor (e.g. plan-step) leaves the slot empty.
    seq2 = await log.append("inbox_put", target="a", msg_id="n", msg_kind="user", payload={})
    journal.snapshot.applied_seq = seq2
    await journal.cut_generation()           # no anchor
    assert anchors.get(seq2) == ""


# ── list_rewind_points surfacing ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_rewind_points_surfaces_anchor(tmp_path):
    """Tier 2: list_rewind_points adds the anchor field additively (per WAL seq)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda _p: (_ for _ in ()).throw(AssertionError("no factory")),
        state_log=state_log,
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")

    seq = await state_log.append(
        "inbox_put", target="alpha", msg_id="m", msg_kind="user", payload={},
    )
    snap = AgentSnapshot.empty("alpha")
    snap.applied_seq = seq
    reg._store_for("alpha").record(snap)             # a generation at this seq
    reg.anchor_store.capture(seq, "what's the weather")

    rows = reg.list_rewind_points()
    row = next(r for r in rows if r["seq"] == seq)
    assert row["anchor"] == "what's the weather"     # surfaced additively
    assert set(row) >= {"seq", "ts", "kind", "anchor"}


# ── widget render ────────────────────────────────────────────────────────────


def test_widget_renders_anchor_dim_line():
    """Tier 2: RewindMenuWidget renders the per-checkpoint anchor as a dim line;
    empty = omitted. Tree mode (the only mode since #1561); per-checkpoint anchor
    restored for tree in #1576. Single branch so the anchor surfaces only via the
    per-row render (no fork header to carry it)."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": 3,
                 "parent_branch_id": None, "is_active": True}]
    with_anchor = RewindMenuWidget.from_tree_rows(build_branch_tree_rows(
        branches,
        [{"seq": 1, "ts": "", "kind": "turn", "anchor": "fix the parser bug", "branch_id": 0}],
    ))
    assert "fix the parser bug" in with_anchor.render().plain

    # additive: a row without an anchor renders no extra line.
    without = RewindMenuWidget.from_tree_rows(build_branch_tree_rows(
        branches,
        [{"seq": 1, "ts": "", "kind": "turn", "anchor": "", "branch_id": 0}],
    ))
    assert "fix the parser bug" not in without.render().plain
