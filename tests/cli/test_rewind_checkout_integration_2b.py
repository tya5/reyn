"""Tier 2: 2b fork-picker integration — real registry → tree → checkout.

The #1556-lesson guard: the registry=None path is covered elsewhere, but the
construction-forwarding risk (a runtime error when the checkout dispatch meets a
*real* registry) is only caught by driving the live methods against a real
``AgentRegistry``. This exercises the full data path:

    list_branches() + list_rewind_points(include_abandoned=True)
        → build_branch_tree_rows  (app._build_rewind_tree_rows)
    Enter → registry.checkout(seq)  (app._do_checkout)

with a real fork (active + dead branch) on the WAL. No DOM mount needed — the
data path is what the integration risk lives in; the render is covered by the
tree-mode run_test tests.

Real AgentRegistry + StateLog + ReynTUIApp — no mocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets._branch_tree import ROW_HEADER
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


async def _make_forked_registry(tmp_path: Path) -> AgentRegistry:
    """Real registry with an active branch + one dead (rewound-past) branch."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory,
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    log = reg.state_log

    def _gen(name: str, seq: int) -> None:
        snap = AgentSnapshot.empty(name)
        snap.applied_seq = seq
        reg._store_for(name).record(snap)

    s1 = await log.append("inbox_consume", target="alpha", msg_id="m1")
    s2 = await log.append("inbox_consume", target="alpha", msg_id="m2")
    for s in (s1, s2):
        _gen("alpha", s)
    # Checkout back to s1 → abandons (s1, R): s2 becomes a dead branch.
    await reg.checkout(s1)
    s3 = await log.append("inbox_consume", target="alpha", msg_id="m3")
    _gen("alpha", s3)
    return reg


@pytest.mark.asyncio
async def test_build_rewind_tree_rows_real_registry_has_both_branches(tmp_path) -> None:
    """Tier 2: the live data path (list_branches + list_rewind_points(
    include_abandoned) → build_branch_tree_rows) yields a tree with the active
    AND the dead branch — no construction-forwarding error against a real
    registry."""
    reg = await _make_forked_registry(tmp_path)
    app = ReynTUIApp(registry=reg, agent_name="alpha", model="m", budget_tracker=None)

    rows = app._build_rewind_tree_rows()
    headers = [r for r in rows if r.get("row") == ROW_HEADER]
    actives = [h for h in headers if h["is_active"]]
    deads = [h for h in headers if not h["is_active"]]
    assert actives, "active branch header missing"
    assert deads, "dead (rewound-past) branch header missing — fork not surfaced"


@pytest.mark.asyncio
async def test_do_checkout_invokes_registry_checkout(tmp_path) -> None:
    """Tier 2: the Enter dispatch (_do_checkout) actually runs registry.checkout
    against a real registry — the WAL grows a reset-record (proof the unified
    checkout primitive was driven, not rewind_to)."""
    reg = await _make_forked_registry(tmp_path)
    app = ReynTUIApp(registry=reg, agent_name="alpha", model="m", budget_tracker=None)
    head_before = reg.state_log.current_seq

    # Checkout to seq 1 (an active-branch node → undo flavour) via the dispatch.
    await app._do_checkout(1)

    assert reg.state_log.current_seq > head_before  # a reset-record was appended
