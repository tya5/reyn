"""Tier 2: InterventionWidget "skip rest" chip — Wave-13 T2-4 / audit B#5.

When multiple interventions queue behind the head one, the user had no
surgical "decline rest" path: they had to either answer each, /pending
discard each, or Ctrl+C (= side-effect kills running skills).

Wave-13 T2-4 adds a dim "skip rest (N pending)" chip to
InterventionWidget when ``queued_extra > 0``.  Clicking it:
  1. Posts ``InterventionWidget.SkipRest`` message.
  2. App handler cancels every non-head iv in the registry.
  3. Emits one conv-log breadcrumb: "✗ N interventions skipped".
  4. Head intervention remains active (user must still answer it).

Public surfaces tested (per testing policy):
  - Widget rendered text (``plain_text``-style query on rendered output).
  - ``InterventionRegistry.list_active()`` — count after cancel.
  - Conv-log lines (``ConversationView._log_lines()`` public read surface).
  - ``InterventionWidget.SkipRest`` message attributes.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app():
    from reyn.tui.app import ReynTUIApp

    return ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)


def _make_registry():
    """Return an InterventionRegistry with a no-op announce callback."""
    from reyn.chat.services.intervention_registry import InterventionRegistry

    async def _noop(iv):  # type: ignore[no-untyped-def]  # noqa: ARG001
        pass

    return InterventionRegistry(on_announce=_noop)


def _make_iv(prompt: str = "allow?"):
    """Return a UserIntervention with a live future (for the running loop)."""
    import asyncio

    from reyn.user_intervention import UserIntervention

    iv = UserIntervention(kind="ask_user", prompt=prompt)
    # Replace the placeholder future with one bound to the running loop.
    try:
        loop = asyncio.get_running_loop()
        iv.future = loop.create_future()
    except RuntimeError:
        pass
    return iv


# ---------------------------------------------------------------------------
# Test 1: "skip rest" chip renders when queued_extra > 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_rest_chip_renders_when_queued() -> None:
    """Tier 2: InterventionWidget with queued_extra=3 renders 'skip rest (3 pending)' chip.

    Pins the scope-guard for Test 2 (queued_extra=0 → no chip).
    Uses the widget's own DOM (``query(Button)`` labels) so the assertion
    is on the public widget surface, not private state.
    """
    from textual.widgets import Button

    from reyn.tui.widgets.intervention import InterventionWidget

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        from reyn.tui.widgets import ConversationView

        conv = app.query_one("#conversation", ConversationView)
        # Mount head intervention with 3 queued behind it.
        conv.mount_intervention(
            question="allow write?",
            choices=[{"label": "[y]es", "id": "yes", "hotkey": "y"}],
            iv_id="iv-head",
            queued_extra=3,
        )
        await pilot.pause()

        # Collect all button labels rendered in the widget.
        buttons = list(app.query(Button))
        labels = [str(b.label) for b in buttons]
        # "skip rest (3 pending)" must appear as one of the chip labels.
        skip_labels = [l for l in labels if "skip rest" in l and "3" in l]
        assert skip_labels, (
            f"Expected a 'skip rest (3 pending)' chip but found only: {labels!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: no "skip rest" chip when only head intervention (queued_extra=0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_skip_rest_chip_when_queue_empty() -> None:
    """Tier 2: InterventionWidget with queued_extra=0 does not render 'skip rest' chip.

    Scope guard: the skip button must only surface when there is something
    to skip, not as a permanent fixture on every intervention prompt.
    """
    from textual.widgets import Button

    from reyn.tui.widgets.intervention import InterventionWidget

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        from reyn.tui.widgets import ConversationView

        conv = app.query_one("#conversation", ConversationView)
        conv.mount_intervention(
            question="allow read?",
            choices=[{"label": "[y]es", "id": "yes", "hotkey": "y"}],
            iv_id="iv-only",
            queued_extra=0,
        )
        await pilot.pause()

        buttons = list(app.query(Button))
        labels = [str(b.label) for b in buttons]
        skip_labels = [l for l in labels if "skip rest" in l]
        assert not skip_labels, (
            f"'skip rest' chip must not appear when queued_extra=0; "
            f"found: {skip_labels!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: clicking skip_rest cancels all non-head IVs; head IV stays
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_rest_cancels_queued_ivs_not_head() -> None:
    """Tier 2: after skip_rest, all non-head IVs are cancelled; head IV remains.

    Wires a real InterventionRegistry into a stub session attached to the
    app via ``app._get_session`` so the handler can reach
    ``session._interventions``.  Uses ``registry.list_active()`` (public
    surface) to assert post-cancel state.
    """
    from reyn.chat.services.intervention_registry import InterventionRegistry
    from reyn.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        registry = _make_registry()
        # Build a minimal session stub that exposes _interventions.
        class _StubSession:
            _interventions = registry

        # Patch _get_session to return the stub.
        original_get_session = app._get_session
        app._get_session = lambda: _StubSession()  # type: ignore[method-assign]

        # Manually enqueue 4 interventions (head + 3 queued).
        iv_head = _make_iv("head-question")
        iv2 = _make_iv("q2")
        iv3 = _make_iv("q3")
        iv4 = _make_iv("q4")
        # Register in registry's internal queue directly (bypass dispatch
        # coroutine to keep the test synchronous + headless-safe).
        loop = asyncio.get_running_loop()
        for iv in (iv_head, iv2, iv3, iv4):
            iv.future = loop.create_future()
            registry._active[iv.id] = iv
            registry._order.append(iv.id)

        assert len(registry.list_active()) == 4

        # Mount the head intervention widget with queued_extra=3.
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_intervention(
            question="head-question",
            choices=[{"label": "[y]es", "id": "yes", "hotkey": "y"}],
            iv_id=iv_head.id,
            queued_extra=3,
        )
        await pilot.pause()

        # Click the "skip rest" chip.
        await pilot.click("#chip__skip_rest")
        await pilot.pause()

        # Head IV must still be active; all queued IVs must be gone.
        active_ids = {iv.id for iv in registry.list_active()}
        assert active_ids == {iv_head.id}, (
            f"Only head IV should remain after skip_rest; "
            f"active set={active_ids!r}, expected={{{iv_head.id!r}}}"
        )
        # Queued IVs' futures should be cancelled.
        for iv in (iv2, iv3, iv4):
            assert iv.future.done(), (
                f"IV {iv.id!r} future should be done (cancelled) after skip_rest"
            )

        app._get_session = original_get_session


# ---------------------------------------------------------------------------
# Test 4: breadcrumb emitted after skip_rest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_rest_emits_breadcrumb() -> None:
    """Tier 2: after skip_rest with 3 queued IVs, conv log contains '3 interventions skipped'.

    Uses ``ConversationView._log_lines()`` (the public read surface used
    by other Tier-2 tests) to assert the breadcrumb text.
    """
    from reyn.chat.services.intervention_registry import InterventionRegistry
    from reyn.tui.widgets import ConversationView

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        registry = _make_registry()

        class _StubSession:
            _interventions = registry

        app._get_session = lambda: _StubSession()  # type: ignore[method-assign]

        loop = asyncio.get_running_loop()
        iv_head = _make_iv("head-q")
        queued = [_make_iv(f"q{i}") for i in range(3)]
        for iv in (iv_head, *queued):
            iv.future = loop.create_future()
            registry._active[iv.id] = iv
            registry._order.append(iv.id)

        conv = app.query_one("#conversation", ConversationView)
        conv.mount_intervention(
            question="head-q",
            choices=[{"label": "[y]es", "id": "yes", "hotkey": "y"}],
            iv_id=iv_head.id,
            queued_extra=3,
        )
        await pilot.pause()

        await pilot.click("#chip__skip_rest")
        await pilot.pause()

        # dump_buffer_text() is the public plain-text read surface for the
        # RichLog buffer (used by /save and other Tier-2 tests).
        lines = conv.dump_buffer_text()
        breadcrumb_lines = [l for l in lines if "skipped" in l and "3" in l]
        assert breadcrumb_lines, (
            f"Expected a breadcrumb containing '3' and 'skipped' in conv log. "
            f"Log lines: {lines!r}"
        )
