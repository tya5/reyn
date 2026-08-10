"""Tier 2: the inline-CUI gate requires a TTY on BOTH std streams.

The inline CUI renders a live region to stdout, so it must not be selected when
stdout is piped/redirected (`reyn chat | tee`) even if stdin is a TTY — otherwise
the prompt_toolkit Application writes cursor/ANSI escapes into the pipe. This pins
the single predicate that gates both the renderer choice and the log redirect.
"""
from __future__ import annotations

from reyn.interfaces.cli.commands.chat import _inline_interactive


def test_inline_active_only_when_both_streams_are_tty() -> None:
    """Tier 2: interactive iff not --cui and stdin AND stdout are both TTYs."""
    assert _inline_interactive(cui=False, stdin_isatty=True, stdout_isatty=True)


def test_piped_stdout_falls_back_to_plain_even_with_tty_stdin() -> None:
    """Tier 2: `reyn chat | tee` (tty stdin, piped stdout) is NOT inline."""
    assert not _inline_interactive(cui=False, stdin_isatty=True, stdout_isatty=False)


def test_piped_stdin_is_not_inline() -> None:
    """Tier 2: a non-TTY stdin (scripted input) is never the inline CUI."""
    assert not _inline_interactive(cui=False, stdin_isatty=False, stdout_isatty=True)


def test_cui_flag_forces_plain_even_on_a_full_tty() -> None:
    """Tier 2: --cui opts out of the inline CUI regardless of TTY state."""
    assert not _inline_interactive(cui=True, stdin_isatty=True, stdout_isatty=True)
