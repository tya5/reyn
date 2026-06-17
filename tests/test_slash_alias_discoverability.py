"""Tier 2: alias discoverability in /help listing and picker rows (B4).

B4 gap: ``SlashCommand.aliases`` was only surfaced in the ``/help <cmd>``
focus panel. The bare ``/help`` listing loop and the picker match-row
renderer both ignored aliases, so users had no way to discover shorthands
(e.g. ``/img`` for ``/image``) without already knowing to ask
``/help image``.

Fixes:
  - bare ``/help`` output: commands with aliases show ``(also: /alias)``
    after their summary.
  - picker row render: commands with aliases show a dim ``·alias`` hint
    appended to the name column.

Public surfaces tested (no private state, no format-pin):
  - ``help_cmd`` output contains the alias text for /image.
  - SlashPicker ``rendered_text()`` contains the alias hint for /image
    when rendered inside a real app (same pattern as existing picker tests).
  - no regression: commands without aliases produce no alias annotation.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── /help listing ─────────────────────────────────────────────────────────────


def test_help_listing_registry_data_carries_alias_for_image() -> None:
    """Tier 2: /image has aliases in the REGISTRY — the listing path precondition."""
    from reyn.interfaces.slash import REGISTRY

    image_cmd = REGISTRY.get("image")
    assert image_cmd is not None, "/image not in REGISTRY"
    assert "img" in image_cmd.aliases, (
        f"/image has aliases={image_cmd.aliases!r}; expected 'img'"
    )


def test_help_listing_output_contains_also_img() -> None:
    """Tier 2: the text output of /help includes '(also: /img)' for /image.

    Drives the bare /help path (no arg) via a minimal fake session that
    captures the outbox message, so we test the actual rendered output
    rather than internal data structures.
    """

    class _FakeSession:
        def __init__(self) -> None:
            self._messages: list[str] = []

        async def _put_outbox(self, msg) -> None:
            self._messages.append(msg.text)

    async def _run() -> str:
        from reyn.interfaces.slash.help import help_cmd
        session = _FakeSession()
        await help_cmd(session, "")  # bare /help, no arg
        return "\n".join(session._messages)

    output = asyncio.run(_run())
    assert "(also: /img)" in output, (
        f"'/help' listing output does not contain '(also: /img)': {output!r}"
    )


def test_help_listing_no_alias_hint_for_no_alias_cmd() -> None:
    """Tier 2: /help line for a command without aliases shows no alias annotation."""

    class _FakeSession:
        def __init__(self) -> None:
            self._messages: list[str] = []

        async def _put_outbox(self, msg) -> None:
            self._messages.append(msg.text)

    async def _run() -> str:
        from reyn.interfaces.slash.help import help_cmd
        session = _FakeSession()
        await help_cmd(session, "")
        return "\n".join(session._messages)

    output = asyncio.run(_run())
    # Find the /help row specifically (no aliases → no "(also:" on that line).
    lines = output.split("\n")
    help_row_lines = [ln for ln in lines if "  /help" in ln and "(also:" in ln]
    assert not help_row_lines, (
        f"/help row should not carry an alias hint: {help_row_lines}"
    )


# ── picker row (needs app context) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_picker_row_contains_alias_hint_for_image() -> None:
    """Tier 2: picker row for /image shows a dim alias hint (·img).

    Uses ``SlashPicker.rendered_text()`` — the public surface from
    ``RenderableCacheMixin`` — inside a real app pilot so the Textual
    widget machinery is properly initialised.
    """
    from reyn.interfaces.slash import REGISTRY
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker

    image_cmd = REGISTRY.get("image")
    assert image_cmd is not None, "/image not in REGISTRY"
    assert image_cmd.aliases, "/image has no aliases — test precondition unmet"

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        picker.set_matches([image_cmd])
        await pilot.pause()
        text = picker.rendered_text()
    # The alias hint must appear in the rendered row.
    assert "img" in text, (
        f"Picker row for /image does not contain alias hint 'img': {text!r}"
    )


@pytest.mark.asyncio
async def test_picker_row_no_alias_hint_for_cmd_without_aliases() -> None:
    """Tier 2: picker row for a no-alias command shows no alias annotation (regression guard)."""
    from reyn.interfaces.slash import REGISTRY
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker

    find_cmd = REGISTRY.get("find")
    assert find_cmd is not None, "/find not in REGISTRY"
    assert not find_cmd.aliases, "/find unexpectedly has aliases"

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        picker.set_matches([find_cmd])
        await pilot.pause()
        text = picker.rendered_text()
    # No alias → no "·" hint in the row.
    assert "·" not in text, (
        f"Picker row for /find (no aliases) contains unexpected '·': {text!r}"
    )
