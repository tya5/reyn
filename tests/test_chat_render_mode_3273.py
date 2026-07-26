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

Both are pure functions over real config dataclasses — no mocks.
"""
from __future__ import annotations

import warnings

import pytest

from reyn.config.chat import CHAT_RENDER_MODES, ChatConfig, _build_chat_config
from reyn.interfaces.repl.client_driver import resolve_render_mode


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
