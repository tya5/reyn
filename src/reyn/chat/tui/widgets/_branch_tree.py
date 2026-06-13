"""Pure branch-tree layout for the Phase-2 fork picker (ADR-0038 2b).

Turns the 2a substrate contract — `list_branches()` (tree topology) +
`list_rewind_points(include_abandoned=True)` (checkpoints, each carrying its
lineage-correct `branch_id`) — into a flat, ordered list of renderable rows for
`RewindMenuWidget`'s tree mode.

**Why `branch_id` per checkpoint (not range-intersection):** a naive
`[fork_point_seq, head_seq] ∩ checkpoints` over-includes, because an active
parent's seq-range physically *contains* an abandoned child's seqs (e.g. after
rewind-to-6-then-continue, the active range `[0,13]` spans the abandoned
segment `(6,11)`). Lineage-correct membership needs the substrate's
segment-ownership map, so 2a stamps each checkpoint with its owning `branch_id`
and 2b simply groups by it — no overlap bug. (Gap found build-first, 2025-06;
contract refined with sandbox_2.)

Pure functions only — no Textual / no I/O — so the layout is unit-testable
against synthetic contract-shape data without a running app or the live 2a
primitive.
"""
from __future__ import annotations

from collections import defaultdict

# Row "kind" tags. Header rows are decorators (NOT cursor-selectable); only
# checkpoint rows are selectable, and Enter on one dispatches checkout(seq)
# (tui-coder converged: every selectable row is a seq row, one Enter semantic).
ROW_HEADER = "header"
ROW_CHECKPOINT = "checkpoint"


def _branch_label(branch: dict, anchor_by_seq: dict[int, str]) -> str:
    """Human label for a branch header.

    Root (no fork point) → "main". A fork → ``fork @ #<seq>`` plus the #1547
    anchor (last-user-message preview) at the fork point, when available — e.g.
    ``fork @ #30  "run the tests"``.
    """
    fork_seq = branch.get("fork_point_seq")
    if not fork_seq or branch.get("parent_branch_id") is None:
        return "main"
    anchor = anchor_by_seq.get(fork_seq, "")
    base = f"fork @ #{fork_seq}"
    return f'{base}  "{anchor}"' if anchor else base


def build_branch_tree_rows(
    branches: list[dict],
    checkpoints: list[dict],
) -> list[dict]:
    """Flatten the branch tree into ordered renderable rows.

    Args:
        branches: ``list_branches()`` rows —
            ``{branch_id, fork_point_seq, head_seq, parent_branch_id, is_active}``.
        checkpoints: ``list_rewind_points(include_abandoned=True)`` rows —
            ``{seq, ts, kind, anchor, branch_id}``. ``branch_id`` is the
            **lineage-correct** owning branch (substrate-computed).

    Returns a flat list, DFS from the root branch, **active-first among
    siblings**; each branch emits a header (decorator) followed by its
    checkpoint rows (newest seq first). Row shapes::

        {"row": "header", "branch_id", "label", "head_seq", "is_active", "depth"}
        {"row": "checkpoint", "seq", "ts", "kind", "anchor", "branch_id", "depth"}
    """
    # Lineage-correct grouping: trust the substrate's per-checkpoint branch_id.
    cps_by_branch: dict[object, list[dict]] = defaultdict(list)
    for cp in checkpoints:
        cps_by_branch[cp.get("branch_id")].append(cp)
    anchor_by_seq = {
        cp["seq"]: cp.get("anchor", "") for cp in checkpoints if "seq" in cp
    }

    by_id = {b["branch_id"]: b for b in branches}
    children: dict[object, list[dict]] = defaultdict(list)
    roots: list[dict] = []
    for b in branches:
        parent = b.get("parent_branch_id")
        if parent is None or parent not in by_id:
            roots.append(b)
        else:
            children[parent].append(b)

    def _sibling_key(b: dict) -> tuple:
        # Active branch first; then by fork point (older fork higher).
        return (0 if b.get("is_active") else 1, b.get("fork_point_seq") or 0)

    rows: list[dict] = []

    def _emit(branch: dict, depth: int) -> None:
        rows.append({
            "row": ROW_HEADER,
            "branch_id": branch["branch_id"],
            "label": _branch_label(branch, anchor_by_seq),
            "head_seq": branch.get("head_seq"),
            "is_active": bool(branch.get("is_active")),
            "depth": depth,
        })
        for cp in sorted(
            cps_by_branch.get(branch["branch_id"], []),
            key=lambda c: c.get("seq", 0),
            reverse=True,
        ):
            rows.append({
                "row": ROW_CHECKPOINT,
                "seq": cp.get("seq"),
                "ts": cp.get("ts", ""),
                "kind": cp.get("kind", ""),
                "anchor": cp.get("anchor", ""),
                "branch_id": branch["branch_id"],
                "depth": depth + 1,
            })
        for child in sorted(children.get(branch["branch_id"], []), key=_sibling_key):
            _emit(child, depth + 1)

    for root in sorted(roots, key=_sibling_key):
        _emit(root, 0)
    return rows


def selectable_rows(rows: list[dict]) -> list[dict]:
    """Checkpoint rows only — headers are non-selectable decorators (Enter on a
    checkpoint = checkout(seq); the cursor never lands on a header)."""
    return [r for r in rows if r.get("row") == ROW_CHECKPOINT]


__all__ = [
    "ROW_HEADER",
    "ROW_CHECKPOINT",
    "build_branch_tree_rows",
    "selectable_rows",
]
