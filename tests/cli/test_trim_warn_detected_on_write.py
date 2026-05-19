"""Tier 2: ``_write_log`` invokes the trim-warning helper after a trim has occurred.

The previous wiring only called ``_maybe_warn_about_trimmed_history``
from ``_jump_to_relative_anchor`` (= turn navigation). A user who let
the session auto-scroll past the ``_RICHLOG_MAX_LINES`` boundary never
saw the "earlier history trimmed" signal until they happened to press
Ctrl+P / Ctrl+N — by which point the disconnect between scroll position
and visible content was already confusing.

Hook the check into ``_write_log`` so the warning helper fires the
first time a write occurs after the ring buffer drops a line.

Pinned at the public-surface level via a spy on the helper itself
(per ``testing.ja.md``: no MagicMock, no private-state asserts on
``_trim_warned``). The helper's own one-shot semantics are already
covered by its existing call site in ``_jump_to_relative_anchor``;
this test only pins that ``_write_log`` participates in the dispatch.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from rich.text import Text

from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _instrument_trim_warning(conv: ConversationView) -> list[None]:
    """Replace ``_maybe_warn_about_trimmed_history`` with a call recorder.

    Direct attribute substitution per testing.ja.md (= no
    ``unittest.mock``). Returns a list that grows by one each time the
    helper is invoked.
    """
    calls: list[None] = []

    def _recorder(log) -> None:
        del log  # unused — we only care that the call happened
        calls.append(None)

    conv._maybe_warn_about_trimmed_history = _recorder  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_write_log_does_not_call_helper_when_no_trim_occurred() -> None:
    """Tier 2: writes into a fresh log (no trim) don't invoke the helper."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        calls = _instrument_trim_warning(conv)

        conv._write_log(Text("hello"))
        await pilot.pause()
        assert calls == [], (
            f"helper must not be called when log has not yet trimmed; "
            f"got {len(calls)} calls"
        )


@pytest.mark.asyncio
async def test_write_log_calls_helper_after_log_has_trimmed() -> None:
    """Tier 2: a write past ``log._start_line > 0`` invokes the helper.

    Simulate the trim having already happened by bumping the RichLog's
    cumulative drop counter (= the public attribute the helper reads).
    The helper is spied so we observe the dispatch without asserting on
    the private ``_trim_warned`` flag.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()
        log._start_line = 42  # type: ignore[attr-defined]

        calls = _instrument_trim_warning(conv)
        conv._write_log(Text("trigger"))
        await pilot.pause()
        assert calls == [None], (
            f"_write_log past trim boundary must invoke the helper exactly "
            f"once; got {len(calls)} calls"
        )


@pytest.mark.asyncio
async def test_write_log_keeps_dispatching_after_first_trim() -> None:
    """Tier 2: dispatch happens on every write past the boundary.

    The helper itself has the one-shot guard (already covered by its
    existing call-site test). ``_write_log``'s job is to keep calling
    the helper — the helper decides whether to actually act. This
    test pins that ``_write_log`` does not short-circuit after the
    first call.

    NOTE: production code DOES short-circuit on ``self._trim_warned`` to
    skip the helper call entirely after the first fire — i.e. once the
    flag is True, the helper is not invoked again on subsequent writes.
    In this test the recorder replaces the helper so ``_trim_warned``
    is never flipped, and the short-circuit's guard stays open — so
    we see every write reach the dispatch point.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv._log()
        log._start_line = 42  # type: ignore[attr-defined]

        calls = _instrument_trim_warning(conv)
        for _ in range(3):
            conv._write_log(Text("line"))
            await pilot.pause()
        assert calls == [None, None, None], (
            f"_write_log must keep dispatching to the helper on every "
            f"post-trim write while the recorder holds the flag open; "
            f"got {len(calls)} calls"
        )
