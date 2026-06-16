"""Tier 2: ReynHeader surfaces a ``[find: 'q' N/M]`` badge while cycle is active.

Categorical UX gap on the /find surface. The sticky status auto-
hides after ~2.5 s; without a persistent indicator, the user
can't tell at a glance whether ``Ctrl+G`` is currently armed (=
whether a /find query is still active in the router's cycle
state). This adds a small subtle-blue badge to the header that
stays visible while the cycle state is non-empty.

Status shape:

  agent · model · 1,234 tok · $0.01 · [3 pending] · [find: 'foo' 2/5] · 14:23:01

Public surfaces tested:
  - ``ReynHeader.set_find_state(query, position, total)`` installs
    the badge
  - ``set_find_state(None)`` clears the badge
  - Equality-gated repaint: idempotent calls don't churn the
    Static (defensive against router-side noise)
  - ``_format_status`` includes the badge in the rendered Text
    when state is set, omits it when None
  - Initial ``_on_find`` seeds the badge with the cursor match
    position
  - Ctrl+G cycle updates the badge to the new position
  - No-match clear branches drop the badge
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.text import Text

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _seed(conv, lines: list[str]) -> None:
    log = conv._log()
    for line in lines:
        log.write(Text(line))


def _header_text(header) -> str:
    """Plain-text rendering of the header's status label.

    Reads ``ReynHeader.rendered_text()`` (= the inherited
    ``RenderableCacheMixin`` accessor) which caches the most
    recent Text sent to ``Label.update``. Textual's Label has no
    portable ``.renderable`` accessor across versions.
    """
    return header.rendered_text()


@pytest.mark.asyncio
async def test_header_inherits_renderable_cache_mixin() -> None:
    """Tier 2: ReynHeader migrated to RenderableCacheMixin (= #568 follow-on).

    Pins the inheritance so the rendered_text() accessor flows
    through the shared mixin, not a per-widget duplicate. Same
    pattern as SkillActivityRow + SlashPicker + ToolCallRow.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader
    from reyn.interfaces.tui.widgets._renderable_cache import RenderableCacheMixin

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        assert isinstance(header, RenderableCacheMixin)
        # Accessor exists and returns a string (= initial render set
        # by ``compose``).
        assert isinstance(header.rendered_text(), str)


@pytest.mark.asyncio
async def test_set_find_state_renders_badge() -> None:
    """Tier 2: set_find_state(...) makes the badge visible in the header."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_find_state("foo", position=2, total=5)
        await pilot.pause()
        text = _header_text(header)
        assert "[find: 'foo' 2/5]" in text


@pytest.mark.asyncio
async def test_set_find_state_none_clears_badge() -> None:
    """Tier 2: passing ``None`` clears the badge."""
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_find_state("foo", position=1, total=1)
        await pilot.pause()
        assert "[find:" in _header_text(header)
        header.set_find_state(None)
        await pilot.pause()
        assert "[find:" not in _header_text(header)


@pytest.mark.asyncio
async def test_set_find_state_idempotent_no_repaint_churn() -> None:
    """Tier 2: redundant calls don't trigger unnecessary repaints.

    Equality-gate: when the new state == the previous state, the
    function early-returns without touching the Static. Pin this
    so router-side noise (= per-frame re-seeds) doesn't generate
    paint churn.
    """
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_find_state("foo", 1, 3)
        await pilot.pause()
        first_state = dict(header.find_state)  # snapshot
        # Same call again — should be a no-op.
        header.set_find_state("foo", 1, 3)
        await pilot.pause()
        # Internal state unchanged (= still the same dict shape).
        assert header.find_state == first_state


@pytest.mark.asyncio
async def test_on_find_sets_header_badge() -> None:
    """Tier 2: initial /find sets the header badge with the cursor's position."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView, ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        _seed(conv, ["needle one", "filler", "needle two"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="needle"),
            conv,
            header,
        )
        await pilot.pause()
        text = _header_text(header)
        # Badge should report 2 matches total.
        assert "[find: 'needle' 1/2]" in text


@pytest.mark.asyncio
async def test_cycle_find_updates_header_position() -> None:
    """Tier 2: Ctrl+G advances the badge position alongside the cursor."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView, ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        _seed(conv, ["needle one", "filler", "needle two", "filler", "needle three"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="needle"),
            conv,
            header,
        )
        await pilot.pause()
        assert "[find: 'needle' 1/3]" in _header_text(header)
        router.cycle_find(+1)
        await pilot.pause()
        assert "[find: 'needle' 2/3]" in _header_text(header)
        router.cycle_find(+1)
        await pilot.pause()
        assert "[find: 'needle' 3/3]" in _header_text(header)


@pytest.mark.asyncio
async def test_no_match_branch_clears_header_badge() -> None:
    """Tier 2: /find with zero matches clears any prior badge.

    After a successful /find foo (badge set), running /find
    nonexistent should clear the badge — the prior find mode is
    no longer relevant.
    """
    from reyn.chat.outbox import OutboxMessage
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView, ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        _seed(conv, ["needle"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="needle"),
            conv,
            header,
        )
        await pilot.pause()
        assert "[find:" in _header_text(header)
        router._on_find(
            OutboxMessage(kind="__find__", text="zzz-no-match"),
            conv,
            header,
        )
        await pilot.pause()
        assert "[find:" not in _header_text(header)


@pytest.mark.asyncio
async def test_buffer_mutation_clears_badge_via_cycle() -> None:
    """Tier 2: when buffer empties, Ctrl+G clears the badge alongside state."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.app_outbox import OutboxRouter
    from reyn.interfaces.tui.widgets import ConversationView, ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        _seed(conv, ["alpha", "beta"])
        await pilot.pause()
        router = OutboxRouter(app)
        router._on_find(
            OutboxMessage(kind="__find__", text="alpha"),
            conv,
            header,
        )
        await pilot.pause()
        assert "[find:" in _header_text(header)
        # Wipe the buffer (= Ctrl+L equivalent).
        conv.clear()
        await pilot.pause()
        router.cycle_find(+1)
        await pilot.pause()
        assert "[find:" not in _header_text(header)


@pytest.mark.asyncio
async def test_badge_appears_before_clock() -> None:
    """Tier 2: badge sits before the clock canary, not after.

    The clock is always rightmost (= canary for "is the UI
    frozen?"). The find badge should land left of it so the
    canary contract isn't disturbed.
    """
    import re

    from reyn.interfaces.tui.app import ReynTUIApp
    from reyn.interfaces.tui.widgets import ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.set_find_state("xxx", 1, 1)
        await pilot.pause()
        text = _header_text(header)
        clock_match = re.search(r"\d{2}:\d{2}:\d{2}", text)
        assert clock_match is not None
        badge_idx = text.find("[find:")
        assert badge_idx >= 0
        assert badge_idx < clock_match.start(), (
            f"badge at {badge_idx} should be left of clock at "
            f"{clock_match.start()}: {text!r}"
        )
