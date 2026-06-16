"""Tier 2: picker hint renders ``usage`` line alongside completions.

Wave-11 finding C#5. Before this PR, ``_repaint_hint`` gated
the structured ``cmd.usage`` row on ``not self._completions``.
The very commands that benefit most from usage discoverability
— /attach, /memory view, /plan resume (= those with both
required args AND a finite arg list) — were exactly the ones
that suppressed the usage row.

This PR drops the gate:
  - ``cmd.usage`` always renders when set, regardless of whether
    completions are surfaced
  - Total row count (= 1 summary + 1 usage + ≤ 8 completions +
    optional "+N more" footer) stays within the CSS
    ``max-height: 11`` budget

Pinned:
  - usage line shows when completions are non-empty (= the
    reversed-gate path)
  - usage line still shows when completions are empty (= the
    pre-existing path)
  - usage line absent when ``cmd.usage`` is empty (backward
    compatible)
  - The row count fits within the picker's height cap even with
    max completions (8) + "+N more" footer
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _picker_text(picker) -> str:
    return picker.rendered_text()


@pytest.mark.asyncio
async def test_usage_renders_with_completions_present() -> None:
    """Tier 2: usage line surfaces when completions are also being shown."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import SlashCommand

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _h(s, a):
            return None
        cmd = SlashCommand(
            name="xattach",
            summary="Switch attached agent",
            handler=_h,
            usage="/xattach <name>",
        )
        picker.set_completions(cmd, ["alpha-agent", "beta-agent"])
        await pilot.pause()
        text = _picker_text(picker)
        # Usage line present.
        assert "↳ usage:" in text
        assert "/xattach <name>" in text
        # Completions also present.
        assert "alpha-agent" in text
        assert "beta-agent" in text


@pytest.mark.asyncio
async def test_usage_renders_without_completions() -> None:
    """Tier 2: hint mode (no completions) still shows usage — regression check.

    The reversed-gate change must not break the bare hint-mode
    path. /find with usage="/find <query>" on a bare ``/find ``
    typing surfaces the same usage line either way.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import SlashCommand

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _h(s, a):
            return None
        cmd = SlashCommand(
            name="xfind",
            summary="Search the conv pane for a substring",
            handler=_h,
            usage="/xfind <query>",
        )
        picker.set_hint(cmd)
        await pilot.pause()
        text = _picker_text(picker)
        assert "↳ usage:" in text
        assert "/xfind <query>" in text


@pytest.mark.asyncio
async def test_no_usage_field_omits_line() -> None:
    """Tier 2: command without ``cmd.usage`` keeps a clean 1-line hint."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import SlashCommand

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _h(s, a):
            return None
        cmd = SlashCommand(
            name="xlegacy",
            summary="legacy command with no structured usage",
            handler=_h,
        )
        picker.set_completions(cmd, ["alpha", "beta"])
        await pilot.pause()
        text = _picker_text(picker)
        assert "↳ usage:" not in text


@pytest.mark.asyncio
async def test_max_completions_plus_usage_fits_height_budget() -> None:
    """Tier 2: max completions (= 8) + usage + summary + "+N more" ≤ 11 rows.

    The CSS ``max-height: 11`` caps the picker. Pin that the
    combined render path doesn't blow past it. Counting newlines
    in the rendered text is the simplest proxy for visual rows.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import SlashCommand

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _h(s, a):
            return None
        cmd = SlashCommand(
            name="xcap",
            summary="cap-budget probe",
            handler=_h,
            usage="/xcap <arg>",
        )
        # Push past the cap so "+N more" footer also renders.
        comps = [f"completion-{i}" for i in range(20)]
        picker.set_completions(cmd, comps)
        await pilot.pause()
        text = _picker_text(picker)
        # Row count = newlines + 1 (= the first row has no preceding newline).
        row_count = text.count("\n") + 1
        # CSS budget = 11 (= ``SlashPicker max-height: 11``).
        assert row_count <= 11, (
            f"hint mode overflowed CSS budget: {row_count} rows in:\n{text}"
        )
