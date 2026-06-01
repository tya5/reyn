"""Tier 2: structured ``usage`` opt-in extended to remaining key commands.

Follow-on to PR #552 (= added ``usage`` field + opt-in for /find,
/save, /copy, /attach) and PR #554 (= /help <cmd> focus mode
displaying the usage field). This PR completes the opt-in for
the rest of the high-traffic commands so the picker hint mode
and the /help <cmd> focus panel both surface structured usage
rows for them.

Pinned:
  - /cancel, /answer, /plan, /memory, /docs-filter, /skill,
    /agent, /image, /reset, /budget all have non-empty
    ``usage`` set
  - usage strings follow the convention <arg> required,
    [arg] optional, [a|b] choice
  - colon-form usage in old summaries (= "Cancel a running
    skill: /cancel <id-prefix>") was stripped — usage now
    lives in its own field, summary is prose only. Regression
    guard against revert.

Hidden commands (matrix / donut / zen) and no-arg commands
(/list / /tasks / /skills / /cost / /pending / /expand /
/quit / /exit / /cost-inline) intentionally do NOT get a
usage line — they have no args to document. Pinned by spot
checks to make sure no accidental opt-in slipped through.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# Commands that should have ``usage`` set after this PR.
_EXPECTED_USAGE: dict[str, str] = {
    "cancel":      "/cancel <id-prefix> [confirm]",
    "answer":      "/answer <id-prefix> <text>",
    "plan":        "/plan [list|discard <id>|resume <id>]",
    "memory":      "/memory [list|view <name>]",
    "docs-filter": "/docs-filter [<substring>]",
    "skill":       "/skill [list|discard <id>]",
    "agent":       "/agent new <name> | /agent edit role <text>",
    "image":       "/image <path>",
    "reset":       "/reset confirm",
    "budget":      "/budget [reset]",
    "pending":     "/pending [list|discard <id>|claim <id>]",
}


def test_all_key_commands_have_usage_set() -> None:
    """Tier 2: every targeted key command has its expected usage string."""
    from reyn.chat.slash import REGISTRY

    for name, expected_usage in _EXPECTED_USAGE.items():
        cmd = REGISTRY.get(name)
        assert cmd is not None, f"/{name} not in registry"
        assert cmd.usage == expected_usage, (
            f"/{name} usage mismatch: expected {expected_usage!r}, "
            f"got {cmd.usage!r}"
        )


def test_summary_does_not_re_embed_usage_in_parens() -> None:
    """Tier 2: cleaned-up summaries no longer carry the embedded usage paren.

    Pre-#552 summaries crammed usage into ``... (/find <query>)``
    or ``... : /find <query>``. The cleanup moves usage to its
    own field; this pins that the summary doesn't re-introduce
    a parenthetical or colon-form copy of the usage string for
    the cleaned commands (= regression guard against revert).
    """
    from reyn.chat.slash import REGISTRY

    # Each entry: (cmd_name, must_not_appear_in_summary).
    # We only check commands whose old summary explicitly carried
    # the usage in parens or after a colon.
    cleaned: list[tuple[str, list[str]]] = [
        ("cancel",  ["/cancel <id-prefix>", ": /cancel"]),
        ("answer",  ["/answer <id-prefix>", ": /answer"]),
        ("plan",    ["(list / discard / resume)"]),
        ("memory",  ["(list / view <name>)"]),
        ("skill",   ["(list / discard)"]),
        ("agent",   ["(subcommands: new <name>)"]),
        ("budget",  ["/budget reset to clear"]),
    ]
    for name, forbidden in cleaned:
        cmd = REGISTRY.get(name)
        assert cmd is not None, f"/{name} not in registry"
        for needle in forbidden:
            assert needle not in cmd.summary, (
                f"/{name} summary still contains stripped usage fragment "
                f"{needle!r}: {cmd.summary!r}"
            )


def test_no_arg_commands_have_no_usage() -> None:
    """Tier 2: commands with no meaningful args don't get a fake usage row.

    Surfacing ``↳ usage: /list`` for a zero-arg command would be
    pure noise. Pin that these stay 1-line in the picker hint.
    """
    from reyn.chat.slash import REGISTRY

    no_arg_commands = [
        "list", "tasks", "skills", "cost",
        "quit", "exit", "cost-inline",
    ]
    for name in no_arg_commands:
        cmd = REGISTRY.get(name)
        assert cmd is not None, f"/{name} not in registry"
        assert cmd.usage == "", (
            f"/{name} should not have usage; got {cmd.usage!r}"
        )


def test_hidden_commands_have_no_usage() -> None:
    """Tier 2: hidden easter-egg commands don't surface usage either."""
    from reyn.chat.slash import REGISTRY

    for name in ("matrix", "donut", "zen"):
        cmd = REGISTRY.get(name)
        assert cmd is not None
        assert cmd.hidden is True
        assert cmd.usage == ""


def test_help_focus_panel_renders_usage_for_extended_commands() -> None:
    """Tier 2: /help <cmd> focus panel surfaces the new usage rows.

    Reuses the focus-mode helper introduced in PR #554; this test
    pins the integration with the freshly-extended commands.
    """
    from reyn.chat.slash.help import _render_command_focus

    # /plan is a representative subcommand-style command — pin it
    # to keep the test from rusting if the exact usage syntax
    # gets tweaked. We only assert the usage *substring* appears,
    # not the full string, so trivial wording shifts don't break.
    panel = _render_command_focus("plan")
    assert "/plan" in panel
    assert "usage:" in panel
    assert "discard" in panel or "resume" in panel or "list" in panel

    panel = _render_command_focus("cancel")
    assert "/cancel <id-prefix>" in panel
