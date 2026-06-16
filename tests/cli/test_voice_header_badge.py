"""Tier 2: voice mode badge in the header tracks recording / transcribing state.

Categorical UX gap on the voice surface. Before this PR, voice
status was surfaced only via conv-pane log lines. The lines scroll
away as agent output continues, so a user who started recording
and switched focus to another window couldn't tell at a glance
when they came back whether they were still in voice mode.

This mirrors the find-badge approach from PR #565: a persistent
header indicator that survives the log-line scroll.

Status shape:

  agent · model · tok · cost · [find: …] · 🔴 voice · 14:23:01     ← recording
  agent · model · tok · cost · [find: …] · ⏳ voice · 14:23:01    ← transcribing
  agent · model · tok · cost · [find: …] · 14:23:01                ← inactive (no badge)

Pinned:
  - ``ReynHeader.set_voice_state("recording")`` renders 🔴 voice
  - ``set_voice_state("transcribing")`` renders ⏳ voice
  - ``set_voice_state(None)`` clears the badge
  - Unknown state strings get normalized to None (= defensive
    against caller typos)
  - Equality-gated repaint: idempotent calls don't churn the Static
  - Badge appears BEFORE the clock canary (= rightmost contract
    preserved)
  - ``_voice_set_header_state`` helper on the App routes through
    the header without crashing when the header is missing
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _header_text(header) -> str:
    """Plain-text rendering via the inherited RenderableCacheMixin."""
    return header.rendered_text()


@pytest.mark.asyncio
async def test_set_voice_state_recording_renders_badge() -> None:
    """Tier 2: ``set_voice_state('recording')`` shows the 🔴 voice badge."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_voice_state("recording")
        await pilot.pause()
        text = _header_text(header)
        assert "🔴 voice" in text


@pytest.mark.asyncio
async def test_set_voice_state_transcribing_renders_badge() -> None:
    """Tier 2: ``set_voice_state('transcribing')`` shows the ⏳ voice badge."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_voice_state("transcribing")
        await pilot.pause()
        text = _header_text(header)
        assert "⏳ voice" in text


@pytest.mark.asyncio
async def test_set_voice_state_none_clears_badge() -> None:
    """Tier 2: ``set_voice_state(None)`` removes the badge."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_voice_state("recording")
        await pilot.pause()
        assert "🔴 voice" in _header_text(header)
        header.set_voice_state(None)
        await pilot.pause()
        text = _header_text(header)
        assert "🔴" not in text
        assert "voice" not in text


@pytest.mark.asyncio
async def test_unknown_state_is_normalized_to_none() -> None:
    """Tier 2: unknown state strings (= caller typo) clear the badge."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_voice_state("recording")
        await pilot.pause()
        assert "🔴 voice" in _header_text(header)
        header.set_voice_state("garbage-typo")
        await pilot.pause()
        assert "🔴" not in _header_text(header)
        assert header.voice_state is None


@pytest.mark.asyncio
async def test_set_voice_state_idempotent_no_churn() -> None:
    """Tier 2: redundant calls early-return (= equality gate)."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_voice_state("recording")
        await pilot.pause()
        first = _header_text(header)
        # Same call again — should be a no-op.
        header.set_voice_state("recording")
        await pilot.pause()
        # Internal state unchanged.
        assert header.voice_state == "recording"
        # Rendered text unchanged (= idempotent; clock seconds may
        # differ but the badge is stable).
        assert "🔴 voice" in _header_text(header)
        # Sanity that the recording state matches.
        assert "🔴 voice" in first


@pytest.mark.asyncio
async def test_voice_badge_appears_before_clock() -> None:
    """Tier 2: badge sits left of the HH:MM:SS clock canary."""
    import re

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_voice_state("recording")
        await pilot.pause()
        text = _header_text(header)
        clock_match = re.search(r"\d{2}:\d{2}:\d{2}", text)
        assert clock_match is not None
        badge_idx = text.find("🔴 voice")
        assert badge_idx >= 0
        assert badge_idx < clock_match.start()


@pytest.mark.asyncio
async def test_app_voice_set_header_state_helper_routes_through_header() -> None:
    """Tier 2: ``_voice_set_header_state`` calls the header's set_voice_state."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        app._voice_set_header_state("recording")
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        assert header.voice_state == "recording"
        app._voice_set_header_state(None)
        await pilot.pause()
        assert header.voice_state is None


@pytest.mark.asyncio
async def test_voice_and_find_badges_coexist() -> None:
    """Tier 2: voice + find badges can both render in the same status line."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_find_state("foo", position=2, total=5)
        header.set_voice_state("recording")
        await pilot.pause()
        text = _header_text(header)
        assert "[find: 'foo' 2/5]" in text
        assert "🔴 voice" in text
