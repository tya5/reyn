"""Tier 2: agent-label row Space renders a meaningful preview (H-F1).

Wave-10 Topic H finding F1 (P2): cursoring to an agent-label row
(the "myagent ○ idle" root line) and pressing Space opened the
preview pane but ``_show_agent_in_preview`` fell through to
``pane.clear()``, leaving the user staring at an empty preview
with no feedback. Space appeared to "work" (pane layout shifted)
but produced no content — confusing for a tab whose other rows
(running_skill / running_plan / recent_skill / recent_plan)
render rich previews.

After the fix, ``_preview_agent`` renders a compact summary
(name + status badge + attached/loaded flags + running-skill
count + head of run_id list) sourced from the same data the
``render_agents`` tree uses, so the preview can never disagree
with the row visible above it.

Public surfaces tested:
  - cursoring to an agent-label item + ``_show_agent_in_preview``
    populates ``pane.title`` with the agent name (= no longer
    empty)
  - the rendered body carries status / attached / loaded text
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _StubPane:
    """Minimal _PreviewPane stand-in capturing show_text / clear calls."""

    def __init__(self) -> None:
        self.last_title: str | None = None
        self.last_renderable = None
        self.clear_count = 0

    def show_text(self, title: str, renderable) -> None:  # type: ignore[no-untyped-def]
        self.last_title = title
        self.last_renderable = renderable

    def clear(self) -> None:
        self.clear_count += 1


@pytest.mark.asyncio
async def test_agent_label_preview_renders_name_and_status() -> None:
    """Tier 2: agent-label preview surfaces name + status + flags.

    Pre-fix the kind="agent" branch fell to ``pane.clear()`` (= empty
    preview). After the fix the pane.show_text path is exercised with
    a non-empty title + rendered body.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "agents"
        # Stub the flat-items list with a single agent-label row at
        # cursor 0. The agent kind item shape is what
        # ``render_agents`` emits at agents_tab.py:528-533.
        panel._agents_items = [
            {
                "kind": "agent",
                "name": "test-agent",
                "attached": True,
                "loaded": True,
            },
        ]
        panel._agents_cursor = 0
        # No running skills in exec_state → "ready" badge.
        panel._exec_state = {}

        stub = _StubPane()
        panel._show_agent_in_preview(stub)

        # Body was populated (= not the legacy silent clear path).
        assert stub.clear_count == 0, "agent kind must not fall through to clear()"
        assert stub.last_title == "test-agent"
        # Render the captured renderable to plain text + verify content.
        from rich.console import Console
        out = Console(file=None, record=True, force_terminal=False)
        out.print(stub.last_renderable)
        plain = out.export_text()
        assert "test-agent" in plain
        # Loaded + not running → "ready" badge.
        assert "ready" in plain
        # Flag rows.
        assert "attached:" in plain and "yes" in plain
        assert "loaded:" in plain
        assert "running skills:" in plain


@pytest.mark.asyncio
async def test_agent_label_preview_running_badge_when_skills_in_flight() -> None:
    """Tier 2: running skill in exec_state → ``running`` badge."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "agents"
        panel._agents_items = [
            {
                "kind": "agent",
                "name": "busy-agent",
                "attached": False,
                "loaded": True,
            },
        ]
        panel._agents_cursor = 0
        panel._exec_state = {
            "run_abcdef12": {"agent_name": "busy-agent", "phase": "p1"},
            "run_xyz98765": {"agent_name": "busy-agent", "phase": "p2"},
            "run_other001": {"agent_name": "other-agent"},
        }
        stub = _StubPane()
        panel._show_agent_in_preview(stub)
        assert stub.last_title == "busy-agent"
        from rich.console import Console
        out = Console(file=None, record=True, force_terminal=False)
        out.print(stub.last_renderable)
        plain = out.export_text()
        # 2 skills running for this agent (the third belongs to another).
        assert "running skills: 2" in plain
        # The third item must NOT be counted.
        assert "running skills: 3" not in plain
        # Both run_ids should appear in the head (truncated to 8 chars).
        assert "run_abcd" in plain or "run_xyz9" in plain


@pytest.mark.asyncio
async def test_non_agent_kind_still_routes_to_dedicated_handler() -> None:
    """Tier 2b: non-agent kinds keep their existing handlers (regression).

    The new "agent" branch must not interfere with running_skill /
    recent_skill / running_plan / recent_plan routing — those still
    call their dedicated ``_preview_*`` methods.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import RightPanel

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one(RightPanel)
        panel._panel_type = "agents"
        # Empty items list → still falls through to clear().
        panel._agents_items = []
        panel._agents_cursor = 0
        stub = _StubPane()
        panel._show_agent_in_preview(stub)
        assert stub.clear_count == 1, "empty items must clear()"
        # Unknown kind → still clears.
        panel._agents_items = [{"kind": "unknown_future_kind", "name": "x"}]
        stub2 = _StubPane()
        panel._show_agent_in_preview(stub2)
        assert stub2.clear_count == 1
