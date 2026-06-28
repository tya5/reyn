"""Tier 2: inline CC-style renderer — kind→marker+text contract + factory wiring.

The inline renderer is the default interactive `reyn chat` surface after the
Textual cutover. Each message renders as a 2-cell marker gutter + a wrapping body
column (the agent body as markdown); these assert the rendered-text contract
(markers present, text preserved, meta prefix applied, gutter reserved on wrap,
markdown parsed) — not exact whitespace, so formatting tweaks don't break them.
"""
from __future__ import annotations

import io

from rich.console import Console

from reyn.interfaces.cli.logger_factory import (
    make_chat_renderer,
    make_inline_renderer,
)
from reyn.interfaces.repl.renderer import (
    ChatRenderer,
    ConsoleChatRenderer,
    InlineChatRenderer,
    format_inline_message,
)
from reyn.runtime.outbox import OutboxMessage


def _plain(kind: str, text: str, meta: dict | None = None, *, width: int = 80) -> str:
    """Render a message to plain text. The renderable is now a gutter grid (not a
    bare Text), so we render it through a Console to assert the marker/body
    contract on the visible output."""
    console = Console(width=width, file=io.StringIO(), color_system=None)
    console.print(format_inline_message(OutboxMessage(kind=kind, text=text, meta=meta or {})))
    return console.file.getvalue()


def _render_ansi(kind: str, text: str, *, width: int = 30) -> str:
    """Render with truecolor ANSI on, to assert styling (e.g. the user-input
    background block emits a ``48;2;`` background SGR)."""
    console = Console(width=width, file=io.StringIO(), force_terminal=True,
                      color_system="truecolor")
    console.print(format_inline_message(OutboxMessage(kind=kind, text=text)))
    return console.file.getvalue()


def test_agent_body_renders_markdown_not_raw_source() -> None:
    """Tier 2: the agent (LLM) body renders as markdown — **bold** becomes styled
    text (the ** source is consumed) and list items become bullets, like CC."""
    out = _plain("agent", "Some **bold** words:\n- one\n- two")
    assert "**" not in out          # markdown parsed, not shown as raw source
    assert "bold" in out
    assert "one" in out and "two" in out
    assert "•" in out               # list rendered as bullets


def test_wrapped_agent_body_hang_indents_clear_of_the_gutter() -> None:
    """Tier 2: a wrapped body continues INDENTED in the body column, never bleeding
    back into the 2-cell marker gutter (the reserved-gutter contract)."""
    out = _plain("agent", "word " * 40, width=40)
    lines = [ln for ln in out.split("\n") if ln.strip()]
    # a wrapped continuation line exists, indented into the body column with no
    # marker → the long body did wrap AND hang-indented clear of the gutter
    assert any(c.startswith("  ") and "⏺" not in c for c in lines)
    # the marker only ever LEADS a line (it never appears inside the body / in the
    # gutter of a continuation line)
    assert all(("⏺" not in c) or c.startswith("⏺") for c in lines)


def test_user_line_carries_a_background_block() -> None:
    """Tier 2: the user's own line gets a background block (CC-style 'you said
    this' design); the plain agent line does not."""
    assert "48;2;" in _render_ansi("user", "my message")      # bg SGR present
    assert "48;2;" not in _render_ansi("agent", "plain reply")  # none on agent


def test_user_echo_leads_with_input_marker_and_keeps_text() -> None:
    """Tier 2: the user's own submitted line is echoed with the > input marker
    and its text — so the message stays visible in the conversation after the
    inline input field clears on submit."""
    out = _plain("user", "what files changed?")
    assert ">" in out
    assert "what files changed?" in out


def test_agent_line_leads_with_dot_marker_and_keeps_text() -> None:
    """Tier 2: an agent message renders with the ⏺ marker and its text."""
    out = _plain("agent", "hello world")
    assert "⏺" in out
    assert "hello world" in out


def test_error_line_uses_cross_marker() -> None:
    """Tier 2: an error renders with the ✗ marker and its text."""
    out = _plain("error", "boom")
    assert "✗" in out
    assert "boom" in out


def test_trace_line_uses_corner_marker() -> None:
    """Tier 2: a trace/detail line renders under the ⎿ marker."""
    out = _plain("trace", "phase started")
    assert "⎿" in out
    assert "phase started" in out


def test_skill_done_leads_with_dot_marker() -> None:
    """Tier 2: a skill_done line leads with the ⏺ marker."""
    out = _plain("skill_done", "skill finished")
    assert "⏺" in out
    assert "skill finished" in out


def test_intervention_keeps_question_text() -> None:
    """Tier 2: an intervention line preserves the question text."""
    out = _plain("intervention", "Which file?")
    assert "Which file?" in out


def test_unknown_kind_renders_text_without_marker() -> None:
    """Tier 2: an unrecognised kind falls back to plain text (no kind marker)."""
    out = _plain("totally_new_kind", "raw payload")
    assert out.strip() == "raw payload"


def test_meta_skill_prefix_is_applied() -> None:
    """Tier 2: skill_name + run_id_short surface as a [skill#abcd] prefix."""
    out = _plain(
        "agent", "done",
        meta={"skill_name": "skill_builder", "run_id_short": "ab12"},
    )
    assert "[skill_builder#ab12]" in out
    assert "done" in out


def test_inline_factory_returns_inline_renderer() -> None:
    """Tier 2: make_inline_renderer() builds the inline (default TTY) renderer."""
    r = make_inline_renderer()
    assert isinstance(r, InlineChatRenderer)
    assert isinstance(r, ChatRenderer)


def test_plain_factory_returns_console_renderer() -> None:
    """Tier 2: make_chat_renderer() still builds the plain --cui renderer."""
    r = make_chat_renderer()
    assert isinstance(r, ConsoleChatRenderer)
    assert isinstance(r, ChatRenderer)
