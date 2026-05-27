"""Tier 2: session.error_box_count is wired to mount/dismiss/clear lifecycle.

Wave-13 cascade audit finding C[1]: session.error_box_count was declared
on ChatSession (line 1627) with the comment "TUI outbox handler increments
on mount_error and decrements on dismiss", but NO caller ever wrote to it.
/pending list and /reset preview always showed "0 errors" regardless of how
many ErrorBoxes were actually mounted.

Fixed by:
  - app_outbox._on_error: increment after conv.render_message() — defensive
    getattr guard so a None/_stub session doesn't crash.
  - ConversationView.dismiss_last_error: decrement via self.app._get_session().
  - ConversationView.dismiss_all_errors: zero out via self.app._get_session().
  - ConversationView.clear: zero out via self.app._get_session().

Pinned invariants:
  1. 3 × render_message (each with kind="error") → count == 3.
  2. dismiss_last_error → count decrements to 2.
  3. dismiss_all_errors → count == 0.
  4. clear() with mounted boxes → count == 0.
  5. session without _error_box_count attr → defensive no-op, no crash.

No MagicMock / AsyncMock / patch.  Real ReynTUIApp + ConversationView
instances.  Stub session attached via app._get_session override (same
pattern as test_out_of_band_signals.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── shared helpers ─────────────────────────────────────────────────────────────


class _StubSession:
    """Minimal session stub exposing error_box_count."""

    def __init__(self) -> None:
        self._error_box_count: int = 0

    @property
    def error_box_count(self) -> int:
        """Mirror of ChatSession.error_box_count for the fake stub."""
        return self._error_box_count


class _StubSessionNoAttr:
    """Session-like stub that intentionally LACKS _error_box_count.

    Used to verify the defensive guard: wiring code must not raise
    AttributeError when working with a stripped / mock session.
    """


def _make_app():
    from reyn.chat.tui.app import ReynTUIApp
    return ReynTUIApp(registry=None, agent_name="t", model="m", budget_tracker=None)


def _make_error_msg(text: str = "oops"):
    from reyn.chat.outbox import OutboxMessage
    return OutboxMessage(kind="error", text=text, meta={"skill": "foo"})


# ── test 1: 3 render_message calls → count == 3 ───────────────────────────────


@pytest.mark.asyncio
async def test_three_render_messages_count_three() -> None:
    """Tier 2: 3 × render_message via _on_error → session.error_box_count == 3."""
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

    app = _make_app()
    session = _StubSession()

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        # Wire the stub session so app_outbox._on_error can reach it.
        app._get_session = lambda: session  # type: ignore[method-assign]

        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        for i in range(3):
            router._on_error(_make_error_msg(f"error {i}"), conv, header)
        await pilot.pause()

        assert session.error_box_count == 3, (
            f"expected 3 after 3 × _on_error, got {session.error_box_count}"
        )


# ── test 2: dismiss_last_error → count decrements ─────────────────────────────


@pytest.mark.asyncio
async def test_dismiss_last_error_decrements_count() -> None:
    """Tier 2: dismiss_last_error after 3 mounts → count decrements to 2."""
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

    app = _make_app()
    session = _StubSession()

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        app._get_session = lambda: session  # type: ignore[method-assign]

        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        for i in range(3):
            router._on_error(_make_error_msg(f"error {i}"), conv, header)
        await pilot.pause()

        assert session.error_box_count == 3, "pre-condition: 3 mounts"

        conv.dismiss_last_error()
        await pilot.pause()

        assert session.error_box_count == 2, (
            f"expected 2 after one dismiss, got {session.error_box_count}"
        )


# ── test 3: dismiss_all_errors → count == 0 ───────────────────────────────────


@pytest.mark.asyncio
async def test_dismiss_all_errors_zeros_count() -> None:
    """Tier 2: dismiss_all_errors after 3 mounts → count == 0."""
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

    app = _make_app()
    session = _StubSession()

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        app._get_session = lambda: session  # type: ignore[method-assign]

        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        for i in range(3):
            router._on_error(_make_error_msg(f"error {i}"), conv, header)
        await pilot.pause()

        assert session.error_box_count == 3, "pre-condition: 3 mounts"

        conv.dismiss_all_errors()
        await pilot.pause()

        assert session.error_box_count == 0, (
            f"expected 0 after dismiss_all_errors, got {session.error_box_count}"
        )


# ── test 4: clear() zeros count ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_zeros_count() -> None:
    """Tier 2: clear() with 3 mounted boxes → session.error_box_count == 0."""
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

    app = _make_app()
    session = _StubSession()

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        app._get_session = lambda: session  # type: ignore[method-assign]

        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        for i in range(3):
            router._on_error(_make_error_msg(f"error {i}"), conv, header)
        await pilot.pause()

        assert session.error_box_count == 3, "pre-condition: 3 mounts"

        conv.clear()
        await pilot.pause()

        assert session.error_box_count == 0, (
            f"expected 0 after clear(), got {session.error_box_count}"
        )


# ── test 5: session without _error_box_count → no crash ──────────────────────


@pytest.mark.asyncio
async def test_session_without_attr_defensive_no_crash() -> None:
    """Tier 2: session lacking _error_box_count → all operations complete without AttributeError.

    Defensive guard: a stripped/mock session must not cause the wiring
    code to raise.  mount_error, dismiss_last_error, dismiss_all_errors,
    and clear() must all complete without exception.
    """
    from reyn.chat.tui.app_outbox import OutboxRouter
    from reyn.chat.tui.widgets import ConversationView, ReynHeader

    app = _make_app()
    no_attr_session = _StubSessionNoAttr()

    async with app.run_test(headless=True) as pilot:
        await pilot.pause()

        # Wire a session that has NO _error_box_count attribute.
        app._get_session = lambda: no_attr_session  # type: ignore[method-assign]

        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        router = OutboxRouter(app)

        # All of these must not raise AttributeError.
        router._on_error(_make_error_msg("err1"), conv, header)
        router._on_error(_make_error_msg("err2"), conv, header)
        await pilot.pause()

        conv.dismiss_last_error()
        await pilot.pause()

        router._on_error(_make_error_msg("err3"), conv, header)
        conv.dismiss_all_errors()
        await pilot.pause()

        router._on_error(_make_error_msg("err4"), conv, header)
        conv.clear()
        await pilot.pause()

        # If we reach here without exception the defensive guard works.
        assert not hasattr(no_attr_session, "_error_box_count"), (
            "stub should not have gained _error_box_count; invariant check"
        )
