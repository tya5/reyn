"""Tier 2: StreamingRow tiered stall escalation.

Wave-11 finding B#1. Before this PR, the streaming row showed
a single dim " …" cue past 5 s of idle — identical whether the
stream paused for 5 s or hung for 5 minutes. Users with a stuck
LLM had no escalating prompt and Ctrl+C was the only escape.

This adds three tiers:

  idle ≥ 60s — red " … (no token in <fmt>, Ctrl+C to cancel)"
  idle ≥ 30s — amber " … (stalled <fmt>)"
  idle  ≥ 5s — dim " …"
  idle  < 5s — live cursor blink

Boundaries match SkillActivityRow's elapsed-color thresholds
(= 30 s amber, 60 s red) so the cross-widget mental model is
consistent: 30 s = "taking a while", 60 s = "probably blocked".

Pinned:
  - ``_fmt_idle`` formats seconds / minutes correctly
  - ``_build_renderable`` returns the right tier for each
    boundary
  - Negative idle (= clock-skew artifact) clamps to 0s
  - Sealed rows skip stall logic entirely (= no cue, no cursor)
  - Append resets ``_last_chunk_at`` (= sanity that stream
    progression clears the tier)
"""
from __future__ import annotations

import sys
from pathlib import Path
from time import monotonic

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_fmt_idle_under_60_returns_seconds() -> None:
    """Tier 2: ``_fmt_idle`` for sub-minute idle returns ``Ns``."""
    from reyn.chat.tui.widgets.streaming_row import _fmt_idle

    assert _fmt_idle(0.0) == "0s"
    assert _fmt_idle(7.4) == "7s"
    assert _fmt_idle(45.0) == "45s"
    assert _fmt_idle(59.999) == "59s"


def test_fmt_idle_minutes_returns_m_form() -> None:
    """Tier 2: ``_fmt_idle`` for ≥ 60s returns ``Nm`` (= minute granularity)."""
    from reyn.chat.tui.widgets.streaming_row import _fmt_idle

    assert _fmt_idle(60.0) == "1m"
    assert _fmt_idle(89.5) == "1m"
    assert _fmt_idle(120.0) == "2m"
    assert _fmt_idle(305.0) == "5m"


def test_fmt_idle_negative_clamps_to_zero() -> None:
    """Tier 2: negative idle (= clock skew) clamps to ``0s``."""
    from reyn.chat.tui.widgets.streaming_row import _fmt_idle

    assert _fmt_idle(-3.0) == "0s"
    assert _fmt_idle(-100.0) == "0s"


@pytest.mark.asyncio
async def test_idle_under_5s_shows_cursor_no_stall_text() -> None:
    """Tier 2: idle under tier 1 → live cursor, no stall cue text."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream(msg_id="r-1", agent_name="a")
        # idle = ~0s, cursor visible.
        row._cursor_visible = True
        text = row._build_renderable().plain
        # No stall cue.
        assert "stalled" not in text
        assert "no token" not in text
        assert "…" not in text  # the dim "…" only fires at tier 1
        # Cursor block present.
        assert "▍" in text


@pytest.mark.asyncio
async def test_tier1_dim_ellipsis_at_5s_to_30s() -> None:
    """Tier 2: 5s ≤ idle < 30s → dim " …" (= ambient pause)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream(msg_id="r-2", agent_name="a")
        # Simulate ~10s idle.
        row._last_chunk_at = monotonic() - 10.0
        text = row._build_renderable().plain
        assert "…" in text
        # No higher-tier text yet.
        assert "stalled" not in text
        assert "no token" not in text


@pytest.mark.asyncio
async def test_tier2_amber_stalled_at_30s_to_60s() -> None:
    """Tier 2: 30s ≤ idle < 60s → amber " … (stalled <fmt>)"."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream(msg_id="r-3", agent_name="a")
        # Simulate ~45s idle.
        row._last_chunk_at = monotonic() - 45.0
        text = row._build_renderable().plain
        assert "stalled 45s" in text
        assert "no token" not in text  # not yet tier 3


@pytest.mark.asyncio
async def test_tier3_red_no_token_at_60s_plus() -> None:
    """Tier 2: idle ≥ 60s → red " … (no token in <fmt>, Ctrl+C to cancel)"."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream(msg_id="r-4", agent_name="a")
        # Simulate ~90s idle = 1m.
        row._last_chunk_at = monotonic() - 90.0
        text = row._build_renderable().plain
        assert "no token in 1m" in text
        assert "Ctrl+C to cancel" in text


@pytest.mark.asyncio
async def test_tier3_long_idle_renders_minutes() -> None:
    """Tier 2: 5-minute idle renders ``5m`` not ``300s``."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream(msg_id="r-5", agent_name="a")
        row._last_chunk_at = monotonic() - 305.0
        text = row._build_renderable().plain
        assert "no token in 5m" in text


@pytest.mark.asyncio
async def test_append_resets_stall_state() -> None:
    """Tier 2: a fresh ``append`` call resets ``_last_chunk_at`` so the
    next render returns to the cursor-blink tier."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream(msg_id="r-6", agent_name="a")
        # Simulate stall.
        row._last_chunk_at = monotonic() - 100.0
        text_stalled = row._build_renderable().plain
        assert "no token" in text_stalled
        # Now a chunk lands.
        row.append("hello")
        text_fresh = row._build_renderable().plain
        # Back to live mode — no stall cue.
        assert "no token" not in text_fresh
        assert "stalled" not in text_fresh
        assert "hello" in text_fresh


@pytest.mark.asyncio
async def test_sealed_row_renders_no_stall_cue() -> None:
    """Tier 2: post-``seal`` row never emits a stall cue.

    seal() sets ``_sealed=True`` and the renderable should skip
    the stall block entirely (no cursor, no "…", no escalating
    text). Otherwise a slow-to-mount Markdown swap could leave a
    "stalled 5m" relic on a completed reply.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream(msg_id="r-7", agent_name="a")
        row.append("done.")
        # Pretend a lot of time passed before seal.
        row._last_chunk_at = monotonic() - 200.0
        row.seal()
        text = row._build_renderable().plain
        # No stall artifacts.
        assert "stalled" not in text
        assert "no token" not in text
        assert "…" not in text
        assert "▍" not in text
