"""Tier 2: #6240 ③ (architect ruling, issue #6240 comment 5807710323) --
``Session._append_history``'s own disk write moves onto a
:class:`~reyn.core.events.durability_worker.DurabilityWorker`
(``submit_nowait``, fire-and-forget) instead of writing inline, mirroring
``EventStore.write`` (#6077 提案 3/6). ``_append_history`` itself stays a
plain ``def`` -- only the write moves off the loop.

This trades away the OLD synchronous "appended => durably readable, no
wait" contract (``tests/runtime/test_6077_history_append_handle_flush.py``
pinned it for the pre-③ shape) -- architect's ruling puts the cost on the
ONE reader that reads DISK rather than resident memory
(``Session._durable_active_history_after``, via
``CompactionController.force_compact_now``): it now awaits
``Session._flush_history_durability`` immediately before its own read.
Every RESIDENT reader (``build_history``, ``is_already_spilled`` /
``RouterHistoryBuffer._spill_supersede_map``, ``decompose_history_for_
retry`` -- all via ``self.history``, the SAME list ``_append_history``
still mutates synchronously) needs no such call at all -- untouched by
this PR, not re-witnessed here.

Real ``Session`` (via ``tests._support.agent_session.make_session``, the
same construction path ``test_4472_compaction_reads_durable_store.py``
and ``test_6077_history_append_handle_flush.py`` use) + real
``ChatMessage`` + a real filesystem (``tmp_path``) throughout -- no
``MagicMock``/``AsyncMock``/``patch``. No ``sleep`` anywhere: witness ①
below asserts on the FIRST tick after ``_append_history`` returns (no
``await`` has happened yet, so the worker's drain task cannot have run --
deterministic per asyncio's own cooperative-scheduling contract, not
timing-dependent), and witness ② asserts only after explicitly awaiting
the worker's own ``flush()``.

Strip-falsify performed in-file via Edit -> observe RED -> Edit back
(``git checkout``/``stash``/``restore`` never used, CLAUDE.md) for all
three witnesses; RED text recorded verbatim in each test's own docstring.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session(tmp_path: Path, *, name: str = "durability-worker-test") -> Session:
    return make_session(
        agent_name=name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )


@pytest.mark.asyncio
async def test_append_history_does_not_write_to_disk_before_the_worker_drains(
    tmp_path: Path,
) -> None:
    """Tier 2: witness ① (deny) -- with a running loop, ``_append_history``
    enqueues its disk write on the history ``DurabilityWorker`` and
    returns WITHOUT writing -- the very next synchronous line (no
    ``await`` in between) must see NO line on disk yet, proving the write
    genuinely moved off the loop rather than merely being reordered
    on it.

    Strip-falsify: temporarily changed ``Session._append_history``
    (session.py) to call ``self._write_history_record_owned(seq, record,
    role)`` directly instead of
    ``self._history_durability_worker.submit_nowait(...)`` (simulating
    the OLD, pre-③ inline write). Observed RED (verbatim, ``ts`` value
    elided as ``...``)::

        AssertionError: _append_history must NOT have written to disk yet
        -- the very next line after it returns must see no queued write
        landed. Got: '{"role": "user", "content": "not yet on disk", "ts":
        "...", "seq": 1, "meta": {"wal_seq": 0}, "tool_calls": null,
        "tool_call_id": null, "name": null, "spillability": "last_resort",
        "disclosure": null, "kind": "unspecified"}\\n'. If this failed,
        the disk write is happening INLINE again (submit_nowait removed
        from Session._append_history) instead of being deferred to the
        DurabilityWorker.
        assert '{"role": "us...specified"}\\n' == ''

    Reverted immediately after observing (restored the ``submit_nowait``
    dispatch); confirmed GREEN again."""
    session = _session(tmp_path)

    session._append_history(ChatMessage(role="user", content="not yet on disk", ts=_now()))

    on_disk = session.history_path.read_text() if session.history_path.exists() else ""
    assert on_disk == "", (
        "_append_history must NOT have written to disk yet -- the very "
        "next line after it returns must see no queued write landed. "
        f"Got: {on_disk!r}. If this failed, the disk write is happening "
        "INLINE again (submit_nowait removed from Session._append_history) "
        "instead of being deferred to the DurabilityWorker."
    )
    # Sanity: the RESIDENT append is still synchronous -- unchanged by ③.
    assert len(session.history) == 1 and session.history[0].content == "not yet on disk"


@pytest.mark.asyncio
async def test_worker_drain_makes_the_write_readable(tmp_path: Path) -> None:
    """Tier 2: witness ② (present) -- once
    :meth:`Session._flush_history_durability` has been awaited, every
    write enqueued before it is genuinely on disk. Pairs with witness ①
    above (deny/present over the SAME call) so neither passes vacuously.

    Strip-falsify: temporarily replaced
    ``Session._flush_history_durability``'s body with a bare ``return``
    (never awaiting the worker). Observed RED::

        AssertionError: after awaiting _flush_history_durability(), the
        line must be on disk -- got '' instead of content containing
        'now-on-disk'. If this failed, _flush_history_durability stopped
        actually awaiting DurabilityWorker.flush().

    Reverted immediately after observing (restored the ``await self.
    _history_durability_worker.flush()`` body); confirmed GREEN again."""
    session = _session(tmp_path)

    session._append_history(ChatMessage(role="user", content="now-on-disk", ts=_now()))
    await session._flush_history_durability()

    on_disk = session.history_path.read_text() if session.history_path.exists() else ""
    assert "now-on-disk" in on_disk, (
        "after awaiting _flush_history_durability(), the line must be on "
        f"disk -- got {on_disk!r} instead of content containing "
        "'now-on-disk'. If this failed, _flush_history_durability stopped "
        "actually awaiting DurabilityWorker.flush()."
    )


@pytest.mark.asyncio
async def test_durable_active_history_after_sees_a_just_appended_line_once_flushed(
    tmp_path: Path,
) -> None:
    """Tier 2: witness ③ -- the real read-back reader
    (``Session._durable_active_history_after``, the durable-store read
    ``CompactionController.force_compact_now`` dispatches via
    ``asyncio.to_thread`` after awaiting ``_flush_history_durability``
    immediately before it, compaction_controller.py) still sees
    just-appended content, byte-for-byte the same as
    ``tests/runtime/test_6077_history_append_handle_flush.py`` pinned for
    the pre-③ synchronous shape -- proving ③ did not silently change WHAT
    a flushed reader sees, only WHEN the write actually lands relative to
    ``_append_history`` returning.

    Three appends through the SAME session (exercising the worker's own
    FIFO ordering across enqueues, not just a single write) must all be
    durably readable via ``_durable_active_history_after`` once flushed --
    no wait beyond the one explicit ``await``, no sleep, no reopen.

    Strip-falsify: temporarily removed this test's own
    ``await session._flush_history_durability()`` call below -- the SAME
    hazard ``CompactionController.force_compact_now``'s own identical
    call (compaction_controller.py, immediately before
    ``await asyncio.to_thread(self._history_from_disk, prev_cover)``)
    exists to prevent. Observed RED::

        AssertionError: a just-appended line must be visible via
        _durable_active_history_after once the worker has been flushed --
        got [] instead of ['first', 'second', 'third']. If this failed,
        either the DurabilityWorker never actually wrote the lines, or
        _durable_active_history_after stopped reading the SAME file
        _append_history writes to.

    Reverted immediately after observing (restored the ``await session.
    _flush_history_durability()`` call below); confirmed GREEN again."""
    session = _session(tmp_path)

    for text in ("first", "second", "third"):
        session._append_history(ChatMessage(role="user", content=text, ts=_now()))
    await session._flush_history_durability()

    durable_turns, truncated = session._durable_active_history_after(0)
    contents = [m.content for m in durable_turns]
    assert contents == ["first", "second", "third"], (
        "a just-appended line must be visible via "
        "_durable_active_history_after once the worker has been flushed "
        f"-- got {contents!r} instead of ['first', 'second', 'third']. If "
        "this failed, either the DurabilityWorker never actually wrote "
        "the lines, or _durable_active_history_after stopped reading the "
        "SAME file _append_history writes to."
    )
    assert truncated is False, (
        "sanity: 3 short turns must never trip the batch-read truncation "
        "flag -- a True here would mean the read itself, not the flush "
        "contract, is what this assertion is (accidentally) exercising"
    )
