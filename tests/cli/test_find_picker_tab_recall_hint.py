"""Tier 2: SlashPicker renders the command-supplied ``tab_footer_fn`` footer.

The picker hint can show a dim "↳ <message>" sub-row supplied by a
command via ``SlashCommand.tab_footer_fn`` (a ``() -> str | None``). The
command owns the message + its visibility condition; the picker stays
generic and never hardcodes a command name (P7 — see issue #1070, which
removed the original ``if cmd.name == "find":`` hardcode).

/find is the canonical consumer: it surfaces "Tab inserts a recent
query" only when its history is non-empty (guard: avoids implying Tab
does something when there's nothing to recall). The first three tests
pin /find's end-to-end wiring; the last two pin the generic mechanism
with a synthetic command (proving the picker is not /find-specific).

Public surface tested (no MagicMock, no private-state assertions):
  - /find + non-empty history → hint contains "Tab inserts"
  - /find + empty history → hint does NOT contain "Tab inserts"
  - command without tab_footer_fn → no footer (back-compat / scope guard)
  - synthetic command whose tab_footer_fn returns text → footer rendered
  - synthetic command whose tab_footer_fn returns None → no footer
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


def _seed_find_history(*queries: str) -> None:
    """Inject queries into the find history deque via the public record helper.

    Uses ``_record_find_history`` (module-private, but a write helper
    rather than a state assertion) to pre-populate history. This avoids
    poking the deque directly while still letting the test control state.
    The matching test uses ``find_history_has_entries`` (the new public
    surface) to verify, not the deque.
    """
    from reyn.slash.find import _record_find_history
    for q in queries:
        _record_find_history(q)


def _clear_find_history() -> None:
    """Drain the find history deque between tests to prevent cross-test bleed."""
    from reyn.slash import find as _find_mod
    _find_mod._find_history.clear()


@pytest.mark.asyncio
async def test_find_hint_shows_tab_recall_footer_when_history_nonempty() -> None:
    """Tier 2: /find hint shows Tab-recall footer when history is non-empty."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import REGISTRY

    _clear_find_history()
    _seed_find_history("myquery")

    try:
        app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
        async with app.run_test(headless=True) as pilot:
            await pilot.pause()
            picker = app.query_one("#slash-picker", SlashPicker)

            find_cmd = REGISTRY.get("find")
            assert find_cmd is not None, "/find must be registered"

            picker.set_hint(find_cmd)
            await pilot.pause()
            text = _picker_text(picker)

            assert "Tab inserts" in text, (
                f"Expected 'Tab inserts' in hint when /find history is non-empty. "
                f"Got:\n{text!r}"
            )
    finally:
        _clear_find_history()


@pytest.mark.asyncio
async def test_find_hint_omits_tab_recall_footer_when_history_empty() -> None:
    """Tier 2: /find hint omits Tab-recall footer when history is empty."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import REGISTRY

    _clear_find_history()

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        find_cmd = REGISTRY.get("find")
        assert find_cmd is not None, "/find must be registered"

        picker.set_hint(find_cmd)
        await pilot.pause()
        text = _picker_text(picker)

        assert "Tab inserts" not in text, (
            f"Expected no 'Tab inserts' in hint when /find history is empty. "
            f"Got:\n{text!r}"
        )


@pytest.mark.asyncio
async def test_command_without_tab_footer_fn_renders_no_footer() -> None:
    """Tier 2: a command that supplies no tab_footer_fn renders no footer (back-compat)."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import SlashCommand

    # Populated find history must NOT leak into an unrelated command's hint:
    # the picker keys off the command's own tab_footer_fn, not global state.
    _clear_find_history()
    _seed_find_history("myquery")

    try:
        app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
        async with app.run_test(headless=True) as pilot:
            await pilot.pause()
            picker = app.query_one("#slash-picker", SlashPicker)

            async def _h(s, a):
                return None

            # No tab_footer_fn → defaults to None → no footer row.
            other_cmd = SlashCommand(
                name="help",
                summary="Show command help",
                handler=_h,
            )
            picker.set_hint(other_cmd)
            await pilot.pause()
            text = _picker_text(picker)

            assert "Tab inserts" not in text, (
                f"Expected no 'Tab inserts' for a command without tab_footer_fn even "
                f"with populated find history. Got:\n{text!r}"
            )
    finally:
        _clear_find_history()


@pytest.mark.asyncio
async def test_generic_command_tab_footer_fn_text_is_rendered() -> None:
    """Tier 2: the picker renders ANY command's tab_footer_fn message (not /find-specific)."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import SlashCommand

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _h(s, a):
            return None

        # A synthetic, non-find command supplying a footer. Proves the picker
        # is generic: it renders whatever tab_footer_fn returns, for any name.
        synthetic = SlashCommand(
            name="synthetic",
            summary="A synthetic command for the generic footer test",
            handler=_h,
            tab_footer_fn=lambda: "generic footer line",
        )
        picker.set_hint(synthetic)
        await pilot.pause()
        text = _picker_text(picker)

        assert "generic footer line" in text, (
            f"Expected the synthetic command's tab_footer_fn message to render. "
            f"Got:\n{text!r}"
        )


@pytest.mark.asyncio
async def test_generic_command_tab_footer_fn_none_renders_no_footer() -> None:
    """Tier 2: a tab_footer_fn returning None renders no footer (visibility is command-owned)."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets.slash_picker import SlashPicker
    from reyn.slash import SlashCommand

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _h(s, a):
            return None

        synthetic = SlashCommand(
            name="synthetic",
            summary="A synthetic command whose footer is currently hidden",
            handler=_h,
            tab_footer_fn=lambda: None,
        )
        picker.set_hint(synthetic)
        await pilot.pause()
        text = _picker_text(picker)

        assert "↳" not in text, (
            f"Expected no footer chrome when tab_footer_fn returns None. "
            f"Got:\n{text!r}"
        )
