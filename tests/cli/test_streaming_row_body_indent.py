"""Tier 2: StreamingRow renders body at the ts-on hanging indent (F-F1 + F-F6).

Wave-9 Topic F findings F1 + F6 (P1): the streaming body Static and
the sealed Markdown swap had ``padding: 0 0`` — both rendered at
col 0 — while the final committed markdown went through
``_write_body`` → ``_indent_body``. The body visibly jumped at seal()
time when ``end_stream`` committed through ``_write_agent_markdown``.

Fix: both the streaming Static and the sealed Markdown carry
``padding: 0 0 0 <_BODY_INDENT_COLS>`` so they sit at the same
hanging indent as the committed body. The horizontal jump is gone.

``_BODY_INDENT_COLS`` in streaming_row.py is the static ts-on value
(= 8); ``ConversationView._write_body`` applies the dynamic indent
(ts-on=8 / ts-off=2) for the RichLog path.

Public surfaces tested:
  - Static widget mounted by ``compose()`` has ts-on (8-cell) left padding
  - Markdown widget mounted by ``_apply_markdown_swap`` has ts-on left padding
  - Local ``_BODY_INDENT_COLS`` matches ``conversation._BODY_INDENT_COLS``
    (= the contract that prevents drift)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_streaming_row_indent_constant_matches_conversation() -> None:
    """Tier 2: local ``BODY_INDENT_COLS`` matches conversation.py source.

    Pins the cross-module contract — both files must agree on the
    ts-on hanging indent or the horizontal jump returns.
    """
    from reyn.chat.tui.widgets import conversation as conv_mod
    from reyn.chat.tui.widgets import streaming_row as stream_mod

    assert stream_mod.BODY_INDENT_COLS == conv_mod.BODY_INDENT_COLS, (
        f"streaming_row.BODY_INDENT_COLS={stream_mod.BODY_INDENT_COLS} "
        f"must match conversation.BODY_INDENT_COLS={conv_mod.BODY_INDENT_COLS}"
    )


@pytest.mark.asyncio
async def test_streaming_static_has_ts_on_left_padding() -> None:
    """Tier 2: the streaming Static is rendered at the ts-on hanging indent."""
    from textual.widgets import Static

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.streaming_row import _BODY_INDENT_COLS as _STREAM_INDENT

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("test-msg-id", "test-agent")
        row.append("hello world streaming body")
        await pilot.pause()

        static = row.query_one(Static)
        # Textual resolves the CSS padding into a 4-tuple (top, right,
        # bottom, left) on the widget's styles.
        padding = static.styles.padding
        assert padding.left == _STREAM_INDENT, (
            f"streaming Static left-padding should be {_STREAM_INDENT}, "
            f"got {padding.left} (full: {padding!r})"
        )


@pytest.mark.asyncio
async def test_sealed_markdown_has_ts_on_left_padding() -> None:
    """Tier 2: the sealed Markdown swap also lands at the ts-on indent.

    Even though ``end_stream`` removes the row shortly after sealing
    in production, the brief flash of the sealed Markdown should
    not introduce a different indent — otherwise fast streams show
    a flicker at the wrong x-position.
    """
    from textual.widgets import Markdown

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.streaming_row import _BODY_INDENT_COLS as _STREAM_INDENT

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("seal-test-id", "test-agent")
        row.append("# header\n\nbody paragraph")
        await pilot.pause()
        row.seal()
        await pilot.pause()

        # After seal, _apply_markdown_swap mounted a Markdown child.
        markdowns = list(row.query(Markdown))
        assert markdowns, "seal should have mounted a Markdown widget"
        md = markdowns[0]
        padding = md.styles.padding
        assert padding.left == _STREAM_INDENT, (
            f"sealed Markdown left-padding should be {_STREAM_INDENT}, "
            f"got {padding.left} (full: {padding!r})"
        )
