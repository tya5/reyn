"""Tier 2: branch-tree layout for the Phase-2 fork picker (ADR-0038 2b).

Pure layout over the locked 2a contract — `list_branches()` rows + checkpoints
carrying lineage-correct `branch_id`. The keystone test pins the **build-first
contract catch**: group-by-`branch_id` correctly separates an abandoned child's
checkpoint from the active parent, where the earlier `[fork_point, head]`
range-intersection over-included it.

No mocks, no app — pure functions over synthetic contract-shape data.
"""
from __future__ import annotations

from reyn.chat.tui.widgets._branch_tree import (
    ROW_CHECKPOINT,
    ROW_HEADER,
    build_branch_tree_rows,
    selectable_rows,
)


def _cp(seq: int, branch_id, *, kind: str = "turn", anchor: str = "") -> dict:
    return {"seq": seq, "ts": f"t{seq}", "kind": kind, "anchor": anchor, "branch_id": branch_id}


def test_group_by_branch_id_is_lineage_correct() -> None:
    """Tier 2: the over-inclusion repro — group-by-branch_id assigns the
    abandoned checkpoint to the abandoned branch, NOT the active parent whose
    seq-range physically spans it.

    Scenario: root seqs 1..10 (cp 3,6,9); rewind-to-6 (abandons 7..10);
    continue 12,13 (cp 12). Active range [0,13] CONTAINS abandoned cp #9, but
    the substrate stamps #9 with the abandoned branch_id, so it groups there.
    """
    branches = [
        {"branch_id": 0, "fork_point_seq": 0, "head_seq": 13, "parent_branch_id": None, "is_active": True},
        {"branch_id": 11, "fork_point_seq": 6, "head_seq": 10, "parent_branch_id": 0, "is_active": False},
    ]
    checkpoints = [
        _cp(3, 0), _cp(6, 0), _cp(12, 0),   # active branch (substrate-tagged)
        _cp(9, 11),                          # abandoned branch
    ]
    rows = build_branch_tree_rows(branches, checkpoints)

    active_seqs = {r["seq"] for r in rows if r["row"] == ROW_CHECKPOINT and r["branch_id"] == 0}
    abandoned_seqs = {r["seq"] for r in rows if r["row"] == ROW_CHECKPOINT and r["branch_id"] == 11}
    assert active_seqs == {3, 6, 12}      # NOT {3,6,9,12} — #9 is not here
    assert abandoned_seqs == {9}          # #9 correctly on the abandoned branch


def test_headers_are_non_selectable() -> None:
    """Tier 2: every selectable row is a checkpoint (seq) row; headers are
    decorators the cursor skips (tui-coder converged invariant)."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": 6, "parent_branch_id": None, "is_active": True}]
    rows = build_branch_tree_rows(branches, [_cp(3, 0), _cp(6, 0)])
    assert any(r["row"] == ROW_HEADER for r in rows)
    sel = selectable_rows(rows)
    assert all(r["row"] == ROW_CHECKPOINT for r in sel)
    assert {r["seq"] for r in sel} == {3, 6}


def test_fork_nested_under_parent_with_depth() -> None:
    """Tier 2: a fork's header nests below its parent (greater depth)."""
    branches = [
        {"branch_id": 0, "fork_point_seq": 0, "head_seq": 9, "parent_branch_id": None, "is_active": True},
        {"branch_id": 5, "fork_point_seq": 3, "head_seq": 7, "parent_branch_id": 0, "is_active": False},
    ]
    rows = build_branch_tree_rows(branches, [_cp(6, 0), _cp(5, 5)])
    root_hdr = next(r for r in rows if r["row"] == ROW_HEADER and r["branch_id"] == 0)
    fork_hdr = next(r for r in rows if r["row"] == ROW_HEADER and r["branch_id"] == 5)
    assert fork_hdr["depth"] > root_hdr["depth"]


def test_active_branch_first_among_siblings() -> None:
    """Tier 2: the active branch renders before inactive siblings."""
    branches = [
        {"branch_id": 0, "fork_point_seq": 0, "head_seq": 20, "parent_branch_id": None, "is_active": False},
        {"branch_id": 3, "fork_point_seq": 2, "head_seq": 10, "parent_branch_id": 0, "is_active": True},
        {"branch_id": 8, "fork_point_seq": 2, "head_seq": 15, "parent_branch_id": 0, "is_active": False},
    ]
    rows = build_branch_tree_rows(branches, [_cp(5, 3), _cp(12, 8)])
    header_ids = [r["branch_id"] for r in rows if r["row"] == ROW_HEADER]
    # branch 3 (active) before branch 8 (inactive) among the children of root.
    assert header_ids.index(3) < header_ids.index(8)


def test_single_branch_degrades_to_flat() -> None:
    """Tier 2: with no forks, the tree = one header + its checkpoints (newest
    first) — visually the Phase-1 flat list."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": 9, "parent_branch_id": None, "is_active": True}]
    rows = build_branch_tree_rows(branches, [_cp(3, 0), _cp(6, 0), _cp(9, 0)])
    assert rows[0]["row"] == ROW_HEADER and rows[0]["label"] == "main"
    seqs = [r["seq"] for r in rows if r["row"] == ROW_CHECKPOINT]
    assert seqs == [9, 6, 3]   # newest first


def test_fork_label_uses_anchor_at_fork_point() -> None:
    """Tier 2: a fork header label carries the #1547 anchor at its fork point."""
    branches = [
        {"branch_id": 0, "fork_point_seq": 0, "head_seq": 9, "parent_branch_id": None, "is_active": True},
        {"branch_id": 5, "fork_point_seq": 3, "head_seq": 7, "parent_branch_id": 0, "is_active": False},
    ]
    # The fork point (#3) lives on the parent branch and carries the anchor.
    checkpoints = [_cp(3, 0, anchor="run the tests"), _cp(6, 0), _cp(5, 5)]
    rows = build_branch_tree_rows(branches, checkpoints)
    fork_hdr = next(r for r in rows if r["row"] == ROW_HEADER and r["branch_id"] == 5)
    assert "#3" in fork_hdr["label"]
    assert "run the tests" in fork_hdr["label"]
