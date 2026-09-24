"""Tier 2: #6077 提案 1 -- ``Session._append_history`` now writes through a
SESSION-LIFETIME ``history.jsonl`` append handle (opened once, lazily, and
reused across every append) instead of a fresh ``open``/``write``/``close``
per message. Real-time AV on the owner's Windows machine hooks file OPEN,
so 1 ``open`` per message meant 1 AV scan per message against a file
measured at 547 MB (#6240) -- this is the fix.

The property this file pins: **``flush()`` after every write is what keeps
the synchronous "appended => durably readable" contract intact.** Before
this change, ``close()`` (implicit at the end of the per-call ``with``
block) was what made a just-written line visible to a reader opening
``history.jsonl`` fresh -- and real production consumers do exactly that,
same turn, no wait: ``Session._durable_active_history_after`` (this test's
own seam, shared with ``tests/runtime/test_4472_compaction_reads_durable_
store.py``) and ``CompactionController.force_compact_now``'s own read path.
Holding the handle open across the WHOLE session lifetime means ``close()``
no longer fires per message -- so an explicit ``flush()`` is the only thing
standing between "written" and "durably readable this turn" now. This is
exactly the property CLAUDE.md's brief for this PR named as the most
fragile one to preserve.

Real ``Session`` (via ``tests._support.agent_session.make_session``, the
same construction path ``test_4472_compaction_reads_durable_store.py`` and
``test_4468_narrowing_survives_eviction.py`` use) + real ``ChatMessage`` +
a real filesystem (``tmp_path``) throughout -- no ``MagicMock``/``patch``.
No ``sleep`` anywhere: the assertion is same-turn, not time-based.

Strip-falsify (performed in-file via Edit -> observe RED -> Edit back,
``git checkout``/``stash``/``restore`` never used, CLAUDE.md): removing
the ``f.flush()`` call in ``Session._append_history`` (session.py) turned
``test_appended_lines_are_immediately_visible_through_durable_active_
history_after`` RED with::

    AssertionError: a just-appended line must be visible via
    _durable_active_history_after in THE SAME TURN, with no wait -- got
    [] instead of ['first', 'second', 'third']. If this failed, flush()
    was removed from Session._append_history (session.py) -- close() no
    longer fires per message now that the append handle is held open for
    the session's whole lifetime, so flush() is the ONLY thing that still
    makes a just-written line durably readable this turn.

restored by re-adding ``f.flush()`` (Edit), confirmed GREEN again.

#6240 ③ update (architect ruling, issue #6240 comment 5807710323): the
synchronous "appended => durably readable, no wait" contract this file
pins is now ONLY guaranteed when no event loop is running at append time
(this file's own test functions are plain ``def``, so that is exactly
the path they exercise — ``Session._append_history`` falls back to
writing inline). When a loop IS running (every real chat turn), the disk
write is enqueued on a ``DurabilityWorker`` instead
(``submit_nowait`` — fire-and-forget) and this file's own ``f.flush()``
call moved into the worker's own off-loop job
(``Session._write_history_record_owned``). The payer for same-turn
read-back moved from every WRITER to the one reader that reads disk
(``Session._flush_history_durability``, awaited by
``CompactionController.force_compact_now`` immediately before its own
disk read) — see ``tests/runtime/test_6240_3_append_history_durability_
worker.py`` for the witnesses covering the loop-running path this file
does not exercise.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session(tmp_path: Path) -> Session:
    return make_session(
        agent_name="history-append-handle-test",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )


def test_appended_lines_are_immediately_visible_through_durable_active_history_after(
    tmp_path,
):
    """Tier 2: see module docstring. Three appends through the SAME
    session-lifetime handle (exercising reuse across calls, not merely a
    first-open) must all be durably readable via
    ``_durable_active_history_after`` in the same turn -- no wait, no
    reopen of the session, no sleep."""
    session = _session(tmp_path)

    for text in ("first", "second", "third"):
        session._append_history(ChatMessage(role="user", content=text, ts=_now()))

    durable_turns, truncated = session._durable_active_history_after(0)
    contents = [m.content for m in durable_turns]
    assert contents == ["first", "second", "third"], (
        "a just-appended line must be visible via "
        "_durable_active_history_after in THE SAME TURN, with no wait -- "
        f"got {contents!r} instead of ['first', 'second', 'third']. If "
        "this failed, flush() was removed from Session._append_history "
        "(session.py) -- close() no longer fires per message now that "
        "the append handle is held open for the session's whole "
        "lifetime, so flush() is the ONLY thing that still makes a "
        "just-written line durably readable this turn."
    )
    assert truncated is False, (
        "sanity: 3 short turns must never trip the batch-read truncation "
        "flag -- a True here would mean the read itself, not the flush "
        "contract, is what this assertion is (accidentally) exercising"
    )
