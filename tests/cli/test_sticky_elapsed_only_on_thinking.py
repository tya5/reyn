"""Tier 2: elapsed suffix shows only for kind="thinking" (I-F2).

Wave-10 follow-up Topic I finding F2 (P2): ``_repaint`` rendered
``· N.Ns`` regardless of kind, so a transient ``general`` flash
like ``↑ turn 3 / 8`` displayed as ``↑ turn 3 / 8  · 1.4s`` —
the elapsed counter is meaningful only when the user is asking
"how long has the agent been working?", which is the ``thinking``
semantics. For navigation breadcrumbs and one-shot error notices,
the appended elapsed read as noise the user neither needed nor
expected.

After the fix the elapsed suffix is emitted only when
``self._kind == "thinking"``. ``_start`` is still refreshed on
each ``show()`` so the value is meaningful for any future kind
that opts in.

Public surfaces tested:
  - thinking → render contains ``· N.Ns`` (regression guard)
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
    """Drive a real mounted StickyStatus + read the rendered plain text.

    Backs the kind/body into the sticky via ``show``, lets the
    coalesced ``_repaint`` run, then reads the widget's internal
    state through the snapshot path + reconstructs the rendered
    text by calling ``_repaint`` and capturing the ``update`` arg.
    """
    from reyn.chat.tui.widgets import ConversationView
    conv = pilot.app.query_one("#conversation", ConversationView)
    sticky = conv._sticky()
    sticky.show(body, kind=kind)
    # Back-date _start so a meaningful elapsed value appears for
    # the thinking case. 2.5s is enough to verify the float pattern.
    import time
    sticky._start = time.monotonic() - 2.5
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
async def test_thinking_kind_renders_elapsed_suffix() -> None:
    """Tier 2 (regression): thinking still shows ``· N.Ns``."""
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        plain = await _render_plain(pilot, "thinking", "thinking…")
        assert "thinking…" in plain
        assert _ELAPSED_RE.search(plain), (
            f"thinking should render elapsed suffix, got: {plain!r}"
        )


@pytest.mark.asyncio
async def test_general_kind_does_not_render_elapsed_suffix() -> None:
    """Tier 2: general flash has no trailing elapsed."""
    from reyn.chat.tui.app import ReynTUIApp

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
    from reyn.chat.tui.app import ReynTUIApp

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        plain = await _render_plain(pilot, "error", "copy failed")
        assert "copy failed" in plain
        assert not _ELAPSED_RE.search(plain), (
            f"error kind should not render elapsed suffix, got: {plain!r}"
        )
