"""Tier 2: _SlashCompleter's newline guard (multiline input support).

Slash commands are inherently single-line — once the input buffer (now
multiline-capable, see run_inline_input's Buffer) has moved past a bare
command word onto a second line, the completer must go quiet, same intent as
its existing " " in text check, just extended to the "\\n" case. Real
Document + real completer instance, no mocks.
"""
from __future__ import annotations

from prompt_toolkit.document import Document

from reyn.interfaces.inline.app import _SLASH_COMPLETER


class _FakeCompleteEvent:
    completion_requested = True


def _completions_for(text: str) -> list:
    doc = Document(text=text, cursor_position=len(text))
    return list(_SLASH_COMPLETER.get_completions(doc, _FakeCompleteEvent()))


def test_completes_bare_slash_prefix():
    """Tier 2: a bare "/mo" still suggests matching commands (pre-existing
    behavior, unaffected by the newline guard)."""
    completions = _completions_for("/mo")
    assert any(c.display[0][1] == "/model" for c in completions)


def test_stops_after_space_unchanged():
    """Tier 2: the pre-existing " " in text guard still stops suggestions once
    args begin."""
    assert _completions_for("/model standard") == []


def test_stops_after_newline():
    """Tier 2: a newline (Shift+Enter having inserted one) also stops
    suggestions — the new guard this change adds."""
    assert _completions_for("/mo\nsomething") == []


def test_stops_after_bare_trailing_newline():
    """Tier 2: even a lone trailing newline right after the command word
    (nothing typed on the second line yet) stops suggestions."""
    assert _completions_for("/mo\n") == []


def test_non_slash_text_yields_nothing():
    """Tier 2: text not starting with "/" never suggests (pre-existing guard,
    unaffected)."""
    assert _completions_for("hello") == []
