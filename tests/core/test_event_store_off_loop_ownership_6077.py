"""Tier 2: EventStore off-loop write OWNERSHIP (#6077 提案 3 + 提案 6,
architect ruling).

Two prior fixes each moved PART of ``EventStore.write()``'s hot path off
the event loop: #2780 moved the blocking ``open``/``write``/``fsync``
syscalls; this issue's own architect review found that ``json.dumps`` (the
CPU-bound serialization) and the rotation accounting it feeds were STILL
running synchronously on the loop, plus ``_open_new_file``'s own
``mkdir``/``touch``/purge-trigger — all fired on the hot path (every audit
event; rotation is ON by default, 10MB/1day). Separately, a 95,030-event
workdir turned out to mean 95,030 individual ``open``/``write``/``fsync``
round-trips through the worker — the SAME class of defect #6247 fixed for
``history.jsonl`` (open/close per message hooks Windows real-time AV), one
order of magnitude up.

Architect's ruling (both proposals, same PR — they touch the exact same
ownership question): everything that decides "does the active file need
rotating / creating / recovering, and what are this line's bytes" now
lives in ONE place, off-loop (``EventStore._write_owned`` — see its own
docstring), including a session-lifetime file handle (open once, reused,
closed/reopened only on rotation or on detecting the file was deleted/
replaced out from under it — the SAME inode hazard #6247's own review
flagged for THIS store's held-open handle). ``fsync`` stays exactly once
per event; durability is UNCHANGED by either proposal.

This file's 3 tests are the witnesses the architect's brief required, each
observed via a REAL, external fact — never a private call-count or
attribute read:

1. Nothing lands on the real filesystem before the worker actually runs
   (``Path.exists()`` on the events dir, checked with no ``await`` between
   ``write()`` and the check).
2. ``Path.open`` in append mode (the actual OS-facing boundary a real
   ``open()`` syscall crosses) fires once for many writes, not once per
   write — the same interposition technique this suite's sibling file
   (``test_event_store_off_loop_write.py``) already uses for ``Path.stat``.
3. Recovery after an external deletion lands on a NEW, real inode
   (``Path.stat().st_ino``) — the #6247-class hazard: a stale held-open
   handle would keep "succeeding" at the OS level while writing into an
   orphaned, invisible inode.

Real ``EventStore``/``DurabilityWorker`` instances, real filesystem
(``tmp_path``), no mocks of collaborators — the ``Path.open``/``Path.stat``
interception below wraps the REAL implementation (never replaces it),
matching this suite's sibling file's own established pattern.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.event_store import EventStore
from reyn.schemas.models import Event


def _ev(kind: str = "test_event", **data) -> Event:
    return Event(type=kind, data=data)


# ---------------------------------------------------------------------------
# Witness 1 — nothing on disk before the worker runs (#6077 提案 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_lands_on_disk_before_the_worker_drains_the_write(tmp_path):
    """Tier 2: ``write()``'s own synchronous tick does ONLY ``model_dump`` +
    enqueue — ``json.dumps``, ``mkdir``, ``touch``, and the actual write all
    happen inside the worker's off-loop job, never before it runs.

    Witness: checked with NO ``await`` between ``write()`` returning and the
    filesystem check — if ANY of those 4 operations still ran synchronously,
    the events directory (or a ``.jsonl`` file under it) would already exist
    at that point. After ``await flush()`` (which drains the worker), the
    file exists with the event's content.

    Strip-falsify (in-file Edit only, ``git checkout``/``stash``/``restore``
    never used): changing ``EventStore.write()`` to call
    ``self._write_owned(data)`` directly, unconditionally (bypassing the
    ``asyncio.get_running_loop()`` check + ``submit_nowait``, i.e.
    reintroducing a synchronous write even with a loop running), turned
    this RED with::

        AssertionError: mkdir/touch/dumps must not have happened yet -- the
        events dir already exists immediately after write() returned with
        no await. If this failed, EventStore.write() started doing
        filesystem work synchronously again instead of only model_dump +
        enqueue.

    Edited back (restoring the ``asyncio.get_running_loop()`` guard +
    ``submit_nowait`` call), confirmed GREEN again.
    """
    events_dir = tmp_path / "events"
    store = EventStore(events_dir)

    store.write(_ev("first"))
    assert not events_dir.exists(), (
        "mkdir/touch/dumps must not have happened yet -- the events dir "
        "already exists immediately after write() returned with no await. "
        "If this failed, EventStore.write() started doing filesystem work "
        "synchronously again instead of only model_dump + enqueue."
    )

    await store.flush()
    files = list(events_dir.rglob("*.jsonl"))
    assert files and not files[1:], f"expected exactly 1 file after flush, got {files}"
    assert "first" in files[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Witness 2 — open() collapses to ~1 across many writes (#6077 提案 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_mode_open_fires_once_for_many_writes(tmp_path, monkeypatch):
    """Tier 2: a session-lifetime handle means N audit-events -> 1 real
    ``open()`` call, not N (#6077 提案 6, architect ruling — same class of
    fix as #6247's ``history.jsonl`` handle, one order of magnitude up:
    this workdir alone carries 95,030 audit-events).

    Witness: intercepts the real ``Path.open`` call in append ("a") mode —
    the actual OS-facing boundary a genuine per-write ``open()`` syscall
    would cross — and counts calls. This is the SAME interposition
    technique ``test_event_store_off_loop_write.py``'s own
    ``test_rotation_size_check_never_stats_for_st_size`` already uses for
    ``Path.stat``: an observation at a real external boundary, never a
    private counter on the store itself.

    Expected count is 2, not 1: the events dir doesn't exist yet for the
    VERY FIRST write, so ``_ensure_active_handle``'s first ``path.open("a")``
    raises ``FileNotFoundError`` (caught, recovered via ``mkdir`` + a SECOND
    ``open("a")`` that succeeds) -- both calls are counted (both are real
    append-mode ``open()`` attempts), giving 2 total for the whole 20-write
    burst rather than 20. That "2, not 20" gap is the property under test.

    Strip-falsify (in-file Edit only): changing ``_ensure_active_handle``
    to unconditionally reopen (removing the
    ``if self._active_fh is not None and not self._active_fh.closed:
    ... return`` reuse branch, always falling through to
    ``path.open("a", ...)``) turned this RED with::

        AssertionError: expected at most 2 append-mode open() calls for 20
        writes with no rotation (a session-lifetime handle, #6077 提案 6:
        1 initial FileNotFoundError-recovery pair, then reuse) -- got 21.
        If this failed, EventStore stopped holding its active-file handle
        open across writes.

    Edited back (restoring the reuse branch), confirmed GREEN again.
    """
    orig_open = Path.open
    open_calls: list[Path] = []

    def _tracking_open(self, mode="r", *a, **kw):
        if mode == "a":
            open_calls.append(self)
        return orig_open(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", _tracking_open)

    events_dir = tmp_path / "events"
    store = EventStore(events_dir)  # default max_bytes=0 here -> single-run (no rotation)
    for i in range(20):
        store.write(_ev("burst", i=i))
    await store.aclose()

    assert not open_calls[2:], (
        "expected at most 2 append-mode open() calls for 20 writes with no "
        "rotation (a session-lifetime handle, #6077 提案 6: 1 initial "
        f"FileNotFoundError-recovery pair, then reuse) -- got "
        f"{len(open_calls)}. If this failed, EventStore stopped holding "
        "its active-file handle open across writes."
    )


# ---------------------------------------------------------------------------
# Witness 3 — recovery lands on a NEW inode, never a stale one (#6247-class)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_after_external_deletion_writes_through_a_new_inode(tmp_path):
    """Tier 2: holding a handle open across writes (#6077 提案 6) creates
    the SAME inode hazard #6247's own review flagged: on POSIX, a write
    through a handle whose file was deleted out from under it SUCCEEDS
    SILENTLY at the OS level while writing into an orphaned inode no
    reader of the path can ever see again. The next write after an
    external deletion must land on a NEW, real inode at the same path --
    not vanish into the old one.

    Witness: ``Path.stat().st_ino`` before/after (the exact technique
    #6247's own review named, and the one architect's brief for THIS
    change cited directly) -- never a private flag.

    Strip-falsify (in-file Edit only): changing ``_ensure_active_handle``
    to skip the inode-identity check entirely (``_handle_matches_path``
    call removed, always reusing ``self._active_fh`` when it is open and
    not closed) turned this RED with::

        AssertionError: recovery must recreate the file at the same path --
        it does not exist. If this failed, the held-open handle kept
        writing into the deleted file's orphaned inode (invisible to any
        reader of the path) instead of detecting the deletion and
        reopening.

    Edited back (restoring the ``_handle_matches_path`` check), confirmed
    GREEN again.
    """
    events_dir = tmp_path / "events"
    store = EventStore(events_dir)
    store.write(_ev("before"))
    await store.flush()
    path = store.active_path
    assert path is not None and path.exists()
    before_ino = path.stat().st_ino

    path.unlink()  # external deletion -- store's held-open handle is now stale

    store.write(_ev("after"))
    await store.aclose()

    assert path.exists(), (
        "recovery must recreate the file at the same path -- it does not exist. "
        "If this failed, the held-open handle kept writing into the deleted "
        "file's orphaned inode (invisible to any reader of the path) instead "
        "of detecting the deletion and reopening."
    )
    after_ino = path.stat().st_ino
    assert after_ino != before_ino, (
        "recovery must write through a NEW inode, not the old unlinked one "
        f"-- inode unchanged at {before_ino}."
    )
    assert "after" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Witness 4 — enqueue order == write order (FIFO contract preserved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_order_equals_write_order_with_the_new_off_loop_owner(tmp_path):
    """Tier 2: #6077 restructured WHAT ``write()`` enqueues (a captured
    ``model_dump`` dict, not a pre-serialized line) and WHAT the worker job
    does (rotation/handle-ownership + ``dumps`` + write, not just a bare
    write) -- this test pins the ONE property that must survive that
    restructuring unchanged: enqueue order (``write()``'s own call order,
    synchronous, no ``await`` between calls) still equals on-disk order,
    because ``submit_nowait`` is still called synchronously per ``write()``
    call and the worker's own drain loop is still strictly FIFO."""
    events_dir = tmp_path / "events"
    store = EventStore(events_dir)
    for i in range(30):
        store.write(_ev("seq", i=i))
    await store.aclose()

    path = store.active_path
    assert path is not None
    import json
    lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    on_disk = [e["data"]["i"] for e in lines]
    assert on_disk == list(range(30))
