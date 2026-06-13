"""Tier 2: 2c edit pre-fill data-flow — InputBar.set_text + _prefill_edit (2c).

The data-flow half of the fork-picker edit feature (co-impl with tui-coder's
edit-mode binding). Pins:
- ``InputBar.set_text`` REPLACES the buffer (vs append_text's concatenate).
- ``_prefill_edit(seq)`` loads the **full** original message (AnchorStore.get_full,
  NOT the truncated display anchor) into the InputBar, so an edited re-run keeps
  the whole message.
- the tree footer advertises ``ctrl+e edit`` (discoverability; ctrl+e not bare
  ``e`` — printable keys are swallowed by the focused InputBar).

run_test real-DOM (mount-path) + a real AgentRegistry/AnchorStore — no mocks.
The submit-handler (predecessor-turn checkout + fork) lands once sandbox_2's
``predecessor_turn_checkpoint`` merges; this is the pre-fill + footer half.
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
from reyn.chat.tui.widgets import ConversationView, InputBar
from reyn.chat.tui.widgets._branch_tree import build_branch_tree_rows
from reyn.chat.tui.widgets.rewind_menu import RewindMenuWidget
from reyn.events.state_log import StateLog


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


@pytest.mark.asyncio
async def test_set_text_replaces_buffer() -> None:
    """Tier 2: InputBar.set_text replaces the buffer (not append) + cursor-to-end."""
    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        bar.append_text("a draft")
        await pilot.pause()
        bar.set_text("the original full message")
        await pilot.pause()
        ta = bar.query_one("#input")
        assert ta.text == "the original full message"   # replaced, not "a draft …"


@pytest.mark.asyncio
async def test_prefill_edit_loads_full_message(tmp_path) -> None:
    """Tier 2: _prefill_edit loads the FULL message (get_full), not the truncated
    anchor, into the InputBar."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory,
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    full_msg = "fix the auth bug in the login handler and add a regression test for it"
    reg.anchor_store.capture(42, "fix the auth bug…", full=full_msg)

    app = ReynTUIApp(registry=reg, agent_name="alpha", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._prefill_edit(42)
        await pilot.pause()
        ta = app.query_one("#inputbar", InputBar).query_one("#input")
        assert ta.text == full_msg                    # FULL, not the "…" anchor


@pytest.mark.asyncio
async def test_prefill_edit_no_message_is_noop(tmp_path) -> None:
    """Tier 2: _prefill_edit with no recorded full message → no-op (no crash,
    InputBar untouched) — non-turn / legacy checkpoint degrades gracefully."""
    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory,
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
    )
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    app = ReynTUIApp(registry=reg, agent_name="alpha", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        bar = app.query_one("#inputbar", InputBar)
        bar.set_text("draft")
        app._prefill_edit(999)   # no anchor for 999
        await pilot.pause()
        assert bar.query_one("#input").text == "draft"   # untouched


def test_tree_footer_advertises_ctrl_e_edit() -> None:
    """Tier 2: the tree footer surfaces ctrl+e edit (discoverability; ctrl+e
    because printable keys are swallowed by the focused InputBar)."""
    branches = [{"branch_id": 0, "fork_point_seq": 0, "head_seq": 6, "parent_branch_id": None, "is_active": True}]
    cps = [{"seq": 3, "ts": "", "kind": "turn", "anchor": "", "branch_id": 0}]
    w = RewindMenuWidget.from_tree_rows(build_branch_tree_rows(branches, cps))
    rendered = w.render().plain
    assert "ctrl+e" in rendered and "edit" in rendered
