"""Tier 2: AsyncStackPanel middle-elides long agent_id on narrow widths.

Wave-11 finding B#5. Before this PR, the truncation order
shrunk ``summary`` first, keeping the full ``agent_id`` always
visible. For typical UUID-shaped task ids (~36 chars) the
prefix consumed ~50 cells; on a 50-cell narrow pane (panel
open + small terminal) summary went to 0 cells and the entry
became unreadable identity-only.

This PR adds a middle-elide fallback: when summary would be
wiped below ``_MIN_SUMMARY_BUDGET_CELLS``, the agent_id is
middle-elided (= ``abcd…7890``) to free cells for the summary.
Identity is preserved via head+tail preview, which is enough
to disambiguate among ≤ _CAP simultaneous tasks.

Pinned:
  - ``_middle_elide_id`` head+tail+ellipsis output shape
  - Short input round-trips unchanged
  - Sub-3-cell budget degrades to plain head truncation
  - Wide-panel renders preserve the full agent_id
    (= no regression on the common case)
  - Narrow-panel renders elide the id + render the summary
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_middle_elide_id_short_input_unchanged() -> None:
    """Tier 2: ID shorter than budget round-trips unchanged."""
    from reyn.chat.tui.widgets.async_stack_panel import _middle_elide_id

    assert _middle_elide_id("alice", 9) == "alice"
    assert _middle_elide_id("ab", 5) == "ab"


def test_middle_elide_id_long_input_keeps_head_and_tail() -> None:
    """Tier 2: long input collapses to ``<head>…<tail>``."""
    from reyn.chat.tui.widgets.async_stack_panel import _middle_elide_id

    uuid_36 = "abcdef12-3456-7890-abcd-ef1234567890"
    out = _middle_elide_id(uuid_36, 9)
    assert len(out) == 9
    assert out.startswith("abcd")
    assert out.endswith("7890")
    assert "…" in out


def test_middle_elide_id_subtle_budget_degrades_to_trunc() -> None:
    """Tier 2: < 3 cell budget degrades to plain head trim."""
    from reyn.chat.tui.widgets.async_stack_panel import _middle_elide_id

    assert _middle_elide_id("abcdefgh", 2) == "ab"
    assert _middle_elide_id("abcdefgh", 1) == "a"


@pytest.mark.asyncio
async def test_wide_panel_keeps_full_agent_id() -> None:
    """Tier 2: wide panel (= plenty of summary budget) preserves the
    full agent_id (= regression check)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(140, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        uuid_36 = "abcdef12-3456-7890-abcd-ef1234567890"
        conv.add_async_task(uuid_36, "code_review")
        await pilot.pause()
        panel = conv._async_stack()
        snap = panel.snapshot()
        # Snapshot returns the raw agent_id (not the rendered form).
        ids = {entry["agent_id"] for entry in snap if not entry["is_overflow"]}
        assert uuid_36 in ids
        # Rendered text (via cache) preserves the full id at wide width.
        text = panel.rendered_text()
        assert uuid_36 in text


@pytest.mark.asyncio
async def test_narrow_panel_elides_agent_id_to_preserve_summary() -> None:
    """Tier 2: narrow panel middle-elides the id + still renders summary."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    # Narrow terminal — async stack panel is full-width on the bottom
    # strip, so total width matters here.
    async with app.run_test(headless=True, size=(60, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        uuid_36 = "abcdef12-3456-7890-abcd-ef1234567890"
        conv.add_async_task(uuid_36, "this-is-a-distinctive-summary")
        await pilot.pause()
        panel = conv._async_stack()
        text = panel.rendered_text()
        # Full UUID NOT in render — it was elided.
        assert uuid_36 not in text
        # Head + ellipsis + tail signature present.
        assert "abcd" in text
        assert "7890" in text
        assert "…" in text
        # Summary surfaced (at least partially — the distinctive
        # head should land).
        assert "this-is" in text or "distinctive" in text


@pytest.mark.asyncio
async def test_narrow_panel_no_summary_skips_elide() -> None:
    """Tier 2: when entry has no summary, no elide pressure — id stays full.

    Without a summary to make room for, there's nothing to gain by
    eliding the id; the row just renders ``<glyph> async: <id> · <elapsed>``.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(60, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Short id (already fits) — no elide needed.
        conv.add_async_task("short-id", "")  # empty summary
        await pilot.pause()
        panel = conv._async_stack()
        text = panel.rendered_text()
        assert "short-id" in text
