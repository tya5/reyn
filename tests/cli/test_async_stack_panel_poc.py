"""Tier 2: AsyncStackPanel entry state mgmt + ordering + cap behaviour.

issue #427 L4 step 5 PoC — widget shape only, no production wiring
(= agent registry subscription is a follow-up). Pins the contract
that step 6 / production code can rely on.

Contract pinned here:

1. ``add(agent_id, summary)`` mounts a running entry visible in the
   rendered output and the public ``snapshot()`` view.
2. ``add`` is idempotent — same ``agent_id`` updates instead of
   double-mounting.
3. ``set_pending(agent_id, count)`` switches the entry's glyph + carries
   the pending count in the row.
4. ``set_running(agent_id, summary)`` reverses the pending state.
5. ``remove(agent_id)`` drops the entry (= invisible in render +
   snapshot).
6. Ordering: pending entries (= ⚑) come first, then running entries
   sorted by elapsed (= shortest first). Empty / non-existent
   ``agent_id`` calls degrade safely.
7. Cap behaviour: more than ``_CAP`` (= 5) entries collapse the tail to
   a ``"… +N more (panel for all)"`` overflow row visible at the end
   of the rendered output and as the final ``snapshot()`` entry with
   ``is_overflow=True``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.widgets.async_stack_panel import (  # noqa: E402
    AsyncStackPanel,
)


class _PanelOnlyApp(App):
    """Minimal app that mounts AsyncStackPanel for ``run_test`` pilots."""

    def compose(self) -> ComposeResult:
        yield AsyncStackPanel(id="async_stack")


# ── Unmounted construction tests (= state without Textual context) ─────────


def _panel() -> AsyncStackPanel:
    """Construct an unmounted panel — ``_refresh`` returns early when
    ``_static`` is None so the public API still drives internal state.
    """
    return AsyncStackPanel()


def test_empty_panel_renders_empty_text_and_empty_snapshot() -> None:
    """Tier 2: no entries → empty Text + empty snapshot list."""
    panel = _panel()
    assert panel._build_lines().plain == ""
    assert panel.snapshot() == []


def test_add_creates_running_entry_visible_in_snapshot() -> None:
    """Tier 2: ``add()`` produces a snapshot row + render line."""
    panel = _panel()
    panel.add("alice", "code_review")
    snap = panel.snapshot()
    assert len(snap) == 1
    assert snap[0]["agent_id"] == "alice"
    assert snap[0]["glyph"] == "⟳"
    assert snap[0]["summary"] == "code_review"
    assert snap[0]["pending_count"] == 0
    rendered = panel._build_lines().plain
    assert "alice" in rendered
    assert "code_review" in rendered
    assert "⟳" in rendered


def test_add_is_idempotent_for_same_agent_id() -> None:
    """Tier 2: second ``add()`` with same id updates summary, doesn't duplicate."""
    panel = _panel()
    panel.add("alice", "first")
    panel.add("alice", "second")
    snap = panel.snapshot()
    assert len(snap) == 1
    assert snap[0]["summary"] == "second"


def test_set_pending_switches_glyph_and_carries_count() -> None:
    """Tier 2: ``set_pending()`` flips ⟳ → ⚑ + visible pending count."""
    panel = _panel()
    panel.add("alice", "code_review")
    panel.set_pending("alice", 2)
    snap = panel.snapshot()
    assert snap[0]["glyph"] == "⚑"
    assert snap[0]["pending_count"] == 2
    rendered = panel._build_lines().plain
    assert "⚑" in rendered
    assert "2 pending" in rendered


def test_set_running_reverses_pending_state() -> None:
    """Tier 2: ``set_running()`` flips ⚑ → ⟳ and resets pending count."""
    panel = _panel()
    panel.add("alice", "code_review")
    panel.set_pending("alice", 1)
    panel.set_running("alice", "code_review (resumed)")
    snap = panel.snapshot()
    assert snap[0]["glyph"] == "⟳"
    assert snap[0]["pending_count"] == 0
    assert snap[0]["summary"] == "code_review (resumed)"


def test_remove_drops_entry() -> None:
    """Tier 2: ``remove()`` makes the entry vanish from snapshot + render."""
    panel = _panel()
    panel.add("alice", "code_review")
    panel.add("bob", "monitor")
    panel.remove("alice")
    snap = panel.snapshot()
    assert len(snap) == 1
    assert snap[0]["agent_id"] == "bob"
    rendered = panel._build_lines().plain
    assert "alice" not in rendered
    assert "bob" in rendered


def test_pending_entries_sort_before_running_entries() -> None:
    """Tier 2: ⚑ pending entries surface above ⟳ running entries."""
    panel = _panel()
    panel.add("alice", "code_review")
    panel.add("bob", "monitor")
    panel.set_pending("bob", 1)
    snap = panel.snapshot()
    # bob (pending) on top, alice (running) below.
    assert snap[0]["agent_id"] == "bob"
    assert snap[0]["glyph"] == "⚑"
    assert snap[1]["agent_id"] == "alice"
    assert snap[1]["glyph"] == "⟳"


def test_cap_collapses_tail_to_overflow_indicator() -> None:
    """Tier 2: > ``_CAP`` entries produce a ``… +N more`` overflow row."""
    panel = _panel()
    for i in range(8):  # _CAP=5 + 3 overflow
        panel.add(f"agent-{i}", f"task-{i}")
    snap = panel.snapshot()
    # 5 entries + 1 overflow indicator = 6 rows.
    assert len(snap) == 6
    assert snap[-1]["is_overflow"] is True
    assert "+3 more" in snap[-1]["summary"]
    rendered = panel._build_lines().plain
    assert "+3 more" in rendered


def test_empty_agent_id_degrades_safely() -> None:
    """Tier 2: ``add("")`` is a no-op, ``set_pending``/``remove`` on
    unknown id don't crash.
    """
    panel = _panel()
    panel.add("", "should-not-mount")
    panel.set_pending("never-mounted", 3)
    panel.remove("also-never-mounted")
    assert panel.snapshot() == []


def test_clear_resets_all_entries() -> None:
    """Tier 2: ``clear()`` empties the panel."""
    panel = _panel()
    panel.add("alice", "x")
    panel.add("bob", "y")
    panel.clear()
    assert panel.snapshot() == []
    assert panel._build_lines().plain == ""


# ── Mounted integration test ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_panel_renders_under_app_with_multiple_entries():
    """Tier 2: mounted panel actually renders text content at terminal size."""
    app = _PanelOnlyApp()
    async with app.run_test(headless=True, size=(80, 10)) as pilot:
        await pilot.pause()
        panel = app.query_one("#async_stack", AsyncStackPanel)
        panel.add("alice", "code_review")
        panel.add("bob", "monitor")
        panel.set_pending("alice", 1)
        await pilot.pause()
        rendered = panel._build_lines().plain
        # Both agents appear; pending sorted first.
        assert "alice" in rendered
        assert "bob" in rendered
        first_line = rendered.split("\n", 1)[0]
        assert "alice" in first_line
        assert "⚑" in first_line
