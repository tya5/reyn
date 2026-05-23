"""Tier 2: /find picker hint shows Tab-recall footer iff history is non-empty.

Wave-12 T2-2 (Topic A #5). The /find command documents a Tab-recall
affordance (= last 5 queries accessible via Tab) only in its module
docstring. The picker hint had no visible surface for this, so users
had to read source to discover the feature.

This PR adds a dim "↳ Tab inserts a recent query" footer row to the
picker hint, rendered ONLY when:
  - the command is /find (scoped: other commands unaffected)
  - find history is non-empty (guard: avoids implying Tab does
    something when there's nothing to recall)

Public surface tested (no MagicMock, no private-state assertions):
  - /find + non-empty history → hint contains "Tab inserts"
  - /find + empty history → hint does NOT contain "Tab inserts"
  - non-find command + non-empty history → hint does NOT contain
    "Tab inserts" (scope guard)
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
    from reyn.chat.slash.find import _record_find_history
    for q in queries:
        _record_find_history(q)


def _clear_find_history() -> None:
    """Drain the find history deque between tests to prevent cross-test bleed."""
    from reyn.chat.slash import find as _find_mod
    _find_mod._find_history.clear()


@pytest.mark.asyncio
async def test_find_hint_shows_tab_recall_footer_when_history_nonempty() -> None:
    """Tier 2: /find hint shows Tab-recall footer when history is non-empty."""
    from reyn.chat.slash import REGISTRY
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

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
    from reyn.chat.slash import REGISTRY
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

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
async def test_non_find_hint_omits_tab_recall_footer_even_with_history() -> None:
    """Tier 2: non-/find command hint does NOT show Tab-recall footer even if history is populated."""
    from reyn.chat.slash import SlashCommand
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

    _clear_find_history()
    _seed_find_history("myquery")

    try:
        app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
        async with app.run_test(headless=True) as pilot:
            await pilot.pause()
            picker = app.query_one("#slash-picker", SlashPicker)

            async def _h(s, a):
                return None

            # A non-find command: name differs, so the scope guard applies.
            other_cmd = SlashCommand(
                name="help",
                summary="Show command help",
                handler=_h,
            )
            picker.set_hint(other_cmd)
            await pilot.pause()
            text = _picker_text(picker)

            assert "Tab inserts" not in text, (
                f"Expected no 'Tab inserts' in non-/find hint even with populated "
                f"find history. Got:\n{text!r}"
            )
    finally:
        _clear_find_history()
