"""Tier 2: a raising slash handler is contained, not fatal to the session loop.

session.run()'s `while await run_one_iteration(): pass` has no `except`, so an
uncaught error from a slash handler propagates out and ends the session run loop —
the front-end keeps accepting input but never replies again. Slash dispatch now
wraps the handler call: it surfaces a clean error and treats the command as
consumed so the loop continues.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path) -> Session:
    return make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "alpha_snapshot.json",
    )


def _drain_outbox(session: Session) -> list:
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


@pytest.mark.asyncio
async def test_raising_slash_handler_is_contained_not_fatal(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: a slash handler that raises is caught — dispatch reports the
    command consumed (loop survives) and emits an error line."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    async def _boom(sess: Session, args: str) -> None:
        raise RuntimeError("handler exploded")

    from reyn.interfaces.slash import REGISTRY, SlashCommand

    # Register a throwaway raising command; monkeypatch auto-removes it at teardown.
    monkeypatch.setitem(
        REGISTRY._commands,
        "__f3boom__",
        SlashCommand(name="__f3boom__", summary="test", handler=_boom),
    )

    # Before the fix this raised RuntimeError out of dispatch (→ killed run()).
    consumed = await session._maybe_handle_slash("/__f3boom__")
    assert consumed is True  # handled → the run loop continues
    msgs = _drain_outbox(session)
    err = next(m for m in msgs if m.kind == "error")
    # Exception type + message must appear in the error text so the user sees
    # what went wrong without needing developer log access.
    assert "RuntimeError" in err.text
    assert "handler exploded" in err.text
