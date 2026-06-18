"""Tier 2: clear() preserves today's date so same-day Ctrl+L doesn't dup separator (G-F13).

Wave-10 follow-up Topic G finding F13 (P3): ``clear()`` reset
``_last_header_date = ""`` which is the cold-start sentinel
meaning "no date written yet". On the next message after a
same-day Ctrl+L, ``_maybe_write_header`` saw the date sentinel,
treated the day as un-marked, and emitted another
``── YYYY-MM-DD ──`` separator. The separator is supposed to
mark *day boundaries* — emitting it after every Ctrl+L on the
same day made it appear multiple times per day purely from
session-clear actions.

After the fix ``clear()`` sets ``_last_header_date`` to today's
date so a same-day clear suppresses the redundant separator;
day-crossing clears (= rare midnight case) still emit correctly
because the next message's ``today`` differs.

Public surfaces tested:
  - clear() sets ``_last_header_date`` to today's YYYY-MM-DD
    string (= same format ``_maybe_write_header`` compares
    against, so the next-message branch is a no-op for same-day)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_clear_sets_last_header_date_to_today() -> None:
    """Tier 2: post-clear ``_last_header_date`` equals today's YYYY-MM-DD.

    Same-format invariant means the ``_maybe_write_header`` guard
    ``if today != self._last_header_date`` evaluates False on the
    next same-day message, suppressing the redundant separator.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        conv.clear()
        await pilot.pause()

        today = time.strftime("%Y-%m-%d")
        assert conv.last_header_date == today, (
            f"clear() should set last_header_date to today's date "
            f"({today!r}); got {conv.last_header_date!r}"
        )


@pytest.mark.asyncio
async def test_same_day_clear_does_not_emit_duplicate_date_separator() -> None:
    """Tier 2: same-day Ctrl+L → next message has no extra separator.

    End-to-end check: render a user message → date separator emits.
    clear(). Render another user message → no new date separator
    (= the same day was already marked).
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView
    from reyn.runtime.outbox import OutboxMessage

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        today = time.strftime("%Y-%m-%d")

        # First message → separator should appear.
        conv.render_user_message("first message")
        conv._render_agent_markdown(OutboxMessage(kind="agent", text="reply"))
        await pilot.pause()
        log_text = "\n".join(
            getattr(s, "text", "") for s in getattr(conv._log(), "lines", [])
        )
        assert today in log_text, (
            f"first message should emit today's date separator; got:\n{log_text!r}"
        )
        first_sep_count = log_text.count(today)

        conv.clear()
        await pilot.pause()

        # Second message after clear → no NEW separator (= the same
        # date was already marked).
        conv.render_user_message("post-clear message")
        await pilot.pause()
        log_text_after = "\n".join(
            getattr(s, "text", "") for s in getattr(conv._log(), "lines", [])
        )
        # Pre-fix: today appears ONCE more after clear (= dup separator).
        # Post-fix: today appears 0 times (= log was cleared, no new
        # separator emitted on the post-clear message).
        assert today not in log_text_after, (
            f"post-clear same-day message should not emit a new date "
            f"separator; got:\n{log_text_after!r}"
        )
        del first_sep_count  # quiet linter
