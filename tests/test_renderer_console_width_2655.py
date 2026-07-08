"""Tier 2: repl renderer Console width tracks the live terminal (issue #2655).

`RichChatRenderer` and `InlineChatRenderer` both render via a Rich `Console`
writing to an in-memory `StringIO` buffer (so ANSI can be relayed to the real
terminal outside prompt_toolkit's patched stdout — see each class's
docstring). A Rich `Console` backed by a `StringIO` can't auto-detect a real
terminal's column count and silently falls back to 80 columns, which wraps/
truncates wide content (tables, diffs, code) regardless of the actual
terminal width.

Both classes' `message()` now re-read `_live_terminal_width()` immediately
before every render (the terminal can resize between turns, so this can't be
a construction-time-only fix). These tests exercise the render path
behaviorally: a short label followed by a long unbreakable token wraps onto a
new line when the live width is narrow, and stays on the SAME line as the
label when the live width is wide — proving the render actually consults the
live-width helper's return value (not a hardcoded/construction-time width),
rather than asserting on the renderer's private Console attribute directly.
"""
from __future__ import annotations

import io

from reyn.interfaces.repl import renderer as renderer_module
from reyn.interfaces.repl.renderer import InlineChatRenderer, RichChatRenderer
from reyn.runtime.outbox import OutboxMessage

# A long unbreakable token (no internal spaces): at a narrow width it can't
# share a line with the "Path:" label; at a wide (>= label+token length)
# width it fits alongside the label on one line.
_TOKEN = "segment_" * 12 + "TOKENTAIL"
_LABEL = "Path:"


def _patch_live_width(monkeypatch, width: int) -> None:
    """Monkeypatch the module's own `_live_terminal_width` free function — not a
    collaborator object — so the render path can be exercised without a real
    running prompt_toolkit `Application` (which `get_app()` otherwise requires)."""
    monkeypatch.setattr(renderer_module, "_live_terminal_width", lambda default=80: width)


def _rendered_output(monkeypatch, renderer, kind: str, text: str) -> str:
    """Capture what message() flushes to sys.__stdout__ (the class's own
    documented write target — see RichChatRenderer/InlineChatRenderer
    docstrings), rather than reading renderer-private buffer state."""
    captured = io.StringIO()
    monkeypatch.setattr(renderer_module.sys, "__stdout__", captured)
    renderer.message(OutboxMessage(kind=kind, text=text))
    return captured.getvalue()


def _label_and_token_share_a_line(out: str) -> bool:
    return any(_LABEL in ln and _TOKEN in ln for ln in out.splitlines())


def test_rich_chat_renderer_wide_live_width_keeps_label_and_token_on_one_line(monkeypatch) -> None:
    """Tier 2: RichChatRenderer.message() applies a wide live terminal width, so
    the label and the long unbreakable token stay on the SAME rendered line —
    proving the Console width came from the live-width helper, not Rich's 80-col
    StringIO fallback (issue #2655)."""
    _patch_live_width(monkeypatch, width=200)
    out = _rendered_output(monkeypatch, RichChatRenderer(), "agent", f"{_LABEL} {_TOKEN}")
    assert _label_and_token_share_a_line(out)


def test_rich_chat_renderer_narrow_live_width_wraps_token_onto_new_line(monkeypatch) -> None:
    """Tier 2: the same render path, given a narrow live width, wraps the token
    onto its own line — confirming the width is actually read per render rather
    than a fixed wide value."""
    _patch_live_width(monkeypatch, width=20)
    out = _rendered_output(monkeypatch, RichChatRenderer(), "agent", f"{_LABEL} {_TOKEN}")
    assert not _label_and_token_share_a_line(out)
    assert _TOKEN in out.replace("\n", "")  # token content still present, just wrapped (no hang-indent here)


def test_inline_chat_renderer_wide_live_width_keeps_label_and_token_on_one_line(monkeypatch) -> None:
    """Tier 2: InlineChatRenderer.message() applies the live width for ordinary
    ("agent") kinds too — the fix widens the earlier presentation-only gate
    (FP-0054 §6) to every render, since plain agent replies can also carry wide
    code/diff content."""
    _patch_live_width(monkeypatch, width=200)
    out = _rendered_output(monkeypatch, InlineChatRenderer(), "agent", f"{_LABEL} {_TOKEN}")
    assert _label_and_token_share_a_line(out)


def test_inline_chat_renderer_narrow_live_width_wraps_token_onto_new_line(monkeypatch) -> None:
    """Tier 2: the same InlineChatRenderer render path, given a narrow live width,
    wraps the token onto its own line — confirming per-render (not
    construction-time-only) width."""
    _patch_live_width(monkeypatch, width=20)
    out = _rendered_output(monkeypatch, InlineChatRenderer(), "agent", f"{_LABEL} {_TOKEN}")
    assert not _label_and_token_share_a_line(out)
    assert "segment" in out  # token content still present, just wrapped onto new line(s)
