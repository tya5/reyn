"""Tier 2: SlashPicker hint mode renders a usage line when ``cmd.usage`` is set.

Follow-on to PR #550 (= unknown-command hint). Completes the input-
bar discoverability axis. Before this PR, the picker hint mode
(= what shows once the user types ``/<cmd> ``) was a single dim row:

    /find  Search the conv pane for a substring (/find <query>)

The usage was crammed into the summary string in parens, which:
  1. Truncated on narrow terminals (usage often got cut off)
  2. Mixed prose description with formal arg syntax
  3. Was inconsistent across commands (some used parens, others
     used colon, others didn't surface usage at all)

This PR adds an optional structured ``usage`` field to
:class:`SlashCommand`. When set, the hint mode renders a second
row:

    /find  Search the conv pane for a substring
            ↳ usage: /find <query>

Commands that don't set ``usage`` stay 1-line (backward
compatible — no existing command behaviour changes by default).
Opt-in update for /find, /save, /copy, /attach in this PR;
remaining commands stay 1-line until a future PR extends them.

Public surfaces tested:
  - ``SlashCommand.usage`` defaults to empty string
  - ``@slash(... usage=...)`` decorator stores the value
  - ``_repaint_hint`` renders a second ``↳ usage:`` line when
    ``cmd.usage`` is set, NOT when it's empty
  - ``_repaint_hint`` does NOT render the usage line when
    completions are active (= /attach <partial> with matches;
    completions already carry actionable arg info)
  - /find, /save, /copy, /attach commands have usage configured
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _picker_text(picker) -> str:
    return picker.rendered_text()


def test_slashcommand_usage_field_defaults_to_empty() -> None:
    """Tier 2: ``usage`` defaults to empty so existing commands keep working."""
    from reyn.slash import SlashCommand

    async def _h(s, a):
        return None

    cmd = SlashCommand(name="x", summary="summary", handler=_h)
    assert cmd.usage == ""


def test_slash_decorator_forwards_usage() -> None:
    """Tier 2: ``@slash(usage=...)`` propagates the value into the registered command."""
    from reyn.slash import REGISTRY, slash

    @slash("xtmpcmd_usage", summary="t", usage="/xtmpcmd_usage <arg>")
    async def _h(s, a):
        return None

    cmd = REGISTRY.get("xtmpcmd_usage")
    assert cmd is not None
    assert cmd.usage == "/xtmpcmd_usage <arg>"


@pytest.mark.asyncio
async def test_hint_with_usage_renders_two_lines() -> None:
    """Tier 2: hint mode shows the usage line below the summary."""
    from reyn.slash import SlashCommand
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets.slash_picker import SlashPicker

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _h(s, a):
            return None
        cmd = SlashCommand(
            name="testcmd",
            summary="Do a thing with the buffer",
            handler=_h,
            usage="/testcmd <arg>",
        )
        picker.set_hint(cmd)
        await pilot.pause()
        text = _picker_text(picker)
        # Both lines present.
        assert "/testcmd" in text
        assert "Do a thing with the buffer" in text
        assert "↳ usage:" in text
        assert "/testcmd <arg>" in text
        # Two-line shape: at least one newline between summary and usage.
        assert "\n" in text


@pytest.mark.asyncio
async def test_hint_without_usage_renders_one_line() -> None:
    """Tier 2: legacy command (no usage set) keeps the original 1-line hint."""
    from reyn.slash import SlashCommand
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets.slash_picker import SlashPicker

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _h(s, a):
            return None
        cmd = SlashCommand(
            name="legacy",
            summary="Legacy command with no structured usage",
            handler=_h,
        )
        picker.set_hint(cmd)
        await pilot.pause()
        text = _picker_text(picker)
        assert "Legacy command" in text
        # No usage line.
        assert "↳ usage:" not in text


@pytest.mark.asyncio
async def test_usage_and_completions_render_together() -> None:
    """Tier 2: usage line + completions BOTH render in the picker hint.

    Wave-11 C#5 reversed the original "suppress usage when
    completions present" guard. The commands with both required
    args AND a finite arg list (= /attach, /memory view, /plan
    resume) were exactly the ones that benefit most from showing
    usage; the prior guard hid usage from them. Total row count
    (= 1 summary + 1 usage + ≤ 8 completions + optional "+N more")
    stays within the CSS ``max-height: 11`` budget.
    """
    from reyn.slash import SlashCommand
    from reyn.tui.app import ReynTUIApp
    from reyn.tui.widgets.slash_picker import SlashPicker

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _h(s, a):
            return None
        cmd = SlashCommand(
            name="testcmd2",
            summary="Has both usage and completions",
            handler=_h,
            usage="/testcmd2 <name>",
        )
        picker.set_completions(cmd, ["alpha", "beta"])
        await pilot.pause()
        text = _picker_text(picker)
        # Completions visible.
        assert "alpha" in text
        assert "beta" in text
        # Usage line ALSO visible (= wave-11 C#5 reversal).
        assert "↳ usage:" in text
        assert "/testcmd2 <name>" in text


def test_find_command_has_usage() -> None:
    """Tier 2: /find opts into the structured usage line.

    Usage syntax expanded in the regex/case opt-in PR
    (``[-r|-c|-rc]`` flag block). The substring ``<query>`` stays
    so the contract "usage names the query placeholder" survives.
    """
    from reyn.slash import REGISTRY

    cmd = REGISTRY.get("find")
    assert cmd is not None
    assert "<query>" in cmd.usage
    assert "/find" in cmd.usage


def test_save_command_has_usage() -> None:
    """Tier 2: /save opts into the structured usage line."""
    from reyn.slash import REGISTRY

    cmd = REGISTRY.get("save")
    assert cmd is not None
    assert cmd.usage == "/save [path]"


def test_copy_command_has_usage() -> None:
    """Tier 2: /copy opts into the structured usage line."""
    from reyn.slash import REGISTRY

    cmd = REGISTRY.get("copy")
    assert cmd is not None
    assert cmd.usage == "/copy [N|list]"


def test_attach_command_has_usage() -> None:
    """Tier 2: /attach opts into the structured usage line."""
    from reyn.slash import REGISTRY

    cmd = REGISTRY.get("attach")
    assert cmd is not None
    assert cmd.usage == "/attach <name>"


def test_find_summary_no_longer_carries_redundant_paren_usage() -> None:
    """Tier 2: /find summary stripped of the embedded ``(/find <query>)``.

    After moving usage to its own field, the summary no longer
    embeds the ``/find`` usage syntax in parens. The regex/case
    opt-in PR added ``(substring or regex)`` to the summary
    which is mode disambiguation, NOT a usage hint, so it stays.
    Pin only that the embedded ``(/find ...)`` form doesn't return.
    """
    from reyn.slash import REGISTRY

    cmd = REGISTRY.get("find")
    assert cmd is not None
    # Embedded ``(/find ...)`` paren-form usage must not return.
    assert "(/find" not in cmd.summary
    # Summary still describes the conv pane scope.
    assert "conv pane" in cmd.summary.lower()
