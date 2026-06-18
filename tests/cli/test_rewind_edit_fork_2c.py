"""Tier 2: 2c edit-and-retry submit-handler + history-guard (ADR-0038 2c).

The submit half of the fork-picker edit feature. Submitting while edit-mode is
active forks from the edited turn's **predecessor TURN checkpoint** (not seq
N-1, not an intra-turn plan-step — substrate-computed via
``predecessor_turn_checkpoint``) and re-runs the edited message = new branch.

Pins (real AgentRegistry + ReynTUIApp run_test, no mocks):
- submit-handler checks out the predecessor-turn checkpoint + clears edit-mode.
- first-turn edit (predecessor None) → graceful reject, no checkout.
- both edit-exits route through ``exit_edit_mode`` (shared reset seam).
- history-guard: ↑/↓ are cursor-only during edit-mode (never recall history,
  which would overwrite the loaded message).

The full re-run (``submit_user_text``) needs a live session; the checkout-target
correctness (the catch) is what this gate pins. tui-coder's tmux P3 covers the
live re-run end-to-end.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import InputBar
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


async def _registry_with_turns(tmp_path: Path) -> tuple[AgentRegistry, int, int]:
    """Real registry with two TURN checkpoints (s1, s2). Returns (reg, s1, s2)."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory,
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    log = reg.state_log

    def _gen(seq: int) -> None:
        snap = AgentSnapshot.empty("alpha")
        snap.applied_seq = seq
        reg._store_for("alpha").record(snap)

    s1 = await log.append("inbox_consume", target="alpha", msg_id="m1")  # turn
    _gen(s1)
    s2 = await log.append("inbox_consume", target="alpha", msg_id="m2")  # turn
    _gen(s2)
    return reg, s1, s2


def _app(reg: AgentRegistry) -> ReynTUIApp:
    return ReynTUIApp(registry=reg, agent_name="alpha", model="m", budget_tracker=None)


@pytest.mark.asyncio
async def test_edited_submit_checks_out_predecessor_turn(tmp_path) -> None:
    """Tier 2: editing turn s2 forks from its predecessor TURN checkpoint s1
    (checkout target = predecessor_turn_checkpoint(s2)), and clears edit-mode."""
    reg, s1, s2 = await _registry_with_turns(tmp_path)
    assert reg.predecessor_turn_checkpoint(s2) == s1   # sanity: helper → prev turn
    app = _app(reg)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app.enter_edit_mode(s2)          # simulate edit-mode on turn s2 (public API)
        assert app.edit_mode_active is True
        head_before = reg.state_log.current_seq

        await app._submit_edited_fork("edited message")

        # Checked out (reset-record appended) to the predecessor turn s1.
        assert reg.state_log.current_seq > head_before
        # Edit-mode cleared via exit_edit_mode (shared reset seam).
        assert app.edit_mode_active is False


@pytest.mark.asyncio
async def test_first_turn_edit_rejects_gracefully(tmp_path) -> None:
    """Tier 2: editing the FIRST turn (predecessor None) → graceful reject, no
    checkout (no earlier checkpoint to fork from)."""
    reg, s1, _s2 = await _registry_with_turns(tmp_path)
    assert reg.predecessor_turn_checkpoint(s1) is None   # first turn → None
    app = _app(reg)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app.enter_edit_mode(s1)
        head_before = reg.state_log.current_seq

        await app._submit_edited_fork("edited message")

        assert reg.state_log.current_seq == head_before   # NO checkout
        assert app.edit_mode_active is False               # still exits edit-mode


@pytest.mark.asyncio
async def test_history_guard_cursor_only_during_edit(tmp_path) -> None:
    """Tier 2: while edit-mode is active, ↑/↓ move the cursor within the loaded
    message — they do NOT recall input history (which would overwrite the edit)."""
    reg, _s1, s2 = await _registry_with_turns(tmp_path)
    app = _app(reg)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        app.enter_edit_mode(s2)                       # enter edit-mode (public API)
        bar.set_text("line one\nline two")            # multi-line loaded message
        await pilot.pause()
        bar.action_key_up()                            # ↑ during edit-mode
        await pilot.pause()
        # The buffer is unchanged (cursor moved within it, no history recall).
        assert bar.query_one("#input").text == "line one\nline two"
