"""Tier 1: #6184 cleanup — the ``meta.get("tool", msg.text)`` fallback
(``presenter.py``'s ``_tool_head``/``_collapsed_retrieval_line``,
``renderer.py``'s ``format_inline_message``) no longer reads ``msg.text``
as a bold tool-name substitute.

Not reachable in production today — both real producers
(``lifecycle_forwarder._enqueue_tool_call``, ``restore.
project_restored_frames``) always set ``meta["tool"]`` — and no test
depended on the old fallback branch either (census,
#6184#issuecomment-5684341577). Fixed anyway (lead-coder GO,
issuecomment-5684647871): NOT because the branch became reachable, but
because ``text`` (#6184 段2b-2) now carries the COMPOSED ``tool(args)``
wire form rather than a bare tool name — a future accidental
``meta["tool"]``-dropping edit would otherwise silently put that
composed string into the bold tool-name slot instead of degrading
honestly. "does not lie if reached" is the property under test, not
"is reached" (it still is not, and this file does not claim otherwise).

Real ``OutboxMessage``/the real render functions throughout (CLAUDE.md
mock ban).
"""
from __future__ import annotations

import io

from rich.console import Console

from reyn.interfaces.inline.textual_chat._meta_keys import RESULT_KIND_KEY
from reyn.interfaces.inline.textual_chat.presenter import (
    _collapsed_retrieval_line,
    _tool_head,
)
from reyn.interfaces.repl.renderer import format_inline_message
from reyn.runtime.outbox import OutboxMessage


def _plain(renderable) -> str:
    """Render any Rich renderable (Text, gutter grid/Table, ...) to plain
    text — mirrors ``test_inline_pr2_tool_rows.py``'s own ``_plain``
    helper (that file's docstring: the renderable is a gutter grid, not
    a bare Text, so it must be rendered through a real Console to assert
    on its content)."""
    console = Console(width=120, file=io.StringIO(), color_system=None)
    console.print(renderable)
    return console.file.getvalue()


def test_tool_head_degrades_to_empty_not_the_composed_wire_text():
    """Tier 1: presenter.py's _tool_head — meta["tool"] absent, text
    carries the #6184 段2b-2 composed wire form (NOT a bare tool name,
    the shape that made the old fallback dangerous) — the bold slot
    must be empty, never that composed string."""
    msg = OutboxMessage(
        kind="tool_call_started", text="read_file(path=/x, n=5)", meta={},
    )
    head = _tool_head(msg)
    assert "read_file(path=/x, n=5)" not in head.plain
    assert "read_file" not in head.plain


def test_format_inline_message_degrades_to_empty_not_the_composed_wire_text():
    """Tier 1: renderer.py's format_inline_message — same witness, the
    REPL's own render path. Rendered through a real Console (the
    returned renderable is a gutter grid, not a bare Text — see
    ``_plain``'s own docstring) so the assertion checks actual rendered
    output, not a non-representative ``str()`` of the renderable
    object."""
    msg = OutboxMessage(
        kind="tool_call_started", text="read_file(path=/x, n=5)", meta={},
    )
    rendered = format_inline_message(msg)
    plain = _plain(rendered)
    assert "read_file(path=/x, n=5)" not in plain
    assert "read_file" not in plain


def test_collapsed_retrieval_line_degrades_cleanly_with_no_tool_name():
    """Tier 1: presenter.py's _collapsed_retrieval_line — meta["tool"]
    absent degrades to "" before _is_retrieval_tool is even consulted,
    never raises, never shows the composed wire text either. An empty
    tool name is never a retrieval tool, so this returns None (falls
    through to the ordinary two-line form) — the same degrade this
    function already had for a genuinely unknown tool name, not a new
    failure mode."""
    msg = OutboxMessage(
        kind="tool_call_started", text="some(composed, wire, text)",
        meta={RESULT_KIND_KEY: "tool_call_completed"},
    )
    result = _collapsed_retrieval_line(msg)
    assert result is None or "some(composed, wire, text)" not in result.plain
