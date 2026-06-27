"""Tier 2: inline app background-task robustness (_submit / _quit).

Both are launched via create_background_task from key bindings, so an uncaught
error or a double-fire is invisible/harmful: a failing _submit silently drops the
input, and a second _quit racing the first hits app.exit() twice ("Return value
already set"). These pin the contained behaviour with real instances (no mocks).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from reyn.interfaces.inline.app import _quit, _submit


class _RaisingSession:
    async def submit_user_text(self, text: str) -> None:
        raise RuntimeError("simulated submit failure")


@pytest.mark.asyncio
async def test_submit_surfaces_error_instead_of_failing_silently() -> None:
    """Tier 2: a submit_user_text failure is contained and surfaced as an error
    line on the outbox, not swallowed by the background-task exception handler."""
    registry = SimpleNamespace(
        attached_session=lambda: _RaisingSession(),
        repl_outbox=asyncio.Queue(),
    )
    await _submit(registry, "hello")  # must not raise out of the background task
    msg = registry.repl_outbox.get_nowait()
    assert msg.kind == "error"
    assert msg.text  # a non-empty, user-visible message


class _CountingApp:
    def __init__(self) -> None:
        self.exit_calls = 0

    def exit(self) -> None:
        self.exit_calls += 1


@pytest.mark.asyncio
async def test_quit_is_idempotent_under_rapid_double_quit() -> None:
    """Tier 2: two concurrent _quit (rapid Ctrl-C / `/quit` then Ctrl-C) call
    app.exit() exactly once — the shared guard dedups before the first await."""
    async def _shutdown() -> None:
        return None

    registry = SimpleNamespace(shutdown=_shutdown)
    app = _CountingApp()
    state: dict = {}
    await asyncio.gather(
        _quit(registry, app, state),
        _quit(registry, app, state),
    )
    assert app.exit_calls == 1
