"""Tier 2: _plain_first_line strips Markdown markup for inline header display.

A5 fix contract: the ``⏺ HH:MM <first-line>`` inline agent header must show
clean prose even when the reply's first line contains Markdown markup:

  ``**Key finding:** detail`` → ``Key finding: detail``
  ``## Heading``              → ``Heading``
  `` `code` snippet``         → ``code snippet``
  ``*emphasis* text``         → ``emphasis text``

Public surface tested: ``_plain_first_line`` (module-level helper in
conversation.py).  The agent reply path (``render_message(kind='agent', ...)``
+ RichLog render) is exercised by the async integration tests in
``test_conv_inline_header_body.py``; these unit tests pin the helper's
own stripping logic without requiring a mounted app.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.tui.widgets.conversation import _plain_first_line


def test_bold_markup_stripped_from_first_line() -> None:
    """Tier 2: ``**Key finding:** foo bar`` → ``Key finding: foo bar`` (no literal ``*``)."""
    raw = "**Key finding:** foo bar\nMore detail on line two."
    result = _plain_first_line(raw)
    assert "**" not in result, f"bold markers must be removed; got {result!r}"
    assert "Key finding:" in result, f"bold content must be preserved; got {result!r}"
    assert "foo bar" in result, f"trailing prose must be preserved; got {result!r}"


def test_h2_heading_stripped() -> None:
    """Tier 2: ``## Heading`` → ``Heading`` (leading ``#`` and spaces removed)."""
    result = _plain_first_line("## Heading\nBody text.")
    assert "#" not in result, f"heading sigil must be removed; got {result!r}"
    assert "Heading" in result, f"heading text must be preserved; got {result!r}"


def test_inline_code_markers_stripped() -> None:
    """Tier 2: `` `code` `` → ``code`` (backtick pairs removed)."""
    result = _plain_first_line("`code` snippet\nmore text")
    assert "`" not in result, f"backtick markers must be removed; got {result!r}"
    assert "code" in result, f"code content must be preserved; got {result!r}"
    assert "snippet" in result, f"trailing prose must be preserved; got {result!r}"


def test_emphasis_markers_stripped() -> None:
    """Tier 2: ``*emphasis* text`` → ``emphasis text``."""
    result = _plain_first_line("*emphasis* text")
    assert result == "emphasis text", (
        f"single-star emphasis must be stripped; got {result!r}"
    )


def test_only_first_line_processed() -> None:
    """Tier 2: multi-line input — only the first line is extracted and stripped."""
    raw = "**First line**\n**Second line** is ignored"
    result = _plain_first_line(raw)
    assert "Second line" not in result, (
        f"only first line should be processed; got {result!r}"
    )
    assert "First line" in result, f"first line content must appear; got {result!r}"


def test_plain_prose_unchanged() -> None:
    """Tier 2: ordinary prose without markdown sigils is returned unchanged."""
    plain = "This is plain text without any markup."
    result = _plain_first_line(plain)
    assert result == plain, f"plain prose must be returned as-is; got {result!r}"


def test_arithmetic_asterisk_not_mangled() -> None:
    """Tier 2: bare ``*`` in arithmetic context (no pair) is left untouched.

    ``2 * 3 + 1`` must not become ``2  + 1`` — the regex requires a matching
    pair with at least one non-``*`` char between them.
    """
    result = _plain_first_line("value is 2 * 3 + 1")
    assert "2" in result and "3" in result and "1" in result, (
        f"arithmetic operands must survive; got {result!r}"
    )
    # The bare * between integers has no pair, so it's left by _MD_EM.
    # The leading-sigil strip doesn't touch interior chars.
    # Just ensure the result is sensible (not empty).
    assert result.strip(), f"result must be non-empty; got {result!r}"


def test_empty_input_returns_empty_string() -> None:
    """Tier 2: empty input → empty output (no crash)."""
    assert _plain_first_line("") == ""


def test_bold_mid_line_stripped() -> None:
    """Tier 2: ``**x**`` mid-line (no leading sigil) is also de-bolded."""
    result = _plain_first_line("Result: **critical** error")
    assert "**" not in result, f"bold markers must be removed; got {result!r}"
    assert "critical" in result, f"bold content must be preserved; got {result!r}"
    assert "Result:" in result, f"leading prose must be preserved; got {result!r}"
