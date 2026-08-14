"""Tier 2: #4762 — tool_call_failed's own ``err`` display line is WORLD-derived
and was NOT covered by #4758's fix.

#4758 neutralized ``summarize_tool_result``'s single return boundary (the
``tool_call_completed`` summary line — a ✓/✗ SHAPE-derived one-liner).
``tool_call_failed`` is a SEPARATE code path in both the REPL renderer
(``renderer.py``'s ``format_inline_message``) and the TUI presenter
(``presenter.py``'s ``_tool_result_line`` / ``_body_and_background``) that
never goes through ``summarize_tool_result`` at all — #4758/#4760 explicitly
scoped it out, tracked as #4762.

Measured (this issue's own required first step, per lead-coder): ``err`` is
built from ``meta.get("error_message")``, which traces to
``dispatch/dispatcher.py``'s ``except Exception as e: message=f"{type(e).
__name__}: {e}"`` — a catch-all around ANY tool-handler invocation (an MCP
call, a sandboxed subprocess, a provider HTTP error). ``str(e)`` on an
arbitrary caught exception can embed WORLD-derived bytes (an MCP server's own
error text, subprocess stderr folded into an exception message, ...), the
same class #4758 fixed for ``tool_call_completed``'s ``stderr`` branch — so
this is a real hole, not a false positive from "looks similar".

Same required witness shape as #4758's own test file
(``tests/interfaces/test_inline_tool_result_summary.py::
test_stderr_with_terminal_control_bytes_is_neutralized`` /
``test_neutralize_preserves_ordinary_text``): one reject-side test per
render site (an ESC/CSI sequence must not reach the rendered text) plus one
accept-side test (ordinary error text must render unchanged) — both REPL and
TUI, per lead-coder's standing "display surface is REPL AND TUI, never one
side" requirement.
"""
from __future__ import annotations

import io

from rich.console import Console

from reyn.interfaces.inline.textual_chat.presenter import (
    _body_and_background,
    _tool_result_line,
)
from reyn.interfaces.repl.renderer import format_inline_message
from reyn.runtime.outbox import OutboxMessage

_ESC_ERR = "boom\x1b[2Jgone"


def _render_to_text(renderable) -> str:
    """Render any Rich renderable to a plain string via a no-color Console —
    mirrors tests/interfaces/test_3318_body_neutralize.py's own helper,
    needed here for renderer.py's ``format_inline_message`` (returns a
    ``Table.grid``, not a bare ``Text`` with a ``.plain`` attribute)."""
    buf = io.StringIO()
    Console(file=buf, color_system=None, width=100).print(renderable)
    return buf.getvalue()


# ── REPL (renderer.py, format_inline_message) ──────────────────────────────


def test_repl_tool_call_failed_strips_control_bytes() -> None:
    """Tier 2: reject-side — an ESC/CSI byte in error_message must not reach
    the rendered REPL line."""
    msg = OutboxMessage(
        kind="tool_call_failed", text="",
        meta={"error_message": _ESC_ERR},
    )
    rendered = _render_to_text(format_inline_message(msg))
    assert "\x1b" not in rendered, "an ESC byte reached the REPL tool_call_failed line"
    assert "boom" in rendered
    assert "gone" in rendered


def test_repl_tool_call_failed_preserves_ordinary_text() -> None:
    """Tier 2: accept-side — ordinary (control-byte-free) error text renders
    unchanged."""
    msg = OutboxMessage(
        kind="tool_call_failed", text="",
        meta={"error_message": "ordinary error text"},
    )
    rendered = _render_to_text(format_inline_message(msg))
    assert "ordinary error text" in rendered


# ── TUI, coalesced/nested row (presenter.py, _tool_result_line) ────────────


def test_tui_coalesced_tool_call_failed_strips_control_bytes() -> None:
    """Tier 2: reject-side — the TUI's nested ⎿ row (a tool_call_started
    entry coalesced with its own tool_call_failed result)."""
    msg = OutboxMessage(
        kind="tool_call_started", text="",
        meta={
            "_result_kind": "tool_call_failed",
            "_result": {"error_message": _ESC_ERR},
        },
    )
    text, _bg = _tool_result_line(msg)
    assert "\x1b" not in text.plain, "an ESC byte reached the TUI coalesced row"
    assert "boom" in text.plain
    assert "gone" in text.plain


def test_tui_coalesced_tool_call_failed_preserves_ordinary_text() -> None:
    """Tier 2: accept-side, same row shape as above."""
    msg = OutboxMessage(
        kind="tool_call_started", text="",
        meta={
            "_result_kind": "tool_call_failed",
            "_result": {"error_message": "ordinary error text"},
        },
    )
    text, _bg = _tool_result_line(msg)
    assert "ordinary error text" in text.plain


# ── TUI, standalone row (presenter.py, _body_and_background) ───────────────


def test_tui_standalone_tool_call_failed_strips_control_bytes() -> None:
    """Tier 2: reject-side — the TUI's own pre-coalesce standalone
    tool_call_failed row (the entry's own kind, not a coalesced result)."""
    msg = OutboxMessage(
        kind="tool_call_failed", text="",
        meta={"error_message": _ESC_ERR},
    )
    body, _bg = _body_and_background(msg)
    assert "\x1b" not in body.plain, "an ESC byte reached the TUI standalone row"
    assert "boom" in body.plain
    assert "gone" in body.plain


def test_tui_standalone_tool_call_failed_preserves_ordinary_text() -> None:
    """Tier 2: accept-side, same row shape as above."""
    msg = OutboxMessage(
        kind="tool_call_failed", text="",
        meta={"error_message": "ordinary error text"},
    )
    body, _bg = _body_and_background(msg)
    assert "ordinary error text" in body.plain
