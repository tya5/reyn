"""Tier 2: SkillActivityRow drill-down expand on click.

Categorical UX gap fill (execution visibility axis). Before this
PR, ``SkillActivityRow`` showed only the LATEST phase — users
who wanted to see "what phases has this skill been through?"
had to either watch the row live or switch to the right panel
agents tab. This adds:

  - Phase history accumulation as ``set_phase`` is called
  - ``toggle_expand`` / ``is_expanded`` public surface
  - Mouse click on the row toggles the drill-down rendering
  - Multi-line render when expanded showing the phase trajectory

Public surfaces tested (= per memory feedback_test_public_surface_
not_private_state, do NOT assert on ``_phase_history`` directly;
drive ``set_phase`` and assert on the Static widget's rendered
text):

  - ``row.is_expanded`` property reflects current state
  - ``toggle_expand`` flips it
  - When expanded, the Static's rendered plain text includes each
    prior phase name (= history line is visible)
  - Click event on the row triggers expand (= equivalent to
    ``toggle_expand`` via the Click handler)
  - ``finish`` preserves the expand state (= drilling down on a
    completed skill still works)
  - Collapsed view does NOT include the history line (= the
    drill-down is gated, not always-on)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _render_text(row) -> str:
    """Return the plain text of the row's currently displayed render.

    Reads the row's public ``rendered_text()`` helper which caches
    the last frame sent to ``Static.update``. This is the same text
    the user sees on-screen — stable across Textual versions
    (= the Static widget itself has no portable accessor for its
    current renderable).
    """
    return row.rendered_text()


@pytest.mark.asyncio
async def test_row_starts_collapsed() -> None:
    """Tier 2: a freshly mounted row is not expanded."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="abcd1234", skill_name="t_skill")
        await pilot.pause()
        assert row.is_expanded is False
        # Collapsed render should NOT include the history-line marker.
        assert "↳ phases:" not in _render_text(row)


@pytest.mark.asyncio
async def test_toggle_expand_shows_history_line() -> None:
    """Tier 2: after ``toggle_expand``, the rendered text includes phase names."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="abcd1234", skill_name="code_review")
        row.set_phase("plan")
        row.set_phase("research")
        row.set_phase("reviewing")
        await pilot.pause()

        row.toggle_expand()
        await pilot.pause()
        assert row.is_expanded is True
        text = _render_text(row)
        # All three phase names should appear in the rendered (expanded) text.
        assert "plan" in text
        assert "research" in text
        assert "reviewing" in text
        # The history-line marker should be present.
        assert "↳ phases:" in text


@pytest.mark.asyncio
async def test_toggle_expand_round_trip_back_to_collapsed() -> None:
    """Tier 2: toggling twice returns to the original collapsed render shape."""
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="aaaa1111", skill_name="s")
        row.set_phase("alpha")
        await pilot.pause()
        baseline = _render_text(row)
        row.toggle_expand()
        await pilot.pause()
        expanded = _render_text(row)
        assert expanded != baseline
        assert "↳ phases:" in expanded
        row.toggle_expand()
        await pilot.pause()
        collapsed_again = _render_text(row)
        assert "↳ phases:" not in collapsed_again
        # The collapsed text after the round-trip should match the
        # baseline shape (= no residual expand artifact). Don't pin
        # absolute equality because the running spinner index advances
        # between paints; compare the non-spinner suffix.
        assert "alpha" in collapsed_again


@pytest.mark.asyncio
async def test_phase_history_records_revisits_with_visit_count() -> None:
    """Tier 2: re-entering a phase at a higher visit count records both.

    Loop-back to the same phase is meaningful execution detail
    ("we went back to research a 2nd time"). Pin that the visit
    count surfaces in the drilled-down render.
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="bbbb2222", skill_name="loop_skill")
        row.set_phase("research", visit=1)
        row.set_phase("plan", visit=1)
        row.set_phase("research", visit=2)  # ← loop-back
        await pilot.pause()
        row.toggle_expand()
        await pilot.pause()
        text = _render_text(row)
        # Both visits should be visible; v2 marker surfaces re-visit.
        assert "research" in text
        assert "v2" in text
        assert "plan" in text


@pytest.mark.asyncio
async def test_repeated_set_phase_same_value_does_not_inflate_history() -> None:
    """Tier 2: noisy duplicate ``set_phase`` calls collapse to one history entry.

    Forwarder noise (= same phase fired multiple times for one
    transition) shouldn't make the drill-down view show "plan →
    plan → plan → research". Pin that consecutive identical calls
    record only once.
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="cccc3333", skill_name="dedup_skill")
        row.set_phase("plan")
        row.set_phase("plan")
        row.set_phase("plan")
        row.set_phase("research")
        await pilot.pause()
        row.toggle_expand()
        await pilot.pause()
        text = _render_text(row)
        # "plan" should appear at least once; we want to ensure that
        # the count of " → " separators implies a 2-entry history
        # (= "plan → research"), not 4 entries.
        history_section = text.split("↳ phases:", 1)[1]
        # 2 entries -> 1 separator " → "; 4 entries would have 3.
        assert history_section.count(" → ") == 1, (
            f"expected 1 arrow separator (= 2 entries), "
            f"got {history_section.count(' → ')} in {history_section!r}"
        )


@pytest.mark.asyncio
async def test_finish_preserves_expand_state() -> None:
    """Tier 2: ``finish()`` on an expanded row keeps the drill-down visible.

    Users who expand mid-run want the history to stay visible after
    completion (= "what did this skill end up doing?"). The expand
    state must outlive the running → finished transition.
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="dddd4444", skill_name="finishing")
        row.set_phase("plan")
        row.set_phase("execute")
        row.toggle_expand()
        row.finish(success=True, reason="2 phases")
        await pilot.pause()
        assert row.is_expanded is True
        text = _render_text(row)
        # Finished-state head still rendered (= ✓ glyph or "2 phases").
        assert "2 phases" in text
        # Plus the drilled-down history still present.
        assert "↳ phases:" in text
        assert "plan" in text
        assert "execute" in text


@pytest.mark.asyncio
async def test_click_event_toggles_expand() -> None:
    """Tier 2: a Click event on the row triggers the expand toggle.

    Mirrors the mouse path (= primary trigger UX). Synthesises a
    Click event and verifies the post-handler state matches a direct
    ``toggle_expand`` call.
    """
    from textual import events as textual_events

    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="eeee5555", skill_name="click_skill")
        row.set_phase("alpha")
        await pilot.pause()
        assert row.is_expanded is False
        # Build a Click event directly. ``stop_propagation`` is called
        # inside ``on_click``; ``stop`` is invoked on the event itself
        # so the dummy event needs to support the stop() call. Textual
        # events.Click has a no-arg ``stop`` we can rely on.
        click = textual_events.Click(
            chain=1,
            widget=row,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=False,
            screen_x=0,
            screen_y=0,
            style=None,
        )
        row.on_click(click)
        await pilot.pause()
        assert row.is_expanded is True


@pytest.mark.asyncio
async def test_expanded_with_no_phase_history_shows_none_yet() -> None:
    """Tier 2: expanding before any ``set_phase`` shows the "none yet" hint.

    Edge case — user clicks the row immediately after mount, before
    any phase transition has been recorded. The drill-down line
    should explain there's nothing to show rather than render empty.
    """
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row(run_id="ffff6666", skill_name="empty")
        await pilot.pause()
        row.toggle_expand()
        await pilot.pause()
        text = _render_text(row)
        assert "↳ phases:" in text
        assert "none yet" in text
