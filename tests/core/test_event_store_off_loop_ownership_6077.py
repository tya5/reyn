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

Follow-up ruling (same #6077, same PR): the FIRST cut of the external-
replacement check ran once per WRITE — rejected (a per-write cost for a
per-BURST problem), and built on a premise architect corrected: POSIX does
NOT raise when writing through a handle whose file was unlinked (the
inode stays alive until the fd closes), so nothing about "catch the
exception" was ever going to work for a held-open handle in the first
place. The ruling instead checks once per DRAIN BURST, using a boundary
this codebase already has: ``DurabilityWorker._drain`` is self-terminating
(runs until its queue is EMPTY, then exits — one ``_drain()`` call IS one
burst). ``DurabilityWorker`` gained an OPTIONAL ``on_drain_start`` hook
(default ``None`` — WAL/snapshot workers, same class, pay nothing);
``EventStore`` is the only substrate that wires one. Cost now scales
inversely with load: one check per N writes during a burst (when a
per-write cost would matter), one check per write only when idle (when it
doesn't).

This file's 5 tests are the witnesses required, each observed via a REAL,
external fact — never a private call-count or attribute read:

1. Nothing lands on the real filesystem before the worker actually runs
   (``Path.exists()`` on the events dir, checked with no ``await`` between
   ``write()`` and the check).
2. ``Path.open`` in append mode (the actual OS-facing boundary a real
   ``open()`` syscall crosses) fires once for many writes, not once per
   write — the same interposition technique this suite's sibling file
   (``test_event_store_off_loop_write.py``) already uses for ``Path.stat``.
3. Deny/present pair for the drain-boundary staleness check itself
   (architect's own instruction: neither alone is sufficient — a "reopen
   every drain, unconditionally" implementation would pass deny vacuously
   without present catching it):
   - **deny**: after an EXTERNAL replacement, the next drain writes into a
     genuinely NEW file at the same path — the #6247-class hazard (a stale
     held-open handle succeeds SILENTLY at the OS level, writing into an
     orphaned, invisible inode) does not happen. Witnessed via
     existence + content (``path.exists()`` and what it contains), NOT
     ``st_ino`` equality — a CI-caught false-RED showed a freed inode
     number can be immediately reused by the very next created file on
     some filesystems (unlike #6247's own RENAME-based replacement, where
     the old inode stays alive so inequality was reliable; this scenario
     is DELETE-then-RECREATE, a different case the same instrument doesn't
     transfer to cleanly — see the test's own docstring for the full
     reasoning).
   - **present**: the store REUSES its held-open handle within a burst,
     never reopening per write. A SECOND CI-caught correction: this
     side's own first version (``Path.stat().st_ino`` equality across two
     SEPARATE drains) passed whether the handle was held OR reopened
     every write — reopening a still-existing path in append mode
     preserves its inode, so equality proved nothing. Witnessed instead
     via a mid-burst unlink (the file is deleted AFTER the burst's first
     write lands but BEFORE its second write runs, both inside the SAME
     drain — exploiting the drain-start check's own accepted per-burst
     blind spot): a REUSED handle keeps writing into the now-orphaned
     inode (``path.exists()`` stays ``False``); a wrongly-reopened-every-
     write implementation recreates the file for the second write
     (``path.exists()`` becomes ``True``) — see the test's own docstring.
4. Enqueue order == write order, unaffected by any of the above.

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
# Witness 3 — deny/present pair for the per-DRAIN-BURST staleness check
# (#6077 提案 6 follow-up, architect ruling)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deny_external_replacement_writes_through_a_new_inode_next_drain(tmp_path):
    """Tier 2: deny side of the required deny/present pair (see present
    sibling below) — holding a handle open across writes (#6077 提案 6) creates the SAME
    inode hazard #6247's own review flagged: on POSIX, a write through a
    handle whose file was deleted out from under it SUCCEEDS SILENTLY at
    the OS level while writing into an orphaned inode no reader of the
    path can ever see again — and (architect's own correction) POSIX never
    raises for this, so nothing about "catch the exception" could ever
    have caught it. The NEXT DRAIN BURST after an external deletion must
    verify (``EventStore._verify_active_handle_at_drain_start``, the
    worker's ``on_drain_start`` hook) and write into a GENUINELY NEW file
    at the same path — never the orphaned, unlinked one.

    The 2 writes below are deliberately in SEPARATE drain bursts
    (``flush()`` between them lets the drainer fully self-terminate) so
    the second write's OWN drain is a fresh ``_drain()`` call — the
    boundary the check runs at.

    Witness — deviates from the ``st_ino``-equality instrument architect's
    brief named (CI catch, reported to lead-coder, this docstring records
    the resolution): #6247 replaced a file via RENAME, where the OLD inode
    stays alive alongside the new one, so inode INEQUALITY reliably meant
    "two different, coexisting files." This scenario is DELETE-then-
    RECREATE instead — the old inode is freed, and a freed inode number can
    be immediately reused by the very next file the filesystem creates (an
    empty ``tmp_path`` on Linux/ext4/tmpfs makes this the LIKELY case, not
    a rare one — observed directly in CI: ``st_ino`` identical before/after
    while ``path.exists()`` and content were still both correct). So
    ``st_ino`` equality does NOT mean "same file" here — it means nothing
    either way. Nothing about that changes what an ACTUALLY BROKEN
    implementation looks like, though: a stale-handle write reaches no
    reader of ``path`` at all (verified locally: ``path.exists()`` is
    ``False`` and ``path.read_text()`` raises ``FileNotFoundError`` when the
    staleness check is disabled — see strip-falsify below), so the witness
    here is ``path.exists()`` + its CONTENT containing "after" — a fact
    with no inode-numbering ambiguity in either direction.

    Strip-falsify (in-file Edit only): changing ``EventStore.__init__`` to
    construct ``self._worker = DurabilityWorker()`` (dropping the
    ``on_drain_start=self._verify_active_handle_at_drain_start`` kwarg —
    i.e. disabling the staleness check entirely) turned this RED with::

        AssertionError: recovery must recreate the file at the same path --
        it does not exist. If this failed, the held-open handle kept
        writing into the deleted file's orphaned inode (invisible to any
        reader of the path) instead of detecting the deletion and
        reopening.

    (with the check disabled, ``_ensure_active_handle`` never sees
    ``self._active_fh`` cleared, so it never reopens at all -- the
    unlinked path simply never comes back.) Edited back (restoring the
    ``on_drain_start=...`` kwarg), confirmed GREEN again.
    """
    events_dir = tmp_path / "events"
    store = EventStore(events_dir)
    store.write(_ev("before"))
    await store.flush()  # drain 1 completes -- the drainer self-terminates
    path = store.active_path
    assert path is not None and path.exists()

    path.unlink()  # external deletion -- store's held-open handle is now stale

    store.write(_ev("after"))  # this write's own submit_nowait starts drain 2
    await store.aclose()

    assert path.exists(), (
        "recovery must recreate the file at the same path -- it does not exist. "
        "If this failed, the held-open handle kept writing into the deleted "
        "file's orphaned inode (invisible to any reader of the path) instead "
        "of detecting the deletion and reopening."
    )
    contents = path.read_text(encoding="utf-8")
    assert "after" in contents, (
        "the recreated file must contain the post-deletion event -- it doesn't. "
        f"If this failed, the write landed somewhere other than the visible "
        f"path. got: {contents!r}"
    )


@pytest.mark.asyncio
async def test_present_handle_is_reused_mid_burst_not_reopened_per_write(tmp_path, monkeypatch):
    """Tier 2: present side of the required deny/present pair (see deny
    sibling above). Architect's own instruction: neither test alone is
    sufficient — a "reopen every drain, unconditionally" implementation
    would pass the deny test above vacuously.

    #6251 review correction (recorded here — the FIRST version of this
    test used ``Path.stat().st_ino`` equality across TWO SEPARATE drains
    and did NOT witness what its own name claimed): reopening the SAME,
    still-existing path in append mode preserves its inode — ``st_ino``
    staying equal is true whether the store held the handle open OR
    reopened it fresh for every write. That test was GREEN under both
    shapes; it witnessed nothing. General form (architect): ``st_ino``
    only tells "same file" while TWO inodes are alive at once to compare
    (#6247's own ``rename`` — the old inode survives the swap); it can't
    tell "same file" from "a fresh open of a path that still resolves to
    the same file," which is exactly the shape "reopen every write" is.

    Distinguishing action instead (architect): unlink the active file
    MID-BURST — after the burst's FIRST write has already landed (using
    whatever handle state existed at drain-start) but BEFORE its SECOND
    write runs, both still inside the SAME ``_drain()`` call (both
    ``write()`` calls below have no ``await`` between them, so the
    drain-start hook — which only runs ONCE, at the top of a burst — has
    already passed by the time the unlink happens; this deliberately
    exploits the drain-start-only check's own accepted blind spot for a
    mid-burst change, the trade-off the per-burst design accepts, per
    architect's own "検知の窓は drain 1 回分に有界" ruling).

    - If the store REUSES its held-open handle for the burst's SECOND
      write (the property under test): that write goes through a handle
      whose file was just unlinked — succeeds at the OS level, but lands
      in the orphaned inode. ``path.exists()`` is ``False`` afterward.
    - If the store (wrongly) REOPENS for every write instead: the second
      write's own ``open(path, "a")`` auto-creates the file (the parent
      dir still exists) — ``path.exists()`` is ``True`` afterward.

    These two outcomes are genuinely different observable facts — unlike
    ``st_ino`` equality, which (per the correction above) can't tell them
    apart. Witness: ``path.exists()`` — never a private flag or call
    count. ``store._write_owned`` is wrapped only to control WHEN the
    unlink happens (mirrors this suite's own established
    ``test_slow_write_does_not_freeze_the_event_loop`` pattern of wrapping
    that same method for timing, not as the observation itself).

    Strip-falsify (in-file Edit only): changing ``_ensure_active_handle``
    to unconditionally reopen (removing the ``if self._active_fh is not
    None and not self._active_fh.closed: return`` reuse branch, always
    falling through to ``path.open("a", ...)``) turned this RED with::

        AssertionError: the SECOND write (same burst) must have gone
        through the REUSED handle, landing in the orphaned inode -- but
        path.exists() is True, meaning something reopened/recreated it.
        If this failed, EventStore stopped holding its active-file handle
        open within a single drain burst.

    Edited back (restoring the reuse branch), confirmed GREEN again.
    """
    events_dir = tmp_path / "events"
    store = EventStore(events_dir)
    store.write(_ev("open"))
    await store.flush()  # drain 1 completes -- opens + confirms the handle
    path = store.active_path
    assert path is not None and path.exists()

    orig_write_owned = store._write_owned
    call_count = {"n": 0}

    def _wrapped(data):
        call_count["n"] += 1
        result = orig_write_owned(data)
        if call_count["n"] == 1:
            path.unlink()  # mid-burst external deletion -- AFTER write 1, BEFORE write 2
        return result

    monkeypatch.setattr(store, "_write_owned", _wrapped)

    store.write(_ev("burst_a"))  # both enqueued before either runs -> ONE drain (one hook call)
    store.write(_ev("burst_b"))
    await store.aclose()

    assert not path.exists(), (
        "the SECOND write (same burst) must have gone through the REUSED "
        "handle, landing in the orphaned inode -- but path.exists() is True, "
        "meaning something reopened/recreated it. If this failed, EventStore "
        "stopped holding its active-file handle open within a single drain burst."
    )


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
