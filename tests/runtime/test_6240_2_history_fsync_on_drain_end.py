"""Tier 2: #6240 ⑵ (architect ruling, issue #6240 comments 5807595460 +
5808598931) -- ``history.jsonl``'s durable append handle is ``os.fsync``'d
once every time the history :class:`~reyn.core.events.durability_worker.
DurabilityWorker`'s queue drains to EMPTY, via the worker's ``on_drain_end``
hook (mirrored, at the other end of ``_drain()``, off ``EventStore``'s own
``on_drain_start``, #6077 提案 6 follow-up) -- never once per line, and
never gated on ``turn_settled`` (that signal MISSES the 5 ``_append_
history`` call sites that fire outside the turn loop: ``inter_agent_
messaging.py`` x4, ``intervention_handler.py`` x1 -- see the issue comment
above).

Why this boundary needs a durability fix at all: the 13 WAL-event kinds
(inbox / chain / intervention / next_turn_context) carry NO chat-turn
payload, so ``history.jsonl`` is chat history's ONE durable copy -- unlike
an audit-event, where per-line fsync was ruled excessive (#6077). No
truncate-falsify test accompanies this PR (CLAUDE.md's "recovery-feature
PRs need a truncate-falsify test" does not apply here): ``history/`` is not
DERIVED from the WAL -- it carries none of the WAL's 13 event kinds -- so
"truncate the WAL past X, reconstruct, assert X survives" has no X to name;
truncating the WAL can never remove or restore a chat turn either way.

Two witnesses, both observed via a REAL external fact -- the real
``os.fsync`` syscall, interposed (wrapped, never replaced -- forwards to
the genuine implementation) the SAME way ``test_event_store_off_loop_
ownership_6077.py`` interposes ``Path.open`` for its own per-burst-not-
per-write claim. This is not a collaborator fake (nothing about ``fh`` or
the worker is replaced; the write still lands for real): it is an
observation at the one real external boundary this hook's whole point is
to reach, exactly the class of witness architect asked for ("a REAL,
external fact -- never a private call-count or attribute read"):

  1. N appends inside ONE burst (no ``await`` between them) drain in a
     single ``_drain()`` call and reach ``os.fsync`` exactly ONCE for that
     handle's fd -- never once per line.
  2. The SAME hook fires on the OTHER exit ``_drain()`` has -- the
     inline-drain branch :meth:`DurabilityWorker._drain_to_empty` takes
     when the background drainer already died (#6260) -- reproduced with
     the SAME "no ``await`` between creating the drainer task and
     cancelling it" determinism ``tests/core/test_1765_durability_worker.
     py``'s own ``test_flush_drains_inline_when_the_drainer_was_cancelled_
     before_it_ever_ran`` uses, so this is not a race depended on to
     resolve a particular way.

A third, structural witness (no execution, per architect's own "fall back
to structure, not behaviour" instruction -- ``os.fsync``'s call COUNT
against a real fd is the strongest witness available and is exercised
above; but "fsync happens once per WRITE, not once per BURST" is a defect
this file can only prove absent by proving PRESENT the two behaviours
above, so this third witness instead pins the two-sided structural
invariant those behaviours rest on): the write path itself
(``Session._write_history_record_owned`` / ``Session._do_write_history_
record``, the code that runs once PER APPEND) contains no ``fsync`` call at
all -- if it did, EVERY append would fsync regardless of what the drain-end
hook does, defeating the whole point of moving this off the per-line path.

Real ``Session`` (``tests._support.agent_session.make_session``, the same
construction path ``test_6240_3_append_history_durability_worker.py`` uses)
+ real ``ChatMessage`` + a real filesystem (``tmp_path``) throughout -- no
``MagicMock``/``AsyncMock``/``patch``. No ``sleep`` anywhere: witness ①
appends with no ``await`` between calls (asyncio's own cooperative
scheduling guarantees none of them can have drained yet) then awaits the
worker's own ``flush()``; witness ② cancels the drainer task with no
``await`` between its creation and the cancel (same determinism
``test_1765_durability_worker.py`` already relies on) then awaits ``flush()``.

PR #6262 review (BLOCKING) caught a hole in the FIRST cut of this design:
``on_drain_end`` was called directly from the ``QueueEmpty`` branch, AFTER
every real item's own ``task_done()`` -- so ``DurabilityWorker.flush()``/
``aclose()`` (both ``await queue.join()``) could return BEFORE the hook's
own ``await`` even started, let alone finished. CI caught this exact gap:
witness ① (this file, head ``8d74b7522``) failed non-deterministically
("got none") on the SAME leg the reviewer's structural argument predicted.
The fix (architect ruling, same PR): ``on_drain_end`` is no longer called
from a post-loop branch at all -- it is ENQUEUED as an ordinary queue item
on the burst's first empty check, so ``join()`` naturally waits for it
(see ``DurabilityWorker._drain``'s own docstring for the full mechanism,
and its module docstring for why ``on_drain_start`` was deliberately left
untouched). Witnesses ⑤/⑥ below are the deny/present pair architect asked
for at the MECHANISM level (queue accounting, not ``os.fsync`` itself) --
distinct from witnesses ①/② above, which exercise the same property one
layer up, through the real fsync syscall.

A SEPARATE hole in the same first cut, also fixed in this PR: ``fh.
fileno()`` used to be read on the calling coroutine BEFORE ``asyncio.
to_thread`` dispatched -- so a handle closed WHILE the fsync ran off-loop
(a #6248 seal, or ``clear_history``) could have its fd number reused by a
freshly-opened file before the thread's own ``os.fsync`` call actually
ran, silently syncing the WRONG file. Moving the ``fileno()`` call INSIDE
the thread body (session.py) does not close that race window -- it turns
a closed handle into a loud ``ValueError`` instead of a silent wrong-file
sync. Not independently witnessed here (no test forces that exact
sub-window); recorded as a structural fix per architect's own "the window
does not disappear, only the silence does" instruction.

Strip-falsify performed in-file via Edit -> observe RED -> Edit back
(``git checkout``/``stash``/``restore`` never used, CLAUDE.md) for every
witness; RED text recorded verbatim in each test's own docstring."""
from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reyn.core.events.durability_worker import DurabilityWorker
from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session(tmp_path: Path, *, name: str = "history-fsync-test") -> Session:
    return make_session(
        agent_name=name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )


def _wrap_fsync(monkeypatch: pytest.MonkeyPatch) -> "list[int]":
    """Interpose the REAL ``os.fsync`` -- forwards to the genuine syscall,
    records every fd it was called with. Same technique ``test_event_
    store_off_loop_ownership_6077.py`` uses for ``Path.open`` (an
    observation at a real external boundary, never a private counter)."""
    orig_fsync = os.fsync
    calls: "list[int]" = []

    def _tracking_fsync(fd: int) -> None:
        calls.append(fd)
        orig_fsync(fd)

    monkeypatch.setattr(os, "fsync", _tracking_fsync)
    return calls


@pytest.mark.asyncio
async def test_fsync_fires_once_per_drain_burst_not_once_per_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: witness ① -- 4 appends enqueued back-to-back (no ``await``
    between them, so none can have drained independently -- deterministic
    per asyncio's own cooperative scheduling) drain in ONE ``_drain()``
    burst once flushed, and ``os.fsync`` fires exactly ONCE against the
    session's history append handle's fd for that whole burst -- not 4
    times.

    Strip-falsify: temporarily changed ``Session.__init__`` (session.py)
    to construct ``self._history_durability_worker = DurabilityWorker()``
    (dropping the ``on_drain_end=...`` kwarg -- disabling the fsync hook
    entirely). Observed RED::

        AssertionError: expected an fsync() call against the history
        handle's fd for a 4-message burst -- got none. If this failed,
        the drain-end fsync hook is not wired.
        assert []

    Reverted immediately after observing (restored the ``on_drain_end=
    self._fsync_history_append_handle_on_drain_end`` kwarg); confirmed
    GREEN again."""
    calls = _wrap_fsync(monkeypatch)
    session = _session(tmp_path)

    for i in range(4):
        session._append_history(ChatMessage(role="user", content=f"line-{i}", ts=_now()))
    await session.flush_history()

    on_disk = session.history_path.read_text(encoding="utf-8")
    assert all(f"line-{i}" in on_disk for i in range(4)), (
        "sanity: all 4 lines must genuinely be on disk before checking the "
        f"fsync count -- got {on_disk!r}"
    )

    fh = session._history_append_fh
    assert fh is not None, "sanity: the append handle must still be open after the burst"
    expected_fd = fh.fileno()
    matching = [fd for fd in calls if fd == expected_fd]
    assert matching, (
        "expected an fsync() call against the history handle's fd for a "
        "4-message burst -- got none. If this failed, the drain-end fsync "
        "hook is not wired."
    )
    assert not matching[1:], (
        "expected exactly 1 fsync() call against the history handle's fd "
        "for a 4-message burst (once per DRAIN, not once per append) -- "
        f"got {matching!r}. If this failed, the drain-end fsync hook fired "
        "more than once for a single burst."
    )


@pytest.mark.asyncio
async def test_fsync_fires_on_the_inline_drain_path_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: witness ② -- the SAME fsync hook fires on ``DurabilityWorker
    ._drain_to_empty``'s OTHER exit: the inline-drain branch ``flush()``
    takes when the background drainer already died (#6260) -- e.g. an
    external hard-cancel (``AgentRegistry.shutdown``, loop teardown on
    ``/quit``/Ctrl-C) landing before the drainer ever got to run. Both of
    ``_drain()``'s callers bottom out in the SAME body (see ``Durability
    Worker._drain``'s own docstring), so wiring the hook there (never on a
    ``Task.add_done_callback``, which the inline branch never creates) is
    the one place that reaches both -- this test is the witness that it
    genuinely does.

    Reproduces ``test_1765_durability_worker.py``'s own ``test_flush_
    drains_inline_when_the_drainer_was_cancelled_before_it_ever_ran``
    determinism: the drainer task is cancelled with NO ``await`` between
    its creation (``_append_history``'s own ``submit_nowait`` -> ``_kick``)
    and the ``.cancel()`` call, so asyncio's cooperative scheduling
    guarantees deterministically that none of its body has run yet --
    ``flush()`` is then forced onto ``_drain_to_empty``'s inline branch,
    which calls ``await self._drain()`` directly (no background task).

    Strip-falsify: temporarily changed ``Session.__init__`` (session.py)
    to construct ``self._history_durability_worker = DurabilityWorker()``
    (dropping the ``on_drain_end=...`` kwarg). Observed RED::

        AssertionError: expected an fsync() call against the history
        handle's fd via the INLINE drain path -- got none. If this
        failed, the hook never reaches the inline-drain exit, or is not
        wired at all.
        assert []

    Reverted immediately after observing (restored the ``on_drain_end=
    self._fsync_history_append_handle_on_drain_end`` kwarg); confirmed
    GREEN again."""
    calls = _wrap_fsync(monkeypatch)
    session = _session(tmp_path)

    before = asyncio.all_tasks()
    session._append_history(ChatMessage(role="user", content="inline-drain-line", ts=_now()))
    new_tasks = asyncio.all_tasks() - before
    assert new_tasks, "at least one drainer task must have been created by _kick()"
    drainer = new_tasks.pop()
    assert not new_tasks, "_kick() must not have created more than one drainer task"
    drainer.cancel()  # external hard-cancel -- before it ever ran

    await session.flush_history()

    on_disk = session.history_path.read_text(encoding="utf-8")
    assert "inline-drain-line" in on_disk, (
        "sanity: the inline drain must still have written the line for "
        f"real -- got {on_disk!r}"
    )

    fh = session._history_append_fh
    assert fh is not None, "sanity: the append handle must still be open after the inline drain"
    expected_fd = fh.fileno()
    matching = [fd for fd in calls if fd == expected_fd]
    assert matching, (
        "expected an fsync() call against the history handle's fd via the "
        "INLINE drain path -- got none. If this failed, the hook never "
        "reaches the inline-drain exit, or is not wired at all."
    )
    assert not matching[1:], (
        "expected exactly 1 fsync() call against the history handle's fd "
        f"via the INLINE drain path -- got {matching!r}."
    )


def test_the_per_append_write_path_itself_never_calls_fsync() -> None:
    """Tier 2: witness ③ (structural) -- the code that runs once PER
    APPEND (``Session._write_history_record_owned``, and the off-loop
    dispatcher wrapping it, ``Session._do_write_history_record``) contains
    no ``fsync`` call anywhere in its own source. If it did, every single
    append would fsync regardless of the drain-end hook's own once-per-
    burst behaviour (witnesses ①/② above) -- defeating the entire point of
    moving this off the per-line path (mirrors the per-line audit-event
    fsync #6077 ruled excessive).

    Strip-falsify: temporarily added
    ``os.fsync(f.fileno())  # STRIP-FALSIFY TEMP -- must not be here``
    right after ``f.flush()`` in ``Session._write_history_record_owned``
    (session.py). Observed RED (verbatim assertion message; the method's
    full source dump that follows it in the real output is elided here
    as ``...``)::

        AssertionError: Session._write_history_record_owned (the
        PER-APPEND write path) must not call fsync directly -- fsync
        belongs ONLY on the drain-end hook (once per BURST). Source:
        ...os.fsync(f.fileno())  # STRIP-FALSIFY TEMP -- must not be here...
        assert 'fsync' not in '    def _wr...ary = True\\n'

    Reverted immediately after observing (removed the added line);
    confirmed GREEN again."""
    for method in (Session._write_history_record_owned, Session._do_write_history_record):
        source = inspect.getsource(method)
        assert "fsync" not in source, (
            f"{method.__qualname__} (the PER-APPEND write path) must not "
            "call fsync directly -- fsync belongs ONLY on the drain-end "
            f"hook (once per BURST). Source:\n{source}"
        )


def test_the_drain_end_hook_itself_calls_fsync() -> None:
    """Tier 2: witness ④ (structural, the positive sibling to witness ③'s
    deny) -- ``Session._fsync_history_append_handle_on_drain_end`` (the
    callable wired as the history worker's ``on_drain_end``) genuinely
    calls ``os.fsync`` in its own source. Without this sibling, witness
    ③'s deny could pass vacuously in a world where fsync was removed from
    EVERYWHERE, not just moved off the per-append path.

    Strip-falsify: temporarily changed this method's own
    ``await asyncio.to_thread(os.fsync, fh.fileno())`` to
    ``await asyncio.to_thread(fh.flush)`` (session.py -- dropping the
    fsync, keeping a flush so the change reads as plausible, not a
    no-op). Observed RED::

        AssertionError: Session._fsync_history_append_handle_on_drain_end
        must itself call os.fsync -- source:
        ...await asyncio.to_thread(fh.flush)  # STRIP-FALSIFY TEMP -- no fsync
        assert 'os.fsync' in '    async def _fsync_hist...no fsync\\n'

    Reverted immediately after observing (restored the ``os.fsync,
    fh.fileno()`` call); confirmed GREEN again."""
    source = inspect.getsource(Session._fsync_history_append_handle_on_drain_end)
    assert "os.fsync" in source, (
        "Session._fsync_history_append_handle_on_drain_end must itself "
        f"call os.fsync -- source:\n{source}"
    )


@pytest.mark.asyncio
async def test_flush_waits_for_the_enqueued_end_of_burst_job_before_returning() -> None:
    """Tier 2: witness ⑤ (deny, PR #6262 review's mechanism-level pair) --
    ``DurabilityWorker.flush()`` (``await queue.join()``) does not return
    until an ``on_drain_end`` job it enqueued has ITSELF completed. This
    is the property the FIRST cut of #6240 ⑵ lacked (``on_drain_end`` was
    called from a post-loop branch OUTSIDE ``join()``'s own accounting) --
    CI caught the resulting non-determinism directly (witness ① above,
    head ``8d74b7522``, "got none").

    ``on_drain_end`` here awaits a real scheduling point (``asyncio.
    sleep(0)`` -- the documented, deterministic single-iteration yield
    several other tests in this repo already rely on, e.g. ``tests/core/
    test_await_quiescent.py``; NOT a duration -- no elapsed time is
    asserted on) before flipping a flag, so "the flag is set" can only be
    true if the drainer's own event loop actually let that coroutine run
    to COMPLETION -- not merely that it was scheduled.

    Strip-falsify: this test ALSO catches a SECOND, more subtle race the
    module's own docstring records -- temporarily removed the pre-emptive
    "enqueue the end-of-burst job before THIS item's own task_done()"
    check in ``DurabilityWorker._drain`` (durability_worker.py), leaving
    only the ``QueueEmpty``-branch enqueue. That shape still enqueues the
    job eventually, but ONE iteration too late: the LAST real item's own
    ``task_done()`` already drops ``unfinished_tasks`` to zero (releasing
    any waiting ``flush()``) BEFORE the end-of-burst job is put back on
    the queue. Observed RED (this test caught it locally BEFORE it could
    reach CI -- unlike the FIRST-cut gap, which CI caught directly)::

        AssertionError: on_drain_end must have COMPLETED by the time
        flush() returns -- got False.
        assert False

    Reverted immediately after observing (restored the pre-emptive
    enqueue check); confirmed GREEN again -- repeatedly (stress-run 25x
    locally with no failure, since the property is now structural, not
    timing-dependent, unlike either broken shape)."""
    done = False

    async def _on_drain_end() -> None:
        nonlocal done
        await asyncio.sleep(0)
        done = True

    async def _noop() -> None:
        return None

    w = DurabilityWorker(on_drain_end=_on_drain_end)
    w.submit_nowait(_noop)
    await w.flush()

    assert done, (
        f"on_drain_end must have COMPLETED by the time flush() returns -- got {done}"
    )
    await w.aclose()


@pytest.mark.asyncio
async def test_flush_returns_normally_with_no_on_drain_end_configured() -> None:
    """Tier 2: witness ⑥ (present, the sibling architect asked for) --
    the SAME shape (submit, then flush()) on a worker with NO
    ``on_drain_end`` at all (WAL/snapshot/audit/media's own shape -- the
    default) returns normally, with the submitted write's own effect
    visible. Without this sibling, witness ⑤'s deny could pass vacuously
    in a world where ``flush()`` was changed to unconditionally wait on
    SOMETHING regardless of whether ``on_drain_end`` is even configured --
    this rules that out."""
    written: "list[str]" = []

    async def _write() -> None:
        written.append("done")

    w = DurabilityWorker()  # no on_drain_end -- the other 3 substrates' own shape
    w.submit_nowait(_write)
    await w.flush()

    assert written == ["done"], (
        "flush() must still return normally and drain the write with no "
        f"on_drain_end configured at all -- got {written!r}"
    )
    await w.aclose()
