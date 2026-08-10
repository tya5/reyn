"""Tier 2: chat.render_mode config knob + TTY-guarded render-path resolution (#3273).

The #3273 TUI pivot makes the interactive chat renderer/driver operator-selectable
instead of hardcoding one render mode (charter "no uncustomizable hardcoded
choices"). Two OS invariants are pinned here:

- **config parse**: ``chat.render_mode`` accepts the four declared values
  (``alt-screen`` default / ``inline`` / ``plain`` / ``auto``); an unknown value
  falls back to the ``alt-screen`` default rather than aborting or silently
  selecting a bogus path.
- **resolution + TTY guard**: :func:`resolve_render_mode` maps
  ``(mode, is_tty)`` to one of the three physical paths. A non-TTY session ALWAYS
  resolves to ``plain`` regardless of mode (the interactive Textual drivers need a
  real terminal — this is the guard that keeps CI / piped / sandbox paths off the
  alt-screen driver). On a TTY each mode routes to its own path.
- **renderer selection (#3292)**: :func:`~reyn.interfaces.cli.commands.chat.
  _renderer_is_interactive` + :func:`~reyn.interfaces.cli.logger_factory.
  make_renderer` prove ``chat.render_mode: plain`` on a TTY (no ``--cui``) now
  selects the SAME ``ConsoleChatRenderer`` a real ``--cui`` invocation gets —
  genuine equivalence, not the pre-#3292 hybrid where only the input driver
  (this file's other tests) switched while the interactive renderer stayed
  selected.

All are pure functions over real config dataclasses / real renderer instances —
no mocks.
"""
from __future__ import annotations

import warnings

import pytest

from reyn.config.chat import CHAT_RENDER_MODES, ChatConfig, _build_chat_config
from reyn.interfaces.cli.commands.chat import _renderer_is_interactive
from reyn.interfaces.cli.logger_factory import make_renderer
from reyn.interfaces.repl.client_driver import resolve_render_mode
from reyn.interfaces.repl.renderer import ConsoleChatRenderer, InlineChatRenderer


def test_render_mode_default_is_alt_screen() -> None:
    """Tier 2: the default render mode is alt-screen — the fixed-out-of-the-box
    mode where the #3285/#3286 inline-driver bugs do not occur."""
    assert ChatConfig().render_mode == "alt-screen"
    # Missing/empty chat block → default.
    assert _build_chat_config(None).render_mode == "alt-screen"
    assert _build_chat_config({}).render_mode == "alt-screen"


@pytest.mark.parametrize("mode", CHAT_RENDER_MODES)
def test_render_mode_each_declared_value_parses(mode: str) -> None:
    """Tier 2: each of the four declared values round-trips through the parser."""
    assert _build_chat_config({"render_mode": mode}).render_mode == mode
    # And still parses when a sibling compaction block is present (the two parse
    # independently).
    cfg = _build_chat_config({"render_mode": mode, "compaction": {"body_token_cap": 99}})
    assert cfg.render_mode == mode
    assert cfg.compaction.body_token_cap == 99


def test_render_mode_unknown_value_falls_back_to_default() -> None:
    """Tier 2: an operator typo / unknown value does not select a bogus path or
    abort startup — it warns and falls back to the alt-screen default."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert _build_chat_config({"render_mode": "fullscreen"}).render_mode == "alt-screen"
        assert _build_chat_config({"render_mode": 123}).render_mode == "alt-screen"


@pytest.mark.parametrize("mode", ["alt-screen", "inline", "plain", "auto", "bogus"])
def test_non_tty_always_resolves_plain(mode: str) -> None:
    """Tier 2: the universal TTY guard — no configured mode can enter an
    interactive Textual driver without a real terminal; every mode → plain."""
    assert resolve_render_mode(mode, is_tty=False) == "plain"


def test_tty_resolution_routes_each_mode() -> None:
    """Tier 2: on a TTY, each mode routes to its own physical path — alt-screen
    and auto to full-screen alt-screen, inline to the legacy bounded driver, plain
    to the plain renderer. An unexpected value defaults to alt-screen."""
    assert resolve_render_mode("alt-screen", is_tty=True) == "alt-screen"
    assert resolve_render_mode("auto", is_tty=True) == "alt-screen"
    assert resolve_render_mode("inline", is_tty=True) == "inline"
    assert resolve_render_mode("plain", is_tty=True) == "plain"
    # Belt-and-braces: an unvalidated/unexpected mode on a TTY → alt-screen default.
    assert resolve_render_mode("something-else", is_tty=True) == "alt-screen"


# ── #3292: chat.render_mode: plain is genuine --cui equivalence (renderer too) ──


@pytest.mark.parametrize("render_mode", ["alt-screen", "inline", "auto", "bogus"])
def test_renderer_is_interactive_stays_interactive_for_non_plain_modes(render_mode: str) -> None:
    """Tier 2: negative control — every render_mode OTHER than "plain" leaves the
    interactive-TTY renderer selection untouched (proves the "plain" branch below
    is a targeted carve-out, not a predicate that happens to always return False)."""
    assert _renderer_is_interactive(is_interactive=True, render_mode=render_mode) is True


def test_renderer_is_interactive_plain_forces_non_interactive() -> None:
    """Tier 2: chat.render_mode: plain on an otherwise-interactive TTY (no --cui)
    now forces the SAME renderer selection a real --cui invocation gets — genuine
    equivalence (#3292), not the pre-#3292 hybrid (plain input loop, interactive
    renderer output)."""
    assert _renderer_is_interactive(is_interactive=True, render_mode="plain") is False


def test_renderer_is_interactive_noop_when_already_noninteractive() -> None:
    """Tier 2: render_mode is irrelevant once --cui / a non-TTY already selected
    the non-interactive path — no render_mode value can flip it back on."""
    for render_mode in (*CHAT_RENDER_MODES, "bogus"):
        assert _renderer_is_interactive(is_interactive=False, render_mode=render_mode) is False


def test_make_renderer_plain_yields_console_renderer_not_inline() -> None:
    """Tier 2: the concrete-class witness for #3292 — feeding
    _renderer_is_interactive's plain-mode result into the SAME make_renderer seam
    chat.py's run() calls yields ConsoleChatRenderer (the real --cui renderer),
    never InlineChatRenderer. Negative control below pins the interactive branch
    still yields InlineChatRenderer, so this isn't a seam that always returns
    ConsoleChatRenderer."""
    renderer = make_renderer(
        _renderer_is_interactive(is_interactive=True, render_mode="plain")
    )
    assert isinstance(renderer, ConsoleChatRenderer)
    assert not isinstance(renderer, InlineChatRenderer)


def test_make_renderer_default_interactive_still_yields_inline_renderer() -> None:
    """Tier 2: negative control for the test above — an interactive TTY with the
    default render_mode still gets the Claude Code-style InlineChatRenderer, so
    the #3292 carve-out is specific to "plain", not a general regression."""
    renderer = make_renderer(
        _renderer_is_interactive(is_interactive=True, render_mode="alt-screen")
    )
    assert isinstance(renderer, InlineChatRenderer)
