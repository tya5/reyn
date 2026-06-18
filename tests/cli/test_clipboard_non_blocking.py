"""Tier 2b: /copy off-loads its blocking subprocess to a thread executor.

Streaming / perf UX audit (MED severity Finding F3): the ``/copy``
handler called ``_copy_to_clipboard(text)`` synchronously inside the
async outbox loop. ``copy_to_clipboard`` uses ``subprocess.run(...,
timeout=2.0)`` — a blocking call. During that window, the outbox
``await repl_outbox.get()`` was suspended and no streaming chunks,
status messages, or trace events processed. ``/copy`` mid-stream could
freeze the TUI for up to ~2 seconds, then unblock in a burst.

The fix:
  1. ``copy_to_clipboard_async`` wraps the sync helper with
     ``loop.run_in_executor(None, ...)`` so the blocking subprocess
     runs on a worker thread.
  2. ``_on_copy_last_reply`` dispatches the work via ``asyncio.create_task``
     and returns synchronously. A "copying…" placeholder transient
     gives the user instant feedback; ``_finish_copy_async`` overwrites
     it with the success / failure result on completion.

Tests pin:
  - The async helper exists and is awaitable, returning the same shape.
  - ``_on_copy_last_reply`` returns synchronously (= no ``await``); the
    actual blocking work is scheduled for later.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui import _clipboard
from reyn.interfaces.tui._clipboard import copy_to_clipboard_async
from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.app_outbox import OutboxRouter
from reyn.interfaces.tui.widgets import ConversationView, ReynHeader
from reyn.runtime.outbox import OutboxMessage


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


# ── async helper contract ────────────────────────────────────────────────────


def test_copy_to_clipboard_async_is_awaitable_coroutine() -> None:
    """Tier 1: the async helper is a coroutine function with the same return shape."""
    assert inspect.iscoroutinefunction(copy_to_clipboard_async), (
        "copy_to_clipboard_async must be ``async def`` so callers can ``await`` it"
    )


def test_copy_to_clipboard_async_returns_tuple_shape() -> None:
    """Tier 2b: ``await``-ing the helper produces a ``(bool, str)`` tuple.

    Drives the helper with empty text against the executor. If no clipboard
    tool is on PATH, returns ``(False, "")``; if one is, returns ``(True,
    "<label>")``. Either way the shape is the contract.
    """
    out = asyncio.run(copy_to_clipboard_async(""))
    # Unpack directly — this pins the (bool, str) contract without a len() pin.
    ok, label = out
    assert isinstance(ok, bool)
    assert isinstance(label, str)


# ── handler does not block the outbox loop ──────────────────────────────────


@pytest.mark.asyncio
async def test_on_copy_last_reply_returns_synchronously() -> None:
    """Tier 2: the handler is sync and returns immediately, no awaiting subprocess.

    The handler used to hold the outbox loop for up to 2 s while
    ``subprocess.run`` ran. After the fix, it parses + arms a placeholder
    + schedules a worker, then returns. We verify the method itself is
    sync (= ``not iscoroutinefunction``) — the OutboxRouter dispatcher
    relies on this contract.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        header = app.query_one("#header", ReynHeader)
        # Lay down a reply so /copy has something to fetch
        conv._write_agent_markdown("hello reply")
        await pilot.pause()

        router = OutboxRouter(app)
        assert not inspect.iscoroutinefunction(router.on_copy_last_reply), (
            "on_copy_last_reply must stay sync — the OutboxRouter dispatch loop "
            "calls handlers without await"
        )

        # Drive the handler; it must complete without raising
        router.on_copy_last_reply(
            OutboxMessage(kind="__copy_last_reply__", text=""),
            conv, header,
        )
        # And a worker should have been scheduled — but we can't easily count
        # tasks portably; rely on the structural assertion above.


@pytest.mark.asyncio
async def test_finish_copy_async_overwrites_placeholder_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the worker replaces the "copying…" placeholder with the result.

    Monkey-patches ``copy_to_clipboard`` (NOT ``unittest.mock`` — just a
    direct attribute substitution via pytest's ``monkeypatch`` fixture)
    so the test doesn't depend on a real ``pbcopy`` / ``xclip`` being on
    PATH or actually mutating the system clipboard.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        captured: list[str] = []

        def _fake_copy(text: str) -> tuple[bool, str]:
            captured.append(text)
            return (True, "fake-clipboard")

        monkeypatch.setattr(_clipboard, "copy_to_clipboard", _fake_copy)

        router = OutboxRouter(app)
        await router._finish_copy_async(conv, "payload-xyz", n=2)
        await pilot.pause()

        # The fake was invoked with the exact text
        assert captured == ["payload-xyz"]
        # And a sticky should now be visible (the success transient)
        # — we can't easily read the sticky text without more plumbing,
        # but a follow-up _show_transient_status would have armed a fresh
        # timer, so the router reports an active timer.
        assert router.has_transient_status_timer()


@pytest.mark.asyncio
async def test_finish_copy_async_surfaces_failure_when_no_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: missing-tool path surfaces the install-hint, doesn't crash."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        def _fake_copy(_text: str) -> tuple[bool, str]:
            return (False, "")

        monkeypatch.setattr(_clipboard, "copy_to_clipboard", _fake_copy)

        router = OutboxRouter(app)
        await router._finish_copy_async(conv, "anything", n=1)
        await pilot.pause()
        # No crash, and a transient was armed (= failure status visible)
        assert router.has_transient_status_timer()
