"""Tier 2: inline CC-style tool rows + working-indicator contract.

Tool-call OutboxMessages render as ⏺ Tool(args) / ⎿ result / ⎿ ✗ error, and the
working indicator turns on/off with the turn lifecycle. Assertions are on the
public surfaces (`.plain`, `bottom_toolbar()`), not whitespace or private state.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone

from rich.console import Console

from reyn.interfaces.repl.renderer import (
    InlineChatRenderer,
    format_inline_message,
)
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


def _plain(kind: str, text: str, meta: dict) -> str:
    """Render a message to plain text — the renderable is now a gutter grid (not a
    bare Text), so we render it to assert the marker/content contract. Wide width
    avoids wrapping confounding the one-line assertions."""
    console = Console(width=120, file=io.StringIO(), color_system=None)
    console.print(format_inline_message(OutboxMessage(kind=kind, text=text, meta=meta)))
    return console.file.getvalue()


def test_tool_started_shows_invoke_marker_tool_and_args() -> None:
    """Tier 2: tool_call_started → ▸ invocation marker (distinct from the ⏺
    assistant reply) + tool name + arg summary."""
    out = _plain("tool_call_started", "read_file",
                 {"tool": "read_file", "args": {"path": "docs/x.md"}})
    assert "▸" in out
    assert "read_file" in out
    assert "docs/x.md" in out


def test_tool_completed_shows_corner_and_result() -> None:
    """Tier 2: tool_call_completed → ⎿ marker + result summary."""
    out = _plain("tool_call_completed", "read_file",
                 {"tool": "read_file", "result": "42 lines"})
    assert "⎿" in out
    assert "42 lines" in out


def test_tool_failed_shows_cross_and_error() -> None:
    """Tier 2: tool_call_failed → ⎿ ✗ marker + error message."""
    out = _plain("tool_call_failed", "web_fetch",
                 {"tool": "web_fetch", "error_message": "timeout"})
    assert "⎿" in out
    assert "✗" in out
    assert "timeout" in out


def test_long_result_is_truncated_to_one_line() -> None:
    """Tier 2: an overlong / multiline result is collapsed to a one-line summary."""
    out = _plain("tool_call_completed", "web_search",
                 {"tool": "web_search", "result": "x" * 500 + "\nsecond line"})
    assert out.strip().count("\n") == 0  # collapsed to one rendered line
    assert "…" in out                    # and truncated
    assert "second line" not in out      # the tail past the cap is dropped


def test_started_with_no_args_renders_empty_parens() -> None:
    """Tier 2: a tool with no args still renders cleanly (no crash, empty args)."""
    out = _plain("tool_call_started", "list_skills", {"tool": "list_skills"})
    assert "list_skills" in out


def _evt(t: str) -> Event:
    return Event(type=t, timestamp=datetime.now(timezone.utc), data={})


def test_indicator_idle_shows_no_toolbar() -> None:
    """Tier 2: with no turn running, the working indicator is absent."""
    r = InlineChatRenderer()
    assert r.bottom_toolbar() is None


def test_indicator_on_during_turn_off_after() -> None:
    """Tier 2: turn_started shows the indicator; turn_completed hides it."""
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    tb = r.bottom_toolbar()
    assert tb is not None
    assert "Working" in tb.value  # HTML markup carries the label
    r.on_chat_event(_evt("turn_completed"))
    assert r.bottom_toolbar() is None


def test_indicator_cleared_on_cancel() -> None:
    """Tier 2: a cancelled turn also clears the indicator."""
    r = InlineChatRenderer()
    r.on_chat_event(_evt("turn_started"))
    assert r.bottom_toolbar() is not None
    r.on_chat_event(_evt("turn_cancelled"))
    assert r.bottom_toolbar() is None


def test_unrelated_event_does_not_toggle_indicator() -> None:
    """Tier 2: a non-lifecycle event leaves indicator state unchanged."""
    r = InlineChatRenderer()
    r.on_chat_event(_evt("llm_request"))
    assert r.bottom_toolbar() is None
    r.on_chat_event(_evt("turn_started"))
    r.on_chat_event(_evt("llm_response_received"))
    assert r.bottom_toolbar() is not None  # still thinking
