"""Tier 2: /compact slash — handler paths (no-engine error, raises, nothing-to-compact, success).

`compact_cmd` has four distinct paths based on whether the compaction engine
is wired, whether it raises, and the `summarized_turns` value in its result.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.compact import compact_cmd
from reyn.runtime.outbox import OutboxMessage


class _FakeSession:
    def __init__(self, *, compact_now=None) -> None:
        if compact_now is not None:
            self._compact_now_for_op = compact_now
        self._outbox: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def reply_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "error")


@pytest.mark.asyncio
async def test_compact_no_engine_sends_error() -> None:
    """Tier 2: /compact with no _compact_now_for_op wired replies an error."""
    session = _FakeSession()  # no compact_now attr
    await compact_cmd(session, "")
    assert session.error_text(), "expected error reply when engine absent"
    assert not session.reply_text(), "expected no system reply when engine absent"


@pytest.mark.asyncio
async def test_compact_engine_raises_sends_error_with_message() -> None:
    """Tier 2: /compact when the engine raises surfaces the exception text, not a crash."""
    async def _raising():
        raise RuntimeError("disk full")

    session = _FakeSession(compact_now=_raising)
    await compact_cmd(session, "")
    err = session.error_text()
    assert err, "expected an error reply"
    assert "disk full" in err


@pytest.mark.asyncio
async def test_compact_nothing_to_compact_no_free_window() -> None:
    """Tier 2: summarized_turns=0 without free_window_after → 'Nothing to compact' reply."""
    async def _nothing():
        return {"summarized_turns": 0}

    session = _FakeSession(compact_now=_nothing)
    await compact_cmd(session, "")
    text = session.reply_text()
    assert "nothing" in text.lower() or "already fits" in text.lower()
    assert not session.error_text()


@pytest.mark.asyncio
async def test_compact_nothing_to_compact_with_free_window_includes_token_count() -> None:
    """Tier 2: summarized_turns=0 + free_window_after → token count surfaced in reply."""
    async def _nothing():
        return {"summarized_turns": 0, "free_window_after": 45000}

    session = _FakeSession(compact_now=_nothing)
    await compact_cmd(session, "")
    text = session.reply_text()
    assert "45000" in text, f"expected free token count in reply; got: {text!r}"


@pytest.mark.asyncio
async def test_compact_success_mentions_summarized_turns() -> None:
    """Tier 2: successful compaction (summarized_turns>0) surfaces the turn count."""
    async def _success():
        return {
            "summarized_turns": 3,
            "compressed_tokens": 1200,
            "bridge_tokens": 180,
        }

    session = _FakeSession(compact_now=_success)
    await compact_cmd(session, "")
    text = session.reply_text()
    assert "3" in text, "turn count not in reply"
    assert not session.error_text()


@pytest.mark.asyncio
async def test_compact_success_singular_turn_word() -> None:
    """Tier 2: exactly 1 summarized turn uses singular 'turn' not 'turns'."""
    async def _one():
        return {"summarized_turns": 1, "compressed_tokens": 400, "bridge_tokens": 60}

    session = _FakeSession(compact_now=_one)
    await compact_cmd(session, "")
    text = session.reply_text()
    assert "1 older turn" in text, f"singular not used; got: {text!r}"
    assert "turns" not in text


@pytest.mark.asyncio
async def test_compact_success_plural_turns_word() -> None:
    """Tier 2: multiple summarized turns uses plural 'turns'."""
    async def _many():
        return {"summarized_turns": 5, "compressed_tokens": 2000, "bridge_tokens": 300}

    session = _FakeSession(compact_now=_many)
    await compact_cmd(session, "")
    text = session.reply_text()
    assert "turns" in text, f"plural not used; got: {text!r}"
