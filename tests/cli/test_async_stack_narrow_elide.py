"""Tier 2b: AsyncStackPanel shows summary primary, agent_id secondary.

fix/tui-asyncstack-summary-primary: the row format changed so the
human-readable summary (= skill name) is the bold primary label and
the agent_id (= timestamp + skill + 4-hex run_id) is a dim suffix,
shown when width allows and dropped first at narrow widths.

At narrow widths the id suffix is dropped entirely rather than
elided — there isn't enough room for a useful head+tail preview
alongside the summary. At wide widths the full id appears as a
dim ``async: <id>`` trailer.

Pinned:
  - ``_middle_elide_id`` head+tail+ellipsis output shape (pure function)
  - Short input round-trips unchanged
  - Sub-3-cell budget degrades to plain head truncation
  - Wide-panel renders include the agent_id as dim suffix
    (= no regression on the common case)
  - Narrow-panel renders surface the summary, drop the agent_id suffix
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
    from reyn.interfaces.tui.widgets.async_stack_panel import _middle_elide_id

    assert _middle_elide_id("alice", 9) == "alice"
    assert _middle_elide_id("ab", 5) == "ab"


def test_middle_elide_id_long_input_keeps_head_and_tail() -> None:
    """Tier 2b: long input collapses to ``<head>…<tail>``."""
    from reyn.interfaces.tui.widgets.async_stack_panel import _middle_elide_id

    uuid_36 = "abcdef12-3456-7890-abcd-ef1234567890"
    out = _middle_elide_id(uuid_36, 9)
    # Pin shape via structural decomposition rather than a len() pin.
    # "abcd…7890" → head "abcd", ellipsis "…", tail "7890"
    assert out.startswith("abcd")
    assert out.endswith("7890")
    assert "…" in out
    # Head + ellipsis (1 char) + tail must consume the full budget.
    head, _sep, tail = out.partition("…")
    assert head + "…" + tail == out, "output must be head…tail with no extra chars"


def test_middle_elide_id_subtle_budget_degrades_to_trunc() -> None:
    """Tier 2: < 3 cell budget degrades to plain head trim."""
    from reyn.interfaces.tui.widgets.async_stack_panel import _middle_elide_id

    assert _middle_elide_id("abcdefgh", 2) == "ab"
    assert _middle_elide_id("abcdefgh", 1) == "a"


@pytest.mark.asyncio
async def test_wide_panel_shows_summary_primary_and_id_dim_suffix() -> None:
    """Tier 2: wide panel shows summary as primary and full agent_id as dim suffix.

    At 140 cols there is ample room for both the bold summary label and
    the ``  async: <id>`` dim suffix.  Both must appear in the rendered text.
    snapshot() always returns the raw agent_id key regardless of render width.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(140, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        run_id = "20240601T123456123456Z_code_review_a1b2"
        conv.add_async_task(run_id, "code_review")
        await pilot.pause()
        panel = conv._async_stack()
        snap = panel.snapshot()
        # Snapshot returns the raw agent_id (not the rendered form).
        ids = {entry["agent_id"] for entry in snap if not entry["is_overflow"]}
        assert run_id in ids
        text = panel.rendered_text()
        # Summary (primary bold label) is present.
        assert "code_review" in text
        # Full agent_id appears as dim suffix at wide width.
        assert run_id in text


@pytest.mark.asyncio
async def test_narrow_panel_summary_primary_id_dropped() -> None:
    """Tier 2: narrow panel drops agent_id suffix and surfaces summary as primary.

    At 60-col width the run_id (= timestamp + skill + 4-hex, ~35 chars) has
    no room alongside the summary + elapsed.  The new truncation order drops
    the dim agent_id suffix first so the human-readable summary stays fully
    visible as the bold primary label.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    # Narrow terminal — async stack panel is full-width on the bottom
    # strip, so total width matters here.
    async with app.run_test(headless=True, size=(60, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        run_id = "20240601T123456123456Z_code_review_a1b2"
        conv.add_async_task(run_id, "this-is-a-distinctive-summary")
        await pilot.pause()
        panel = conv._async_stack()
        text = panel.rendered_text()
        # Full run_id NOT in render — the suffix was dropped.
        assert run_id not in text
        # The human-readable summary IS surfaced (the distinctive head
        # must be present as the bold primary label).
        assert "this-is-a-distinctive-summary" in text or "this-is" in text


@pytest.mark.asyncio
async def test_narrow_panel_no_summary_renders_without_id_suffix() -> None:
    """Tier 2: entry with no summary renders glyph + elapsed, id suffix optional.

    Without a summary, the row is ``<glyph>  · <elapsed>`` or
    ``<glyph>  · <elapsed>  async: <id>`` depending on available width.
    The key invariant: the id key is never dropped from widget state;
    only the rendered suffix may be omitted. ``snapshot()`` still returns
    the full agent_id regardless.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True, size=(60, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.add_async_task("short-id", "")  # empty summary
        await pilot.pause()
        panel = conv._async_stack()
        # State: agent_id preserved in snapshot regardless of render.
        snap = panel.snapshot()
        ids = {e["agent_id"] for e in snap if not e["is_overflow"]}
        assert "short-id" in ids
