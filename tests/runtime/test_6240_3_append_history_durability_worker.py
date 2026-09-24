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
``Session.flush_history`` immediately before its own read.
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
witnesses; RED text recorded verbatim in each test's own docstring.

#6240 ③ follow-up (architect ruling, PR #6260 comment 5808618887):
deferring ``_append_history``'s write onto the worker reintroduces the
EXACT defect class #6257 closed for ``/clear-history`` -- a write still
QUEUED (not yet on disk) when ``Session.clear_history()`` deletes
``history_dir`` and opens a fresh active segment could otherwise land in
that fresh segment, silently un-clearing what the user just asked to
clear. Fixed by making ``clear_history`` ``async def`` and awaiting
:meth:`Session.flush_history` as its FIRST step (see that
method's own docstring for the full 5-step order). Witnesses ④/⑤ below
cover this pair (deny: the queued write does NOT survive into the fresh
segment; present, the sibling positive control: the SAME queued write
DOES land when ``clear_history`` is never called -- without the sibling,
the deny side could pass vacuously in a world where the queue was
already empty).

#6260 -- witness ⑥ covers the OTHER payer PR #6260's reviewer flagged as
having zero witness: ``CompactionController.force_compact_now``'s own
``if self._history_durability_flush is not None: await ...`` guard, and
``Session._build_chat_compaction_engine``'s ``history_durability_flush=
self.flush_history`` wiring that feeds it.
``test_4472_compaction_reads_durable_store.py``'s own ``_turn`` helper
flushes the worker itself right after every append (a #6261 workaround,
unrelated to this concern), so that file's compaction assertions stay
green even if ``force_compact_now``'s own flush call were deleted
entirely -- "passed to X" is not evidence X was ever USED. Witness ⑥
appends a turn with NO flush of its own, calls ``force_compact_now``
directly, and asserts the turn is on DISK afterward -- possible ONLY if
``force_compact_now`` genuinely awaited the flush wiring before its own
disk read.
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
    assert any(m.content == "not yet on disk" for m in session.history), (
        "the resident self.history append must still happen synchronously "
        "-- only the durable disk write is deferred"
    )


@pytest.mark.asyncio
async def test_worker_drain_makes_the_write_readable(tmp_path: Path) -> None:
    """Tier 2: witness ② (present) -- once
    :meth:`Session.flush_history` has been awaited, every
    write enqueued before it is genuinely on disk. Pairs with witness ①
    above (deny/present over the SAME call) so neither passes vacuously.

    Strip-falsify: temporarily replaced
    ``Session.flush_history``'s body with a bare ``return``
    (never awaiting the worker). Observed RED::

        AssertionError: after awaiting flush_history(), the
        line must be on disk -- got '' instead of content containing
        'now-on-disk'. If this failed, flush_history stopped
        actually awaiting DurabilityWorker.flush().

    Reverted immediately after observing (restored the ``await self.
    _history_durability_worker.flush()`` body); confirmed GREEN again."""
    session = _session(tmp_path)

    session._append_history(ChatMessage(role="user", content="now-on-disk", ts=_now()))
    await session.flush_history()

    on_disk = session.history_path.read_text() if session.history_path.exists() else ""
    assert "now-on-disk" in on_disk, (
        "after awaiting flush_history(), the line must be on "
        f"disk -- got {on_disk!r} instead of content containing "
        "'now-on-disk'. If this failed, flush_history stopped "
        "actually awaiting DurabilityWorker.flush()."
    )


@pytest.mark.asyncio
async def test_durable_active_history_after_sees_a_just_appended_line_once_flushed(
    tmp_path: Path,
) -> None:
    """Tier 2: witness ③ -- the real read-back reader
    (``Session._durable_active_history_after``, the durable-store read
    ``CompactionController.force_compact_now`` dispatches via
    ``asyncio.to_thread`` after awaiting ``flush_history``
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
    ``await session.flush_history()`` call below -- the SAME
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
    flush_history()`` call below); confirmed GREEN again."""
    session = _session(tmp_path)

    for text in ("first", "second", "third"):
        session._append_history(ChatMessage(role="user", content=text, ts=_now()))
    await session.flush_history()

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


@pytest.mark.asyncio
async def test_clear_history_flushes_a_queued_write_before_deleting_it(
    tmp_path: Path,
) -> None:
    """Tier 2: witness ④ (deny) -- a history write still QUEUED (not yet
    on disk) when ``Session.clear_history()`` runs must NOT survive into
    the fresh active segment ``clear_history`` opens. ``_append_history``
    is called with NO ``await`` before ``clear_history()`` -- the queued
    job is still pending (per asyncio's own cooperative scheduling, see
    module docstring) when ``clear_history``'s own step 0 flush runs,
    landing the write in the OLD (about-to-be-deleted) segment instead of
    the fresh one.

    Strip-falsify: temporarily moved the
    ``await self.flush_history()`` call from the TOP of
    ``Session.clear_history`` (session.py) to AFTER the fresh-segment
    reopen (the wrong-order defect this witness exists to catch --
    simply DELETING the call instead leaves the drain task with no
    ``await`` point in this coroutine's own body to ever run at, which
    would make this assertion pass vacuously regardless of ordering, not
    exercise the real hazard). Observed RED (verbatim, ``ts`` value
    elided as ``...``)::

        AssertionError: a write still queued when clear_history() ran
        must NOT survive into the fresh active segment -- got
        '{"role": "user", "content": "queued-before-clear", "ts": "...",
        "seq": 1, "meta": {"wal_seq": 0}, "tool_calls": null,
        "tool_call_id": null, "name": null, "spillability":
        "last_resort", "disclosure": null, "kind": "unspecified"}\\n'
        instead of ''. If this failed, clear_history() stopped flushing
        the history worker before deleting history_dir.
        assert '{"role": "us...specified"}\\n' == ''

    Reverted immediately after observing (restored the flush call at the
    top); confirmed GREEN again."""
    session = _session(tmp_path)

    session._append_history(ChatMessage(role="user", content="queued-before-clear", ts=_now()))
    await session.clear_history()

    assert session.history_path.exists(), (
        "clear_history() must have opened a fresh active segment -- without "
        "this, a clear_history() that silently never re-opened the segment "
        "would still pass the on_disk == '' assertion below vacuously "
        "(read_text() if exists() else '' returns '' for BOTH 'no file' and "
        "'file exists but empty')."
    )
    on_disk = session.history_path.read_text() if session.history_path.exists() else ""
    assert on_disk == "", (
        "a write still queued when clear_history() ran must NOT survive "
        f"into the fresh active segment -- got {on_disk!r} instead of ''. "
        "If this failed, clear_history() stopped flushing the history "
        "worker before deleting history_dir."
    )


@pytest.mark.asyncio
async def test_a_queued_write_lands_normally_without_a_clear(tmp_path: Path) -> None:
    """Tier 2: witness ⑤ (present) -- the sibling positive control for
    witness ④ above. The SAME queue-then-flush shape, but
    ``clear_history()`` is never called: the queued write DOES land, on
    the (still original) active segment. Without this sibling, witness
    ④'s deny assertion could pass vacuously in a world where the queue
    was already empty (e.g. a broken dispatch that never enqueues
    anything at all)."""
    session = _session(tmp_path)

    session._append_history(ChatMessage(role="user", content="queued-no-clear", ts=_now()))
    await session.flush_history()

    on_disk = session.history_path.read_text()
    assert "queued-no-clear" in on_disk, (
        "the SAME queued write must land normally when clear_history() is "
        f"never called -- got {on_disk!r} instead of content containing "
        "'queued-no-clear'"
    )


@pytest.mark.asyncio
async def test_compaction_flush_guard_actually_invokes_the_history_flush_wiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: witness ⑥ (#6260) -- the OTHER payer for the same-turn
    read-back ``_append_history`` used to guarantee synchronously:
    ``CompactionController.force_compact_now``'s own ``if self.
    _history_durability_flush is not None: await ...`` guard, fed by
    ``Session.__init__``'s own ``history_durability_flush=self.
    flush_history`` wiring into the ``CompactionController``
    it constructs. ``test_4472_compaction_reads_durable_store.py``'s own
    ``_turn`` helper flushes after EVERY append (a #6261 loop-rebind
    workaround, orthogonal to this concern), so that file's own
    compaction assertions would pass GREEN even with force_compact_now's
    flush call deleted entirely -- "passed to X" is not evidence X was
    ever USED.

    Asserts the wiring was CALLED (a spy on ``Session._flush_history_
    durability``, installed on the CLASS before construction so it
    becomes part of the SAME bound-method reference ``Session.__init__``
    captures into the controller -- never reaching into either object's
    already-constructed private state), not on the WRITE'S SIDE EFFECT
    reaching disk. A disk-content assertion was tried first and rejected:
    ``asyncio.to_thread`` (the disk read's own dispatch, immediately
    after the guard) is ITSELF an ``await`` that gives the queued write's
    background drainer a chance to catch up on its own, independent of
    whether the explicit guard ever ran -- confirmed empirically: with
    the guard's ``await`` replaced by ``pass``, the write still landed on
    disk before the read every time (the drainer consistently won that
    race, since the event loop typically services its own already-ready
    task before a fresh worker-thread pool submission gets scheduled).
    That makes a disk-content assertion here NON-deterministic evidence
    of the SPECIFIC guard -- watching the call directly is not.

    Strip-falsify: temporarily replaced the guard (``if self.
    _history_durability_flush is not None: await self._history_
    durability_flush()``, compaction_controller.py) with ``pass`` (never
    calling the wiring at all -- the wiring itself stays intact, matching
    this witness's own claim precisely). Observed RED::

        AssertionError: force_compact_now must actually CALL its own
        history_durability_flush wiring before its disk read -- it was
        never invoked.
        assert []

    Reverted immediately after observing (restored the guard); confirmed
    GREEN again."""
    import reyn.runtime.session as session_mod

    calls: "list[None]" = []
    original = session_mod.Session.flush_history

    async def _tracking_flush(self: Session) -> None:
        calls.append(None)
        await original(self)

    monkeypatch.setattr(session_mod.Session, "flush_history", _tracking_flush)

    session = _session(tmp_path)
    session._append_history(ChatMessage(role="user", content="queued-turn", ts=_now()))
    await session._compaction_controller.force_compact_now(
        spill_fn=lambda **kw: [], spill_capability_present=False,
    )

    assert calls, (
        "force_compact_now must actually CALL its own history_durability_"
        "flush wiring before its disk read -- it was never invoked."
    )
