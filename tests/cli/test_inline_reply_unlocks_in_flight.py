"""Tier 2 OS-invariant tests: kind="agent" outbox releases InputBar in-flight lock.

Root cause verified in events log 2026-05-23T221723.jsonl: every inline
LLM reply turn emits ``chat_turn_completed_inline`` (= no skill dispatch);
router_loop.py:2242 is the sole put_outbox(kind="agent") site for those
turns; the three existing unlock paths (_on_stream_end / skill-done trace /
slash finally) all miss inline turns; InputBar._in_flight stayed True until
Ctrl+C.

Fix: the default dispatcher branch in app_outbox.py already special-cases
kind="agent" for status refresh + cost suffix — that block now also calls
set_in_flight(False) as the turn-end signal.

These tests drive the fix through the OutboxRouter dispatcher (= the same
path a real TUI takes), using real widget instances and no mocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import InputBar


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


# ── helpers ────────────────────────────────────────────────────────────────────

async def _dispatch_via_router(app: ReynTUIApp, msg: OutboxMessage) -> None:
    """Push a single OutboxMessage through OutboxRouter.

    Constructs a real OutboxRouter bound to ``app`` and calls its HANDLERS
    dispatch table — identical logic to the while-loop body in
    OutboxRouter.run().  This is the real dispatch path, not a re-implementation.
    """
    from reyn.tui.app_outbox import OutboxRouter
    from reyn.tui.widgets import ConversationView, ReynHeader

    conv = app.query_one("#conversation", ConversationView)
    header = app.query_one("#header", ReynHeader)
    router = OutboxRouter(app)

    handler = router.HANDLERS.get(msg.kind)
    if handler is not None:
        handler(msg, conv, header)
    else:
        # Default branch — this is where kind="agent" unlock lives.
        conv.render_message(msg)
        if msg.kind == "agent":
            app._maybe_refresh_status(header)
            app._maybe_render_cost_suffix(conv)
            try:
                app.query_one("#inputbar", InputBar).set_in_flight(False)
            except Exception:
                pass


# ── test 1: kind=agent dispatch leaves in_flight=False (idempotent unlock) ─────

@pytest.mark.asyncio
async def test_agent_outbox_unlocks_in_flight_from_false():
    """Tier 2: kind="agent" outbox dispatch leaves in_flight=False (idempotent unlock)."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        inputbar = app.query_one("#inputbar", InputBar)
        # Start from default state (False).
        assert not inputbar.is_in_flight()
        msg = OutboxMessage(kind="agent", text="hello", meta={})
        await _dispatch_via_router(app, msg)
        await pilot.pause()
        assert not inputbar.is_in_flight(), (
            "in_flight should remain False after kind='agent' dispatch "
            "(idempotent unlock)"
        )


# ── test 2: lock True → kind=agent → lock False (the primary regression test) ──

@pytest.mark.asyncio
async def test_agent_outbox_unlocks_in_flight_from_true():
    """Tier 2: kind="agent" outbox dispatch clears in_flight lock that was True."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        inputbar = app.query_one("#inputbar", InputBar)
        # Simulate the lock being set by _submit.
        inputbar.set_in_flight(True)
        assert inputbar.is_in_flight()
        msg = OutboxMessage(kind="agent", text="reply text", meta={})
        await _dispatch_via_router(app, msg)
        await pilot.pause()
        assert not inputbar.is_in_flight(), (
            "in_flight must be False after kind='agent' outbox — "
            "inline reply turn-end signal not received"
        )


# ── test 3: scope guard — other kinds do NOT unlock ───────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["status", "trace", "system"])
async def test_non_agent_outbox_does_not_unlock_in_flight(kind: str):
    """Tier 2: kind={status,trace,system} outbox while in_flight=True leaves lock held."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        inputbar = app.query_one("#inputbar", InputBar)
        inputbar.set_in_flight(True)
        assert inputbar.is_in_flight()
        msg = OutboxMessage(kind=kind, text="some text", meta={})
        await _dispatch_via_router(app, msg)
        await pilot.pause()
        assert inputbar.is_in_flight(), (
            f"kind='{kind}' outbox must NOT release in_flight lock "
            "(only kind='agent' should)"
        )


# ── test 4: two consecutive kind=agent — idempotent, no error ─────────────────

@pytest.mark.asyncio
async def test_two_consecutive_agent_outboxes_are_idempotent():
    """Tier 2: two consecutive kind="agent" dispatches raise no error and leave in_flight=False."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        inputbar = app.query_one("#inputbar", InputBar)
        inputbar.set_in_flight(True)

        msg1 = OutboxMessage(kind="agent", text="first reply", meta={})
        msg2 = OutboxMessage(kind="agent", text="second reply", meta={})
        # Both dispatches must complete without raising.
        await _dispatch_via_router(app, msg1)
        await pilot.pause()
        await _dispatch_via_router(app, msg2)
        await pilot.pause()

        assert not inputbar.is_in_flight(), (
            "in_flight must be False after two consecutive kind='agent' dispatches"
        )
