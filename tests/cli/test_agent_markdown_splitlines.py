"""Tier 2: _write_agent_markdown stores full text verbatim in _recent_replies.

Originally tested CRLF-normalisation in the fold path (now removed). The
core contract that survives the fold removal:

  - ``_write_agent_markdown`` appends the verbatim text (any line endings)
    to ``_recent_replies`` so ``/copy`` returns the original LLM output.
  - LF-only text round-trips unchanged.
  - CRLF text is stored verbatim (not normalised in the buffer — the LLM
    text is preserved as-is; rendering is handled by RichMarkdown).

Public surfaces tested:
  - ``last_reply_text()`` returns the exact input regardless of line endings
  - LF-only text round-trip
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.mark.asyncio
async def test_crlf_text_stored_verbatim_in_recent_replies() -> None:
    """Tier 2: CRLF agent reply lands in _recent_replies verbatim.

    ``_write_agent_markdown`` appends text to ``_recent_replies`` before
    passing it to RichMarkdown for rendering. The buffer should preserve
    the exact input so ``/copy`` returns what the LLM actually sent.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        crlf_text = "line one\r\nline two\r\nline three"
        conv._write_agent_markdown(crlf_text)
        await pilot.pause()
        # Buffer holds verbatim text (= unchanged).
        assert conv.last_reply_text() == crlf_text


@pytest.mark.asyncio
async def test_lf_only_text_unchanged_regression() -> None:
    """Tier 2b: plain LF text round-trips unchanged through _write_agent_markdown."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ConversationView

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        lf_text = "line one\nline two\nline three"
        conv._write_agent_markdown(lf_text)
        await pilot.pause()
        assert conv.last_reply_text() == lf_text
