"""Tier 2: slash picker filter matches command aliases (B1).

B1 gap: ``_update_picker`` only checked ``c.name.startswith(token)``.
``SlashCommand.aliases`` was ignored, so typing ``/img`` showed the
unknown-command hint instead of surfacing ``/image``.

Fix: extend the filter to match when ANY alias starts with the token.
Dedup by command identity is implicit — ``REGISTRY.all_commands()``
returns one entry per canonical command (aliases are excluded from that
list by design), so a command can only appear once.

Public surfaces tested:
  - typing a known alias prefix (``/img``) → ``/image`` appears in
    picker candidates (assert via ``picker.has_matches`` + rendered text)
  - typing the canonical prefix (``/ima``) still works
  - typing a non-alias, non-name prefix → unknown hint (no regression)
  - no command appears twice when both name and alias match
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


@pytest.mark.asyncio
async def test_alias_prefix_surfaces_canonical_command() -> None:
    """Tier 2: typing ``/img`` (alias of /image) shows /image in the picker.

    /image has ``aliases=("img",)`` in slash/image.py. Before the fix,
    typing ``/img`` triggered the unknown-hint path because the filter
    only checked ``c.name.startswith("img")``. After the fix, the alias
    check surfaces /image as a candidate.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import InputBar
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import REGISTRY

    # Verify the alias actually exists in the registry before asserting on it.
    image_cmd = REGISTRY.get("image")
    assert image_cmd is not None, "/image not found in REGISTRY"
    assert "img" in image_cmd.aliases, (
        f"/image has aliases={image_cmd.aliases!r}, expected 'img'"
    )

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        input_bar = app.query_one("#inputbar", InputBar)
        input_bar.update_slash_commands(REGISTRY.all_commands())
        await pilot.pause()

        input_bar._update_picker("/img")
        await pilot.pause()

        picker = app.query_one("#slash-picker", SlashPicker)
        assert picker.has_matches, (
            "picker has no matches for '/img' — alias filter not working"
        )
        text = _picker_text(picker)
        assert "/image" in text, (
            f"'/image' not in picker text for token 'img': {text!r}"
        )
        # Must NOT fall through to the unknown-hint path.
        assert "unknown" not in text, (
            f"unknown-hint showing for known alias 'img': {text!r}"
        )


@pytest.mark.asyncio
async def test_canonical_name_prefix_still_works() -> None:
    """Tier 2: typing ``/ima`` (canonical name prefix) still surfaces /image.

    Regression guard: the alias extension must not break the existing
    name-prefix path.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import InputBar
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import REGISTRY

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        input_bar = app.query_one("#inputbar", InputBar)
        input_bar.update_slash_commands(REGISTRY.all_commands())
        await pilot.pause()

        input_bar._update_picker("/ima")
        await pilot.pause()

        picker = app.query_one("#slash-picker", SlashPicker)
        assert picker.has_matches, "picker has no matches for '/ima' — name filter broken"
        text = _picker_text(picker)
        assert "/image" in text


@pytest.mark.asyncio
async def test_no_duplicate_when_both_name_and_alias_match() -> None:
    """Tier 2: a command rendered once even if name AND alias both match.

    ``REGISTRY.all_commands()`` returns one entry per canonical command
    (aliases are separate registry entries but NOT returned by
    ``all_commands()`` — they only appear as ``SlashCommand.aliases``
    on the canonical entry). The filter iterates over canonical commands
    only, so a command that matches via name OR alias is always one
    candidate object. Pin that the picker renders ``/ab_cmd_test`` only
    once in its text.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import InputBar
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import SlashCommand

    async def _noop(_session, _args: str) -> None:
        return None

    # Name="ab_cmd_test", alias="ab_cmd_test_alias".
    # Token "ab_cmd" is a prefix of BOTH name and alias.
    synthetic = SlashCommand(name="ab_cmd_test", summary="dedup-test", handler=_noop,
                              aliases=("ab_cmd_test_alias",))

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        input_bar = app.query_one("#inputbar", InputBar)
        # Load only our synthetic command so the test is deterministic.
        input_bar._slash_commands = [synthetic]

        input_bar._update_picker("/ab_cmd")
        await pilot.pause()

        picker = app.query_one("#slash-picker", SlashPicker)
        assert picker.has_matches, "expected at least one match for '/ab_cmd'"
        text = _picker_text(picker)
        # The canonical name "/ab_cmd_test" must appear exactly once — not twice.
        assert text.count("/ab_cmd_test") == 1, (
            f"'/ab_cmd_test' appeared more than once (dedup failed): {text!r}"
        )


@pytest.mark.asyncio
async def test_unknown_prefix_still_shows_unknown_hint() -> None:
    """Tier 2: a token that matches no name and no alias → unknown hint (no regression).

    Alias support must not suppress the unknown-hint path for genuinely
    unknown tokens.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import InputBar
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import REGISTRY

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        input_bar = app.query_one("#inputbar", InputBar)
        input_bar.update_slash_commands(REGISTRY.all_commands())
        await pilot.pause()

        input_bar._update_picker("/zzznomatch")
        await pilot.pause()

        picker = app.query_one("#slash-picker", SlashPicker)
        assert not picker.has_matches
        text = _picker_text(picker)
        assert "unknown" in text
