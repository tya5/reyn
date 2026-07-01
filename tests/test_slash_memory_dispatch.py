"""Tier 2: /memory slash — dispatch paths that don't reach the data store.

The 'list' and 'view' subcommand paths call into ``reyn.data.memory`` which
requires a real filesystem.  This file covers the two paths that short-circuit
before touching the store: no-args (→ usage text) and unknown sub (→ error).
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.memory import memory_cmd
from reyn.runtime.outbox import OutboxMessage


class _FakeSession:
    def __init__(self) -> None:
        self._outbox: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def reply_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "error")


@pytest.mark.asyncio
async def test_memory_no_args_shows_usage() -> None:
    """Tier 2: /memory with no args → usage hint (not an error)."""
    session = _FakeSession()
    await memory_cmd(session, "")  # type: ignore[arg-type]
    text = session.reply_text()
    assert "list" in text.lower()
    assert "view" in text.lower()
    assert not session.error_text()


@pytest.mark.asyncio
async def test_memory_unknown_sub_replies_error() -> None:
    """Tier 2: /memory with an unrecognised sub-command → usage error."""
    session = _FakeSession()
    await memory_cmd(session, "delete foo")  # type: ignore[arg-type]
    assert session.error_text()
    assert not session.reply_text()
