"""Tier 2: elapsed suffix is never shown in sticky (kind="thinking" removed).

Wave-10 follow-up Topic I finding F2 originally tested that the elapsed
suffix was only shown for ``kind="thinking"``. The ``"thinking"`` kind has
since been removed from StickyStatus — the inline Braille spinner
(``InlineThinkingRow``) replaced it. This file now guards that neither
``general`` nor ``error`` render an elapsed suffix.

Public surfaces tested:
  - general → render does NOT contain elapsed suffix
  - error → render does NOT contain elapsed suffix
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


import re

_ELAPSED_RE = re.compile(r"·\s+\d+\.\d+s")


async def _render_plain(pilot, kind: str, body: str) -> str:
    """Drive a real mounted StickyStatus + read the rendered plain text."""
    from reyn.interfaces.tui.widgets import ConversationView
    conv = pilot.app.query_one("#conversation", ConversationView)
    sticky = conv._sticky()
    sticky.show(body, kind=kind)
    captured: dict = {}
    original_update = sticky.update
    def _spy(t):  # type: ignore[no-untyped-def]
        captured["text"] = t
        return original_update(t)
    sticky.update = _spy  # type: ignore[method-assign]
    sticky._repaint()
    await pilot.pause()
    return captured["text"].plain


@pytest.mark.asyncio
async def test_general_kind_does_not_render_elapsed_suffix() -> None:
    """Tier 2: general flash has no trailing elapsed."""
    from reyn.interfaces.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        plain = await _render_plain(pilot, "general", "↑ turn 3 / 8")
        assert "↑ turn 3 / 8" in plain
        assert not _ELAPSED_RE.search(plain), (
            f"general kind should not render elapsed suffix, got: {plain!r}"
        )


@pytest.mark.asyncio
async def test_error_kind_does_not_render_elapsed_suffix() -> None:
    """Tier 2: error flash has no trailing elapsed either."""
    from reyn.interfaces.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        plain = await _render_plain(pilot, "error", "copy failed")
        assert "copy failed" in plain
        assert not _ELAPSED_RE.search(plain), (
            f"error kind should not render elapsed suffix, got: {plain!r}"
        )
