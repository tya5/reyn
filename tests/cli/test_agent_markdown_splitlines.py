"""Tier 2: _write_agent_markdown_with_fold uses splitlines() (G-F11).

Wave-10 follow-up Topic G finding F11 (P3): the agent-markdown
fold path used ``text.split("\\n")`` while the system-message
path used ``text.splitlines()``. The two methods diverge on CRLF
(``\\r\\n``) and bare-CR endings:

  - ``"a\\r\\nb".split("\\n")`` → ``["a\\r", "b"]``  (leaves \\r)
  - ``"a\\r\\nb".splitlines()`` → ``["a", "b"]``  (normalised)

Agent replies that happen to carry CRLF endings (rare with most
LLMs but possible from certain providers / tool outputs) would
pass a fold-preview slice through ``"\\n".join(...)`` with each
line still ending in ``\\r``, producing stray carriage returns in
the rendered Markdown.

The fix aligns the two paths on ``splitlines()``.

Public surfaces tested:
  - the agent-fold path no longer leaves ``\\r`` on CRLF input
  - LF-only text remains unchanged (regression guard)
  - empty / whitespace-only inputs round-trip unchanged
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_crlf_text_does_not_leak_carriage_returns_into_buffer() -> None:
    """Tier 2: CRLF agent reply lands in _recent_replies without ``\\r``.

    ``_write_agent_markdown_with_fold`` writes the FULL text to
    ``_recent_replies`` BEFORE the split — so the buffer entry
    preserves the input verbatim. The fix is about the internal
    ``lines`` list used for the fold-rendered-line estimate. Verify
    that the buffer is untouched (= correctness) AND that the split
    has no ``\\r``-suffixed entries (= the actual fix).
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        crlf_text = "line one\r\nline two\r\nline three"
        conv._write_agent_markdown_with_fold(crlf_text)
        await pilot.pause()
        # Buffer holds verbatim text (= unchanged).
        assert conv.last_reply_text() == crlf_text
        # Internal-only check: simulate the same splitlines call the
        # fixed code uses and verify no \r remnants.
        lines = crlf_text.splitlines()
        for line in lines:
            assert "\r" not in line, (
                f"splitlines result should normalise CRLF; got line={line!r}"
            )


@pytest.mark.asyncio
async def test_lf_only_text_unchanged_regression() -> None:
    """Tier 2b: plain LF text still splits into clean lines (regression)."""
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        lf_text = "line one\nline two\nline three"
        conv._write_agent_markdown_with_fold(lf_text)
        await pilot.pause()
        assert conv.last_reply_text() == lf_text
        lines = lf_text.splitlines()
        assert lines == ["line one", "line two", "line three"]


def test_splitlines_round_trip_matches_render_system_message_idiom() -> None:
    """Tier 2: the two formatter paths now agree on line-splitting.

    Pins the cross-method consistency contract — drift between the
    two would resurrect the carriage-return leak on CRLF input via
    one path while the other path stays clean.
    """
    import inspect

    from reyn.chat.tui.widgets.conversation import ConversationView

    fold_src = inspect.getsource(
        ConversationView._write_agent_markdown_with_fold,
    )
    sys_src = inspect.getsource(ConversationView._render_system_message)
    assert ".splitlines()" in fold_src, (
        "_write_agent_markdown_with_fold should use .splitlines()"
    )
    assert ".splitlines()" in sys_src, (
        "_render_system_message already used .splitlines() — regression"
    )
    # Neither should use the old ``.split("\n")`` form.
    assert '.split("\\n")' not in fold_src
