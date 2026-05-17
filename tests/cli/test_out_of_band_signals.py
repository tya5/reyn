"""Tier 2: BEL + terminal title flag "user needed" events out-of-band.

Notification UX audit (HIGH severity Findings F1–F3): the TUI had no
audio (``\\a`` BEL) or out-of-band visual signal for attention events.
A user in another terminal tab had no way to know an intervention was
waiting, an error fired, or a skill aborted — the only signals were
in-pane and required the user to actively look at the TUI.

The fix wires two channels for the three attention events:

  1. ``self._app.bell()`` (via the ``alert()`` helper) — emits ``\\a``
     so terminals that translate BEL to a sound / flash give an audio
     or visual cue. No-op on quiet terminals.
  2. ``self.title = "reyn — <state>"`` (via ``set_title_state``) —
     surfaces in the terminal multiplexer's tab bar so a backgrounded
     reyn window broadcasts its state.

Events that trigger the signals:
  • Intervention mount → title="awaiting answer" + bell
  • Error mount → title="error" + bell
  • Skill aborted (``skill done: aborted``) → title="error" + bell
  • User submit → title resets to None (= "reyn")

These tests pin all four transitions plus the default-title invariant.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.app_outbox import OutboxRouter
from reyn.chat.tui.widgets import ConversationView, ReynHeader


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="aria",
        model="test-model",
        budget_tracker=None,
    )


def _instrument_alert(app: ReynTUIApp) -> list[None]:
    """Replace ``app.alert`` with a no-op counter via direct attribute swap.

    No ``unittest.mock`` per the testing policy.
    """
    calls: list[None] = []

    def _fake() -> None:
        calls.append(None)

    app.alert = _fake  # type: ignore[method-assign]
    return calls


# ── default title ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_title_is_reyn_not_class_name() -> None:
    """Tier 2: the default terminal title is ``"reyn"``, not ``"ReynTUIApp"``.

    Textual defaults the title to the App subclass name when ``TITLE``
    isn't set. That leaked an implementation detail to the terminal tab
    bar. Pinning the ``TITLE`` class attribute keeps the surface clean.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        assert app.title == "reyn", (
            f"expected default title 'reyn'; got {app.title!r}"
        )


# ── set_title_state helper ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_title_state_formats_consistently() -> None:
    """Tier 2: ``set_title_state(state)`` produces ``"reyn — <state>"`` or ``"reyn"``."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        app.set_title_state("awaiting answer")
        assert app.title == "reyn — awaiting answer"
        app.set_title_state("error")
        assert app.title == "reyn — error"
        app.set_title_state(None)
        assert app.title == "reyn"


# ── intervention triggers both signals ───────────────────────────────────────


@pytest.mark.asyncio
async def test_intervention_mount_sets_awaiting_and_rings_bell() -> None:
    """Tier 2: ``_on_intervention`` flips the title AND fires the bell.

    The bell is delegated to ``alert()``; instrumenting it lets us verify
    the call without depending on terminal capabilities. The title is
    user-visible state — assert directly on ``app.title``.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        # Stub _get_session so the queue-depth lookup doesn't crash on None
        app._get_session = lambda: None  # type: ignore[method-assign]

        alerts = _instrument_alert(app)
        router = OutboxRouter(app)
        router._on_intervention(
            OutboxMessage(
                kind="intervention",
                text="proceed?",
                meta={"intervention_id": "iv1"},
            ),
            conv, header,
        )
        await pilot.pause()

        assert app.title == "reyn — awaiting answer", (
            f"intervention must set the title; got {app.title!r}"
        )
        assert len(alerts) == 1, (
            f"intervention must fire exactly one bell; got {len(alerts)}"
        )


# ── error triggers both signals ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_mount_sets_error_title_and_rings_bell() -> None:
    """Tier 2: ``_on_error`` flips title to 'error' + rings bell."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)

        alerts = _instrument_alert(app)
        router = OutboxRouter(app)
        router._on_error(
            OutboxMessage(kind="error", text="boom"),
            conv, header,
        )
        await pilot.pause()

        assert app.title == "reyn — error"
        assert len(alerts) == 1


# ── user submit resets the title ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_submit_resets_title_to_idle() -> None:
    """Tier 2: a fresh user submit clears any prior attention flag.

    The user is back at the keyboard — the signal has done its job.
    Subsequent attention events arm the flag again from idle.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        # Pretend an error / intervention already flipped the title
        app.set_title_state("error")
        assert app.title == "reyn — error"

        # User types a fresh message — drive the submit path
        from reyn.chat.tui.widgets import InputBar
        app.post_message(InputBar.UserSubmitted("hello"))
        # Pump the message queue a few times
        for _ in range(5):
            await pilot.pause()

        assert app.title == "reyn", (
            f"user submit must reset title to idle; got {app.title!r}"
        )


# ── skill aborted triggers signals; finished is silent ───────────────────────


@pytest.mark.asyncio
async def test_skill_aborted_flags_error_and_rings_bell() -> None:
    """Tier 2: ``skill done: aborted`` flips the title + rings the bell.

    Success path (``skill done: finished``) stays silent — the in-pane
    completion line and any agent reply are signal enough; users don't
    want a bell for every successful skill.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        alerts = _instrument_alert(app)

        # Aborted path
        aborted = OutboxMessage(
            kind="trace",
            text="skill done: aborted",
            meta={"skill_name": "s", "run_id": "r1"},
        )
        app._handle_trace_for_skill_row(conv, aborted)
        await pilot.pause()
        assert app.title == "reyn — error"
        assert len(alerts) == 1

        # Reset for the success path
        app.set_title_state(None)
        alerts.clear()

        finished = OutboxMessage(
            kind="trace",
            text="skill done: finished",
            meta={"skill_name": "s", "run_id": "r2"},
        )
        app._handle_trace_for_skill_row(conv, finished)
        await pilot.pause()
        # Successful finish leaves the title at "reyn" (no flag change)
        assert app.title == "reyn"
        assert alerts == [], (
            f"successful finish must not ring the bell; got {len(alerts)}"
        )
