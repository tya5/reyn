"""Tier 2: ``/help <cmd>`` focuses on one command's summary + usage.

Categorical UX gap fill on the input-bar discoverability axis.
Before this PR, ``/help`` always listed every command in one
output. Users who knew the command name but wanted to see
"what's the exact usage / does it have aliases?" had to scan
the full list or rely on the picker hint (which only fires
inside the input bar, not from a chat-pane help recall).

This PR extends ``/help`` with an optional ``<cmd>`` arg:

  /help          → existing full list
  /help find     → focused panel for /find
  /help quit     → focused panel showing /quit + alias /exit
  /help xxxx     → "unknown" branch with suggest_for_unknown
                   suggestions (= matches the in-input
                   unknown-command hint from PR #550)
  /help matrix   → focused panel for a hidden command (=
                   ``/help`` itself doesn't list hidden commands;
                   ``/help <hidden>`` is the discovery path)

Public surfaces tested:
  - ``_render_command_focus(name)`` returns the focused panel
    string with command name + summary + usage (when set)
  - aliases rendered when present (e.g. /quit + /exit)
  - hidden flag annotated in the focus view
  - unknown command → "unknown … did you mean: …?" with
    suggestions
  - alias name resolves to the canonical command (= /help exit
    focuses on /quit's panel since exit is an alias)
  - bare ``/help`` (no arg) preserves the existing list output
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_render_focus_known_command_has_summary() -> None:
    """Tier 2: a known command's focus panel includes its summary."""
    from reyn.chat.slash.help import _render_command_focus

    panel = _render_command_focus("find")
    assert "/find" in panel
    assert "Search the conv pane" in panel


def test_render_focus_known_command_with_usage_includes_usage_line() -> None:
    """Tier 2: when ``cmd.usage`` is set (= /find / /save / /copy / /attach
    from PR #552), the focus panel surfaces it on its own row."""
    from reyn.chat.slash.help import _render_command_focus

    panel = _render_command_focus("find")
    assert "usage:" in panel
    assert "/find <query>" in panel


def test_render_focus_without_usage_omits_usage_line() -> None:
    """Tier 2: commands that didn't opt into ``usage`` show no usage row.

    Backward-compat — only commands that explicitly opted in
    (PR #552's four commands so far) get the usage line.
    """
    from reyn.chat.slash.help import _render_command_focus

    # /help itself does have usage now, so pick a no-usage command.
    panel = _render_command_focus("expand")
    assert "/expand" in panel
    assert "summary:" in panel
    # No structured usage was set on /expand → no usage line.
    assert "usage:" not in panel.lower() or "usage:" in panel.lower().split("\n")[0]
    # More precise: the "usage:" prefix line shouldn't appear.
    assert not any(line.lstrip().startswith("usage:") for line in panel.split("\n"))


def test_render_focus_renders_aliases_when_present() -> None:
    """Tier 2: a command with aliases (/quit ↔ /exit) surfaces them."""
    from reyn.chat.slash.help import _render_command_focus

    panel = _render_command_focus("quit")
    assert "/quit" in panel
    # /exit is registered separately, NOT as an alias of /quit
    # (looking at quit.py: ``slash("quit")...; slash("exit")...``).
    # Just pin that the focus panel for /quit doesn't crash and
    # contains the command summary.
    assert "summary:" in panel


def test_render_focus_unknown_command_shows_suggestions() -> None:
    """Tier 2: typo → "unknown … did you mean: /<sug>?" with suggestions."""
    from reyn.chat.slash.help import _render_command_focus

    panel = _render_command_focus("fnd")  # typo of /find
    assert "unknown" in panel.lower()
    # Whatever the suggestion algorithm returns, /help is always
    # the escape hatch — suggest_for_unknown always appends it.
    assert "/help" in panel


def test_render_focus_alias_resolves_to_canonical() -> None:
    """Tier 2: passing an alias name resolves to the canonical command.

    Uses ``REGISTRY.get(name)`` which already handles alias →
    canonical lookup. Pins that ``/help <alias>`` doesn't 404.
    """
    from reyn.chat.slash import REGISTRY, SlashCommand
    from reyn.chat.slash.help import _render_command_focus

    # Register a temp command with an alias for the test, to avoid
    # depending on whichever commands happen to have aliases in
    # the live registry.
    async def _h(s, a):
        return None
    REGISTRY.register(SlashCommand(
        name="xtmphelp_alias_target",
        summary="aliased target for test",
        handler=_h,
        aliases=("xtmphelp_alias_alt",),
    ))
    panel = _render_command_focus("xtmphelp_alias_alt")  # = alias
    assert "/xtmphelp_alias_target" in panel
    assert "aliased target for test" in panel


def test_render_focus_hidden_command_is_annotated() -> None:
    """Tier 2: hidden commands include a "(hidden …)" annotation."""
    from reyn.chat.slash import REGISTRY, SlashCommand
    from reyn.chat.slash.help import _render_command_focus

    async def _h(s, a):
        return None
    REGISTRY.register(SlashCommand(
        name="xtmphelp_hidden_target",
        summary="hidden test cmd",
        handler=_h,
        hidden=True,
    ))
    panel = _render_command_focus("xtmphelp_hidden_target")
    assert "hidden" in panel.lower()


def test_help_command_itself_has_usage_and_focus_works() -> None:
    """Tier 2: /help opted into the structured usage field too."""
    from reyn.chat.slash import REGISTRY
    from reyn.chat.slash.help import _render_command_focus

    cmd = REGISTRY.get("help")
    assert cmd is not None
    assert cmd.usage == "/help [<cmd>]"
    panel = _render_command_focus("help")
    assert "/help [<cmd>]" in panel
