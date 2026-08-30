"""Tier 2: /compact slash — handler paths (no-engine error, raises, nothing-to-compact, success).

`compact_cmd` has four distinct paths based on whether the compaction engine
is wired, whether it raises, and the `summarized_turns` value in its result.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.compact import compact_cmd
from reyn.runtime.outbox import OutboxMessage
from tests._support.slash import slash_ctx


def _ctx(session):
    """The context the production dispatch hands a slash handler.

    The transport IS this test's display recorder — ``reply()`` writes
    through the client seam now (#3595 S4), so the list these assertions
    read is the one the transport fills.
    """
    return slash_ctx(session, recorder=session._outbox)


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
    await compact_cmd(_ctx(session), "")
    assert session.error_text(), "expected error reply when engine absent"
    assert not session.reply_text(), "expected no system reply when engine absent"


@pytest.mark.asyncio
async def test_compact_engine_raises_sends_error_with_message() -> None:
    """Tier 2: /compact when the engine raises surfaces the exception text, not a crash."""
    async def _raising():
        raise RuntimeError("disk full")

    session = _FakeSession(compact_now=_raising)
    await compact_cmd(_ctx(session), "")
    err = session.error_text()
    assert err, "expected an error reply"
    assert "disk full" in err


@pytest.mark.asyncio
async def test_compact_nothing_to_compact_no_free_window() -> None:
    """Tier 2: summarized_turns=0 without free_window_after → 'Nothing to compact' reply."""
    async def _nothing():
        return {"summarized_turns": 0}

    session = _FakeSession(compact_now=_nothing)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "nothing" in text.lower() or "already fits" in text.lower()
    assert not session.error_text()


@pytest.mark.asyncio
async def test_compact_nothing_to_compact_with_free_window_includes_token_count() -> None:
    """Tier 2: #5579 accept ②' (deny side) — summarized_turns=0 with a
    GENUINELY free window (free_window_after > 0) keeps the 'already fits'
    claim, unchanged, with the token count surfaced in reply. This is the
    deny-side counterweight to accept ①: proves the fix does not become
    "always warn regardless of free_window_after"."""
    async def _nothing():
        return {"summarized_turns": 0, "free_window_after": 45000}

    session = _FakeSession(compact_now=_nothing)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "45000" in text, f"expected free token count in reply; got: {text!r}"
    assert "already fits" in text.lower()


@pytest.mark.asyncio
async def test_compact_nothing_summarized_but_window_still_full_does_not_claim_fits() -> None:
    """Tier 2: #5579 accept ①' — the owner's own observed contradiction
    (summarized_turns=0 AND free_window_after=0 in the SAME reply) must not
    happen. summarized_turns == 0 has three possible causes (no candidates
    / attempted-but-folded-nothing / watermark-didn't-advance) that
    _compact_now_for_op cannot currently distinguish (see compact.py's own
    comment) — the fix does not need to name which one happened; it only
    must stop asserting 'already fits the window' when free_window_after
    says otherwise (== 0, no threshold — max(0, effective_trigger - after)
    is already an exact computed value, not an estimate needing a fudge
    factor)."""
    async def _stuck():
        return {"summarized_turns": 0, "free_window_after": 0}

    session = _FakeSession(compact_now=_stuck)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert text, "expected a non-empty reply, not silence"
    assert "already fits" not in text.lower(), (
        f"reply must not claim the window already fits when free_window_after=0; got: {text!r}"
    )
    assert not session.error_text()


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
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "3" in text, "turn count not in reply"
    assert not session.error_text()


@pytest.mark.asyncio
async def test_compact_success_singular_turn_word() -> None:
    """Tier 2: exactly 1 summarized turn uses singular 'turn' not 'turns'."""
    async def _one():
        return {"summarized_turns": 1, "compressed_tokens": 400, "bridge_tokens": 60}

    session = _FakeSession(compact_now=_one)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "1" in text, f"count not in reply; got: {text!r}"
    assert "turn" in text, f"singular 'turn' not in reply; got: {text!r}"
    assert "turns" not in text


@pytest.mark.asyncio
async def test_compact_success_plural_turns_word() -> None:
    """Tier 2: multiple summarized turns uses plural 'turns'."""
    async def _many():
        return {"summarized_turns": 5, "compressed_tokens": 2000, "bridge_tokens": 300}

    session = _FakeSession(compact_now=_many)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "turns" in text, f"plural not used; got: {text!r}"
