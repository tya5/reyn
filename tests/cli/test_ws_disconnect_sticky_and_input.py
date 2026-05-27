"""Tier 2: WS disconnect drives persistent sticky + InputBar disable (W13 T1-3).

Wave-13 Topic C finding #2 (P1): when the WS connection drops in
``--connect`` (remote WS) mode, the TUI previously showed only a single
ErrorBox below the fold. The sticky was silent, InputBar kept accepting
text that would never reach the server, and the user had no way to know
the session was dead.

After the fix:
  - ``ws_client._receive_loop`` emits an error frame with
    ``meta.source == "ws_disconnected"`` (sentinel).
  - ``app_outbox._on_error`` detects the sentinel and:
    1. Mounts a **persistent** ``✗ connection lost — restart TUI to
       reconnect`` sticky (= sticky remains until TUI exit).
    2. Calls ``InputBar.set_disconnected(True)`` which:
       - Sets the ``.disconnected`` CSS class.
       - Locks ``_in_flight`` so ``_submit`` swallows silently.
       - Sets ``_disconnected = True`` as an explicit guard.

Public surfaces tested (per testing.ja.md — no MagicMock / private
state assertions):

1. Disconnect frame → sticky snapshot contains "connection lost" with
   kind="error".
2. Disconnect frame → ``InputBar.disconnected`` property / CSS class
   True.
3. After disconnect, submit does NOT post a ``UserSubmitted`` message.
4. Normal error path (no sentinel) does NOT trigger the persistent
   disconnected state (scope guard).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_disconnect_msg():
    """Build the error OutboxMessage that ws_client emits on WS drop."""
    from reyn.chat.outbox import OutboxMessage
    return OutboxMessage(
        kind="error",
        text="connection lost: ConnectionResetError()",
        meta={"source": "ws_disconnected"},
    )


def _make_normal_error_msg():
    """Build a plain server-side error frame (no sentinel)."""
    from reyn.chat.outbox import OutboxMessage
    return OutboxMessage(
        kind="error",
        text="internal server error",
        meta={},
    )


async def _get_sticky(pilot):
    """Return the StickyStatus from the ConversationView."""
    from reyn.chat.tui.widgets import ConversationView
    conv = pilot.app.query_one("#conversation", ConversationView)
    return conv._sticky()


# ── test 1: sticky snapshot contains "connection lost" with kind="error" ──────


@pytest.mark.asyncio
async def test_disconnect_frame_shows_persistent_sticky_with_error_kind() -> None:
    """Tier 2: disconnect outbox frame → sticky "connection lost", kind="error".

    Synthesise the same OutboxMessage that ws_client emits on WS drop,
    drive it through _on_error, and verify the sticky shows the
    expected body + kind via the public snapshot() API.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        router = OutboxRouter(app)
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        msg = _make_disconnect_msg()

        router._on_error(msg, conv, header)
        await pilot.pause()

        sticky = await _get_sticky(pilot)
        assert sticky is not None, "StickyStatus must be mounted under ConversationView"
        snap = sticky.snapshot()

        assert snap["active"] is True, (
            f"sticky must be active after disconnect, got active={snap['active']!r}"
        )
        assert snap["kind"] == "error", (
            f"sticky kind must be 'error', got {snap['kind']!r}"
        )
        assert "connection lost" in snap["body"], (
            f"sticky body must contain 'connection lost', got {snap['body']!r}"
        )


# ── test 2: InputBar.disconnected True after disconnect frame ─────────────────


@pytest.mark.asyncio
async def test_disconnect_frame_sets_inputbar_disconnected_state() -> None:
    """Tier 2: disconnect outbox frame → InputBar.disconnected True + CSS class.

    After _on_error routes the ws_disconnected sentinel, InputBar must
    carry both the ``disconnected`` property (True) and the ``.disconnected``
    CSS class so styling can dim the border/text.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, InputBar, ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        router = OutboxRouter(app)
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        bar = app.query_one("#inputbar", InputBar)

        # Pre-condition: not yet disconnected.
        assert bar.disconnected is False
        assert not bar.has_class("disconnected")

        router._on_error(_make_disconnect_msg(), conv, header)
        await pilot.pause()

        assert bar.disconnected is True, (
            "InputBar.disconnected must be True after disconnect frame"
        )
        assert bar.has_class("disconnected"), (
            "InputBar must have .disconnected CSS class after disconnect frame"
        )


# ── test 3: submit does NOT post UserSubmitted after disconnect ───────────────


@pytest.mark.asyncio
async def test_disconnect_submit_swallowed_no_user_submitted_posted() -> None:
    """Tier 2: after disconnect, InputBar._submit swallows without posting.

    Simulates a user typing and pressing Enter after the WS session is
    dead. The message must NOT reach the outbox (= no UserSubmitted
    posted). The text stays in the TextArea so the user can see what
    they typed (= same UX as the in-flight guard).
    """
    from textual.widgets import TextArea

    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, InputBar, ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    posted: list[InputBar.UserSubmitted] = []

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        router = OutboxRouter(app)
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        bar = app.query_one("#inputbar", InputBar)
        ta = app.query_one("#input", TextArea)

        # Spy on InputBar.post_message.
        original_post = bar.post_message

        def _spy(msg):  # type: ignore[no-untyped-def]
            if isinstance(msg, InputBar.UserSubmitted):
                posted.append(msg)
                return True
            return original_post(msg)

        bar.post_message = _spy  # type: ignore[method-assign]

        # Trigger disconnect.
        router._on_error(_make_disconnect_msg(), conv, header)
        await pilot.pause()

        # Attempt to submit — should be swallowed.
        ta.load_text("hello after disconnect")
        bar._submit(ta)

        assert not posted, (
            f"UserSubmitted must not be posted when disconnected, got {posted!r}"
        )


# ── test 4: normal error frame does NOT trigger disconnected state ────────────


@pytest.mark.asyncio
async def test_normal_error_frame_does_not_trigger_disconnected_state() -> None:
    """Tier 2: plain server-side error (no sentinel) leaves InputBar enabled.

    Scope guard: only the ``ws_disconnected`` sentinel triggers the
    permanent lock. A normal server error (= meta.source absent or
    different) must leave InputBar fully functional and sticky NOT
    persistently locked on the disconnected message.
    """
    from reyn.chat.tui.app import ReynTUIApp
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, InputBar, ReynHeader

    app = ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        router = OutboxRouter(app)
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        bar = app.query_one("#inputbar", InputBar)

        router._on_error(_make_normal_error_msg(), conv, header)
        await pilot.pause()

        # InputBar must NOT be in disconnected state.
        assert bar.disconnected is False, (
            "Normal error must not set InputBar.disconnected"
        )
        assert not bar.has_class("disconnected"), (
            "Normal error must not add .disconnected CSS class"
        )

        # Sticky must NOT carry the disconnect message body.
        sticky = await _get_sticky(pilot)
        if sticky is not None:
            snap = sticky.snapshot()
            if snap["active"]:
                assert "connection lost" not in snap["body"], (
                    f"Normal error must not produce 'connection lost' sticky, "
                    f"got body={snap['body']!r}"
                )
