"""Tier 2: Error severity 3-tier classification and visual threading.

Wave-13 T1-4 / Topic A #3 + #7 + #8.

Pinned checks (per spec):
  1. _classify_error_severity("[budget exceeded] ...", {}) → "high"
  2. _classify_error_severity("usage: /image <path>", {}) → "low"
  3. _classify_error_severity("router failed: ...", {}) → "med"
  4. sticky.show("...", kind="error", terminal=True) → priority 110 > thinking 100
  5. events_tab error filter set includes safety_limit_checkpoint,
     chain_timeout, chain_peer_discarded

No mocks. Public surface only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── 1-3: _classify_error_severity ────────────────────────────────────────────


def test_classify_budget_exceeded_is_high() -> None:
    """Tier 2: [budget exceeded] text → high severity."""
    from reyn.chat.tui.widgets.conversation import _classify_error_severity

    assert _classify_error_severity("[budget exceeded] daily limit hit", {}) == "high"


def test_classify_auth_error_is_high() -> None:
    """Tier 2: [auth error] text → high severity."""
    from reyn.chat.tui.widgets.conversation import _classify_error_severity

    assert _classify_error_severity("[auth error] invalid API key", {}) == "high"


def test_classify_permission_denied_text_is_high() -> None:
    """Tier 2: [permission denied] text → high severity."""
    from reyn.chat.tui.widgets.conversation import _classify_error_severity

    assert _classify_error_severity("[permission denied] path /etc/passwd", {}) == "high"


def test_classify_meta_source_failed_is_high() -> None:
    """Tier 2: meta source ending in _failed → high severity."""
    from reyn.chat.tui.widgets.conversation import _classify_error_severity

    assert _classify_error_severity("something went wrong", {"source": "workflow_failed"}) == "high"


def test_classify_meta_source_aborted_is_high() -> None:
    """Tier 2: meta source ending in _aborted → high severity."""
    from reyn.chat.tui.widgets.conversation import _classify_error_severity

    assert _classify_error_severity("aborted", {"source": "skill_run_aborted"}) == "high"


def test_classify_usage_prefix_is_low() -> None:
    """Tier 2: 'usage: /image <path>' → low severity (user-input mistake)."""
    from reyn.chat.tui.widgets.conversation import _classify_error_severity

    assert _classify_error_severity("usage: /image <path>", {}) == "low"


def test_classify_unknown_command_is_low() -> None:
    """Tier 2: 'unknown command /foo' → low severity."""
    from reyn.chat.tui.widgets.conversation import _classify_error_severity

    assert _classify_error_severity("unknown command /foo", {}) == "low"


def test_classify_generic_error_is_med() -> None:
    """Tier 2: unclassified error → med severity (recoverable default)."""
    from reyn.chat.tui.widgets.conversation import _classify_error_severity

    assert _classify_error_severity("router failed: connection reset", {}) == "med"


def test_classify_empty_message_is_med() -> None:
    """Tier 2: empty message with empty meta → med (safe default)."""
    from reyn.chat.tui.widgets.conversation import _classify_error_severity

    assert _classify_error_severity("", {}) == "med"


# ── 4: sticky terminal priority override ──────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_error_priority_above_thinking() -> None:
    """Tier 2: sticky.show(kind='error', terminal=True) → priority 110 > thinking 100.

    We verify via snapshot() that after a terminal error show, the sticky
    IS active (= not suppressed by thinking) when shown over a thinking sticky.
    """
    from reyn.chat.tui.widgets.sticky_status import (
        _KIND_PRIORITY,
        _TERMINAL_ERROR_PRIORITY,
    )

    # Verify the priority constant is above all registered kind priorities
    # (= above what was formerly "thinking" priority = 100, now replaced by
    # the inline Braille spinner; _KIND_PRIORITY no longer has a "thinking"
    # entry).  The constant must beat every existing sticky kind so a
    # terminal error mid-streaming is never suppressed.
    max_registered = max(_KIND_PRIORITY.values())
    assert _TERMINAL_ERROR_PRIORITY > max_registered, (
        f"Expected _TERMINAL_ERROR_PRIORITY ({_TERMINAL_ERROR_PRIORITY}) "
        f"> max registered kind priority ({max_registered})"
    )


@pytest.mark.asyncio
async def test_terminal_error_show_via_show_status() -> None:
    """Tier 2: conv.show_status(terminal=True) routes through to sticky without error.

    ``"thinking"`` is no longer a sticky kind — the inline Braille spinner
    replaced it.  The terminal kwarg is therefore tested by verifying that
    show_status(kind="error", terminal=True) activates the sticky correctly
    and that a subsequent lower-priority general show is suppressed.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        sticky = conv._sticky()
        assert sticky is not None

        # Fire a terminal error via show_status — must activate the sticky.
        conv.show_status("✗ terminal error", kind="error", terminal=True)
        await pilot.pause()
        snap = sticky.snapshot()
        assert snap["kind"] == "error"
        assert snap["active"] is True
        assert snap["body"] == "✗ terminal error"

        # A lower-priority general show (priority 50 < 80 = current error)
        # must be suppressed — the error banner stays visible.
        conv.show_status("● general notice", kind="general")
        await pilot.pause()
        snap_after = sticky.snapshot()
        assert snap_after["body"] == "✗ terminal error", (
            f"Expected error body to survive low-priority show, got: {snap_after}"
        )


# ── 7: events_tab error filter completeness ───────────────────────────────────


def test_events_tab_error_filter_includes_safety_limit_checkpoint() -> None:
    """Tier 2: events-tab 'error' filter includes safety_limit_checkpoint."""
    from reyn.chat.tui.widgets.right_panel.events_tab import _FILTER_GROUPS

    error_set = dict(_FILTER_GROUPS).get("error") or next(
        (s for label, s in _FILTER_GROUPS if label == "error"), frozenset()
    )
    assert "safety_limit_checkpoint" in error_set


def test_events_tab_error_filter_includes_chain_timeout() -> None:
    """Tier 2: events-tab 'error' filter includes chain_timeout."""
    from reyn.chat.tui.widgets.right_panel.events_tab import _FILTER_GROUPS

    error_set = next(
        (s for label, s in _FILTER_GROUPS if label == "error"), frozenset()
    )
    assert "chain_timeout" in error_set


def test_events_tab_error_filter_includes_chain_peer_discarded() -> None:
    """Tier 2: events-tab 'error' filter includes chain_peer_discarded."""
    from reyn.chat.tui.widgets.right_panel.events_tab import _FILTER_GROUPS

    error_set = next(
        (s for label, s in _FILTER_GROUPS if label == "error"), frozenset()
    )
    assert "chain_peer_discarded" in error_set
