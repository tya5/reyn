"""Tier 2: inline CC-style renderer — kind→marker+text contract + factory wiring.

The inline renderer is the default interactive `reyn chat` surface after the
Textual cutover. These assert the OutboxMessage→line mapping on the public
`.plain` surface (markers present, text preserved, meta prefix applied) — not
exact whitespace, so formatting tweaks don't break the test.
"""
from __future__ import annotations

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


def _plain(kind: str, text: str, meta: dict | None = None) -> str:
    return format_inline_message(
        OutboxMessage(kind=kind, text=text, meta=meta or {})
    ).plain


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
