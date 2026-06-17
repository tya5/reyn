"""Tier 2: chainlit-free /rewind web action logic (ADR-0038 2d-2).

`build_rewind_action_specs` (branch-tree rows → per-checkpoint cl.Action specs)
and `handle_rewind_checkout` (seq → registry.checkout → confirmation) are
chainlit-free so they're unit-testable without the chainlit runtime; the thin
`cl.Message`/`cl.Action` glue (app.py) consumes them and is verified in a real
chainlit env (tui-coder, importorskip-guarded). Real AgentRegistry — no mocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.core.events.state_log import StateLog
from reyn.interfaces.chainlit_app.rewind_actions import (
    build_rewind_action_specs,
    handle_rewind_checkout,
    resolve_edit_target,
)


def _no_factory(_profile):
    raise AssertionError("session factory must not be called")


def test_action_specs_label_active_vs_fork() -> None:
    """Tier 2: each checkpoint → an action spec; active node label is plain,
    an inactive (dead-branch) node is tagged (fork) + the #1547 anchor shows."""
    branches = [
        {"branch_id": 0, "fork_point_seq": 0, "head_seq": 13, "parent_branch_id": None, "is_active": True},
        {"branch_id": 11, "fork_point_seq": 6, "head_seq": 10, "parent_branch_id": 0, "is_active": False},
    ]
    cps = [
        {"seq": 12, "ts": "", "kind": "turn", "anchor": "deploy fix", "branch_id": 0},
        {"seq": 9, "ts": "", "kind": "turn", "anchor": "try other", "branch_id": 11},
    ]
    specs = build_rewind_action_specs(branches, cps)
    by_seq = {s["seq"]: s for s in specs}
    assert by_seq[12]["is_active"] is True
    assert "deploy fix" in by_seq[12]["label"] and "(fork)" not in by_seq[12]["label"]
    assert by_seq[9]["is_active"] is False
    assert "(fork)" in by_seq[9]["label"] and "try other" in by_seq[9]["label"]


def test_action_specs_editable_only_on_turns() -> None:
    """Tier 2: turn checkpoints are editable (glue renders the ✎ action);
    non-turn (plan-step) checkpoints are checkout-only (decision A)."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": 9, "parent_branch_id": None, "is_active": True}]
    cps = [
        {"seq": 8, "ts": "", "kind": "turn", "anchor": "", "branch_id": 0},
        {"seq": 5, "ts": "", "kind": "plan_step", "anchor": "", "branch_id": 0},
    ]
    by_seq = {s["seq"]: s for s in build_rewind_action_specs(branches, cps)}
    assert by_seq[8]["editable"] is True
    assert by_seq[5]["editable"] is False


def test_resolve_edit_target_no_registry() -> None:
    """Tier 2: resolve_edit_target with no registry → can_edit False, no raise."""
    info = resolve_edit_target(None, 5)
    assert info["can_edit"] is False and info["fork_target"] is None


def test_action_specs_only_checkpoints() -> None:
    """Tier 2: header (branch decorator) rows never become actions — only seq rows."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": 6, "parent_branch_id": None, "is_active": True}]
    cps = [{"seq": 3, "ts": "", "kind": "turn", "anchor": "", "branch_id": 0}]
    specs = build_rewind_action_specs(branches, cps)
    assert [s["seq"] for s in specs] == [3]


@pytest.mark.asyncio
async def test_handle_checkout_invokes_registry(tmp_path) -> None:
    """Tier 2: handle_rewind_checkout runs registry.checkout(seq) + returns a
    confirmation naming the seq (non-default round-trip: WAL grows)."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory,
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    log = reg.state_log
    s1 = await log.append("inbox_consume", target="alpha", msg_id="m1")
    await log.append("inbox_consume", target="alpha", msg_id="m2")
    head_before = log.current_seq

    msg = await handle_rewind_checkout(reg, s1)

    assert log.current_seq > head_before          # checkout ran
    assert "checked out" in msg and f"seq {s1}" in msg


@pytest.mark.asyncio
async def test_handle_checkout_no_registry() -> None:
    """Tier 2: handle_rewind_checkout with no registry → graceful message."""
    msg = await handle_rewind_checkout(None, 5)
    assert "unavailable" in msg


@pytest.mark.asyncio
async def test_handle_checkout_no_seq(tmp_path) -> None:
    """Tier 2: a button payload missing ``seq`` → graceful message, never a
    ``checkout(None)`` raise into the browser thread."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory,
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
    )
    msg = await handle_rewind_checkout(reg, None)
    assert "unavailable" in msg
