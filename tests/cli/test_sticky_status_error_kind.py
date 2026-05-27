"""Tier 2: StickyStatus ``kind="error"`` renders ✗ in bold red (I-F1).

Wave-10 Topic I finding F1 (P2): seven+ call sites passed
``kind="error"`` to ``show_status`` (``/copy`` failures in
app_outbox, ws_client reconnection notices, right_panel
preview-error surface). The kind was not in ``_GLYPHS`` so
``show()`` silently fell back to ``kind="thinking"`` — the error
message rendered with the same ⟳ amber glyph as the live
``⟳ thinking…`` indicator, easy to read as "the agent is still
working" rather than "an action failed".

After the fix:
  - ``_GLYPHS["error"]`` = ``"✗"`` (= same glyph as ToolCallRow /
    SkillActivityRow failure terminals — cross-surface vocabulary
    uniform)
  - ``_KIND_PRIORITY["error"]`` = 80 (above ``general``) so an
    error can displace a turn-flash
  - ``_repaint`` paints the error glyph in bold red so the alert
    semantic reads even on monochrome terminals (= shape + color
    as redundant cues)

Note: the original "error does NOT overwrite active thinking"
test has been removed. The ``"thinking"`` kind was removed from
StickyStatus — the inline Braille spinner (``InlineThinkingRow``)
replaced it. Error (priority 80) is now the highest-priority kind.

Public surfaces tested:
  - ``show("err", kind="error")`` → snapshot kind == "error"
    (no longer silent thinking fallback)
  - error overwrites general (= priority > 50)
  - error glyph + body appear in the rendered output (= the
    ``Static.update`` Text carries ``✗`` not ``⟳``)
  - unknown kind falls back to ``general`` (not ``thinking``)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


async def _sticky(pilot):
    """Get the StickyStatus mounted under the ConversationView."""
    from reyn.chat.tui.widgets import ConversationView
    conv = pilot.app.query_one("#conversation", ConversationView)
    return conv._sticky()


@pytest.mark.asyncio
async def test_error_kind_is_registered_not_silent_fallback() -> None:
    """Tier 2: ``show(kind="error")`` records "error" not "general"."""
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        s = await _sticky(pilot)
        s.show("copy failed", kind="error")
        snap = s.snapshot()
        assert snap["active"] is True
        assert snap["kind"] == "error", (
            f"error kind must be registered, got kind={snap['kind']!r}"
        )
        assert snap["body"] == "copy failed"


@pytest.mark.asyncio
async def test_error_overwrites_active_general() -> None:
    """Tier 2: ``error`` (priority 80) displaces ``general`` (priority 50)."""
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        s = await _sticky(pilot)
        s.show("↑ turn 2 / 5", kind="general")
        s.show("validation failed", kind="error")
        snap = s.snapshot()
        assert snap["kind"] == "error"
        assert snap["body"] == "validation failed"


@pytest.mark.asyncio
async def test_unknown_kind_falls_back_to_general() -> None:
    """Tier 2: unknown kind falls back to ``general`` (not ``thinking``).

    ``show()`` formerly fell back to ``kind="thinking"`` for unknown
    kinds. After ``"thinking"`` was removed, the fallback is now
    ``"general"``.
    """
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        s = await _sticky(pilot)
        s.show("some message", kind="unknown_kind")
        snap = s.snapshot()
        assert snap["kind"] == "general", (
            f"unknown kind should fall back to 'general', got {snap['kind']!r}"
        )


@pytest.mark.asyncio
async def test_error_glyph_resolves_to_check_cross() -> None:
    """Tier 2: ``_glyph`` resolves to ``✗`` (not the ⟳ thinking fallback).

    ``_glyph`` is the load-bearing internal — it's the string written
    into the Text the Static renders. Pre-fix this was ⟳ for the
    error kind too because the kind fell through to ``"thinking"``.
    """
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        s = await _sticky(pilot)
        s.show("copy failed", kind="error")
        await pilot.pause()
        # ``glyph`` is the resolved glyph string used by ``_repaint``.
        assert s.glyph == "✗", (
            f"error kind glyph should be ✗, got {s.glyph!r}"
        )
        # And the old thinking glyph must NOT have been selected.
        assert s.glyph != "⟳"
