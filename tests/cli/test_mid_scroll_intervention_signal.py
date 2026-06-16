"""Tier 2: mount_intervention / write_error preserve a backlog signal when mid-scroll.

When the user has scrolled up to read history (``_user_scrolled=True``)
and an intervention or error arrives, the previous wiring called
``hide_status()`` unconditionally — the live "thinking…" sticky just
vanished and the user had no on-screen signal that anything was
waiting at the bottom.

The fix swaps the bare ``hide_status()`` for a directional sticky
("⚑ intervention below ↓" / "✗ error below ↓") only when mid-scroll.
At the tail, ``hide_status()`` keeps firing because the widget itself
(intervention) or the newly-written log line (error) will be visible.

Contract pinned:

1. mount_intervention at tail → ``hide_status`` fires (= no
   backlog signal, widget itself is visible).
2. mount_intervention mid-scroll → sticky shows "intervention below"
   directional cue.
3. write_error at tail → ``hide_status`` fires.
4. write_error mid-scroll → sticky shows "error below" cue.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _sticky_text(conv: ConversationView) -> str:
    """Return the current sticky body text via the public snapshot."""
    sticky = conv._sticky()
    if sticky is None:
        return ""
    return sticky.snapshot().get("body", "") or ""


@pytest.mark.asyncio
async def test_intervention_mount_at_tail_clears_sticky() -> None:
    """Tier 2: mount_intervention when at-tail → sticky goes empty (no signal)."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.show_status("thinking…", kind="thinking")
        await pilot.pause()
        assert "thinking" in _sticky_text(conv)

        # at-tail = conv starts with no manual scroll, so the mid-scroll
        # branch should not fire. We don't pre-assert on the private
        # ``_user_scrolled`` flag (testing.ja.md "never assert on
        # private state"); the contract is the sticky body text below.
        conv.mount_intervention(
            question="Continue?", choices=None, iv_id="iv-abc",
        )
        await pilot.pause()

        # No directional cue — the widget itself is visible at the tail.
        assert "below" not in _sticky_text(conv)


@pytest.mark.asyncio
async def test_intervention_mount_mid_scroll_shows_directional_cue() -> None:
    """Tier 2: mount_intervention while mid-scroll → sticky shows "intervention below"."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._scroll_ctrl._user_scrolled = True  # simulate user-up scroll

        conv.mount_intervention(
            question="Continue?", choices=None, iv_id="iv-xyz",
        )
        await pilot.pause()

        body = _sticky_text(conv)
        assert "intervention" in body, (
            f"mid-scroll intervention must surface a directional cue; got {body!r}"
        )
        # Down-arrow hints "below" is reachable by scrolling.
        assert "↓" in body or "below" in body, body


@pytest.mark.asyncio
async def test_error_write_at_tail_clears_sticky() -> None:
    """Tier 2: write_error when at-tail → no backlog signal."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.show_status("thinking…", kind="thinking")
        await pilot.pause()

        # at-tail: same rationale as the intervention-at-tail test above —
        # no private-state precondition assert, contract is sticky body.
        conv.write_error(message="boom", details="")
        await pilot.pause()

        body = _sticky_text(conv)
        assert "below" not in body, (
            f"at-tail error write must not leave a directional cue; got {body!r}"
        )


@pytest.mark.asyncio
async def test_error_write_mid_scroll_shows_directional_cue() -> None:
    """Tier 2: write_error while mid-scroll → sticky shows "error below"."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv._scroll_ctrl._user_scrolled = True

        conv.write_error(message="boom", details="")
        await pilot.pause()

        body = _sticky_text(conv)
        assert "error" in body, (
            f"mid-scroll error write must surface a directional cue; got {body!r}"
        )
        assert "↓" in body or "below" in body, body
