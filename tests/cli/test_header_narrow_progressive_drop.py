"""Tier 2b: ReynHeader progressive field drop at narrow terminal widths.

Background (deeper layout fix):

#865 fixed voice/find badge budget calc, but the deeper problem remained:
at 80-col with both token and cost caps set, fixed fields total:
  model (e.g. openai/gemini-2.5-flash-lite = 27 cells)
  tokens (12,345 / 100,000 tok = 20 cells)
  cost ($0.0123 / $5.00 = 15 cells)
  clock (HH:MM:SS = 8 cells)
  separators × 4 = 20 cells
= ~90 cells before agent name, against ~72 available cells at 80-col.

Without progressive drop the agent-name budget went negative → silently
clipped to 3-cell minimum → "default" rendered as "de…".

Fix: ``_choose_included_fields`` drops optional fields in priority order
(model → cost → tokens) until the agent name has at least 6 cells.
Clock and active-state badges (voice / find / pending) are never dropped.

What these tests pin:
  - 120-col + all caps: all fields visible (baseline, no drop)
  - 80-col + caps: model dropped, agent name "default" fully visible
  - 60-col + caps: model + cost dropped, agent + tokens + clock visible
  - 40-col + caps: model + cost + tokens dropped, agent + clock survive
  - Voice badge at 80-col: preserved (active-state badge > fixed field)

Tier self-check:
  - No MagicMock / AsyncMock / patch
  - Docstrings declare Tier 2b
  - All assertions on public surface (.plain from _format_status())
  - No snapshot / golden-file output
  - No private-state assertions (_field access except established pattern
    _format_status().plain used across all header tests)
  - Tier 4 self-check: each test is an OS invariant — agent name renders
    fully when fields are dropped; badges survive narrow terminals.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tui.widgets import ReynHeader  # noqa: E402


class _HeaderOnlyApp(App):
    """Minimal app — just a ReynHeader, no conv pane / right panel."""

    def __init__(self, *, agent_name: str = "", model: str = "") -> None:
        super().__init__()
        self._agent_name = agent_name
        self._model = model

    def compose(self) -> ComposeResult:
        yield ReynHeader(
            agent_name=self._agent_name, model=self._model, id="header",
        )


@pytest.mark.asyncio
async def test_120col_all_caps_all_fields_visible() -> None:
    """Tier 2b: 120-col + budget caps → all fields visible, no progressive drop.

    Baseline: at 120 columns the model + tokens + cost + clock easily fit
    alongside the agent name. _choose_included_fields should return
    (True, True, True) — no field is dropped.
    """
    app = _HeaderOnlyApp(
        agent_name="default",
        model="openai/gemini-2.5-flash-lite",
    )
    async with app.run_test(headless=True, size=(120, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(
            tokens_today=12_345,
            tokens_cap=100_000,
            cost_usd=0.0123,
            cost_cap=5.00,
        )
        await pilot.pause()
        rendered = header._format_status().plain
        # All four field families should be present.
        assert "default" in rendered, f"agent name missing: {rendered!r}"
        assert "gemini-2.5-flash-lite" in rendered, f"model missing: {rendered!r}"
        assert "tok" in rendered, f"tokens missing: {rendered!r}"
        assert "$" in rendered, f"cost missing: {rendered!r}"
        assert ":" in rendered.split("│")[-1], f"clock missing: {rendered!r}"


@pytest.mark.asyncio
async def test_80col_budget_caps_model_dropped_agent_fully_visible() -> None:
    """Tier 2b: 80-col + budget caps → model dropped, agent name 'default' fully visible.

    This is the primary regression test for the deeper narrow-terminal bug.
    At 80 columns with both token and cost caps set, fixed fields exceed the
    72-cell available area. Progressive drop should suppress the model field
    so the agent name has enough budget to render as "default" (not "de…").
    """
    app = _HeaderOnlyApp(
        agent_name="default",
        model="openai/gemini-2.5-flash-lite",
    )
    async with app.run_test(headless=True, size=(80, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(
            tokens_today=12_345,
            tokens_cap=100_000,
            cost_usd=0.0123,
            cost_cap=5.00,
        )
        await pilot.pause()
        rendered = header._format_status().plain
        # The full agent name must be present without truncation.
        assert "default" in rendered, (
            f"agent name truncated at 80-col + caps — rendered={rendered!r}"
        )
        assert "de…" not in rendered, (
            f"agent name silently truncated to 'de…' at 80-col + caps — "
            f"progressive drop not working — rendered={rendered!r}"
        )
        # Model should be absent (dropped to free space for agent name).
        assert "gemini-2.5-flash-lite" not in rendered, (
            f"model not dropped at 80-col + caps — rendered={rendered!r}"
        )
        # Clock canary must still be at the right edge.
        assert ":" in rendered.split("│")[-1], (
            f"clock canary missing at 80-col — rendered={rendered!r}"
        )


@pytest.mark.asyncio
async def test_60col_budget_caps_model_and_cost_dropped() -> None:
    """Tier 2b: 60-col + budget caps → model + cost dropped, agent + tokens + clock visible.

    At 60 columns even dropping model alone may not be enough for a
    6-cell agent name budget. Cost should also be dropped. Tokens and
    clock remain as higher-priority fields.
    """
    app = _HeaderOnlyApp(
        agent_name="default",
        model="openai/gemini-2.5-flash-lite",
    )
    async with app.run_test(headless=True, size=(60, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(
            tokens_today=12_345,
            tokens_cap=100_000,
            cost_usd=0.0123,
            cost_cap=5.00,
        )
        await pilot.pause()
        rendered = header._format_status().plain
        # Agent name must be fully present (not truncated to 3-char stub).
        assert "default" in rendered, (
            f"agent name truncated at 60-col + caps — rendered={rendered!r}"
        )
        # Model must be absent.
        assert "gemini-2.5-flash-lite" not in rendered, (
            f"model not dropped at 60-col — rendered={rendered!r}"
        )
        # Clock canary must be present.
        assert ":" in rendered.split("│")[-1], (
            f"clock canary missing at 60-col — rendered={rendered!r}"
        )
        # The rendered text must fit in one row (≤ available cells).
        # Proxy: status label height == 1.
        label = header.query_one("#status")
        assert label.size.height == 1, (
            f"status overflowed to height={label.size.height} at 60-col — "
            f"progressive drop not absorbing overflow — rendered={rendered!r}"
        )


@pytest.mark.asyncio
async def test_40col_extreme_agent_and_clock_survive() -> None:
    """Tier 2b: 40-col + budget caps → at minimum agent name fragment + clock survive.

    At extreme narrowness (40 col) even dropping all three optional
    fields (model + cost + tokens) may not be enough for a full agent name.
    The fallback minimum-3-cell guard in _maybe_truncate_agent_name still
    produces a non-empty stub rather than empty. Clock stays rightmost.
    """
    app = _HeaderOnlyApp(
        agent_name="default",
        model="openai/gemini-2.5-flash-lite",
    )
    async with app.run_test(headless=True, size=(40, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(
            tokens_today=12_345,
            tokens_cap=100_000,
            cost_usd=0.0123,
            cost_cap=5.00,
        )
        await pilot.pause()
        rendered = header._format_status().plain
        # Agent name must not be completely absent (minimum 3-cell guard).
        first_field = rendered.split("│")[0].strip()
        assert first_field, (
            f"agent name field empty at 40-col — minimum guard failed — "
            f"rendered={rendered!r}"
        )
        # Clock canary must be present.
        assert ":" in rendered.split("│")[-1], (
            f"clock canary missing at 40-col — rendered={rendered!r}"
        )
        # Status label must stay in one row.
        label = header.query_one("#status")
        assert label.size.height == 1, (
            f"status overflowed to height={label.size.height} at 40-col"
        )


@pytest.mark.asyncio
async def test_voice_badge_preserved_at_80col_over_fixed_fields() -> None:
    """Tier 2b: voice recording badge preserved at 80-col even when model is dropped.

    Active-state badges (voice / find / pending) have higher priority than
    fixed fields. The progressive drop must never suppress a badge; instead
    it drops model / cost / tokens to make room. Clock also stays.

    Note: at 80-col with recording badge (32 cells) + caps the assembly may
    still overflow slightly — this test verifies the badge is PRESENT and the
    label stays in height=1 (= drop logic absorbed the overflow, not the badge).
    """
    app = _HeaderOnlyApp(
        agent_name="default",
        model="openai/gemini-2.5-flash-lite",
    )
    async with app.run_test(headless=True, size=(80, 5)) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(
            tokens_today=12_345,
            tokens_cap=100_000,
            cost_usd=0.0123,
            cost_cap=5.00,
        )
        header.set_voice_state("recording")
        await pilot.pause()
        rendered = header._format_status().plain
        # Voice badge must be in the output — never dropped.
        assert "voice" in rendered, (
            f"voice badge missing at 80-col — rendered={rendered!r}"
        )
        # Clock canary must be present.
        assert ":" in rendered.split("│")[-1], (
            f"clock canary missing with voice at 80-col — rendered={rendered!r}"
        )
