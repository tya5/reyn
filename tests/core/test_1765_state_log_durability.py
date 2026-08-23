"""Tier 2: #1765 Step 1a/1b — StateLog routed through the DurabilityWorker (off-loop WAL write).

The refactor is BEHAVIOR-INVARIANT for durability + recovery (moving the write/fsync off the
loop is the only change): an append returns only once durable, recovery replays every durable
entry, and the `_durable_seq` watermark closes the #1751 lockless-read surface (a
written-but-not-yet-durable tail entry is structurally unreadable). Real instances (no mocks).
"""
from __future__ import annotations

import json

import pytest

from reyn.core.events.state_log import StateLog


@pytest.mark.asyncio
async def test_append_is_durable_then_recovery_replays_all(tmp_path):
    """Tier 2: completeness + recovery. Each append returns only after its entry is durable
    (per-append contract unchanged); after a simulated restart a FRESH StateLog on the same
    file replays EVERY entry. RED if the watermark didn't init from disk (recovery would see
    an empty log) or appends didn't actually persist."""
    p = tmp_path / "wal.jsonl"
    sl = StateLog(p)
    s1 = await sl.append("inbox_put", text="a")
    s2 = await sl.append("inbox_consume", msg_id="m")
    assert (s1, s2) == (1, 2)
    # durable on disk the instant append returned (the per-append contract):
    on_disk = [json.loads(ln)["seq"] for ln in p.read_text().splitlines() if ln.strip()]
    assert on_disk == [1, 2]
    await sl.aclose()
    # restart: a fresh StateLog inits the durable watermark from the on-disk max, so a
    # full replay returns every entry (the recovery behaviour — the public proof).
    recovered = StateLog(p)
    assert [e["seq"] for e in recovered.iter_from(1)] == [1, 2]


@pytest.mark.asyncio
async def test_iter_from_excludes_inflight_entry_during_fsync(tmp_path, monkeypatch):
    """Tier 2: the #1751 closure (the REAL concurrent scenario). While an append's entry is
    written-but-still-fsyncing (off-loop), a CONCURRENT iter_from must NOT expose it — it is
    not yet durable. After the fsync completes it becomes readable. RED if the `_inflight_seq`
    skip is removed (the concurrent read would see the non-durable entry — the surface that
    reverted #1751).

    #5159 (census finding): this used to fix the in-flight window open with a plain
    `time.sleep(0.05)` inside `_slow_fsync` and race it against `await asyncio.sleep(0.01)`
    on the test's own side — a CLOCK stand-in for the real condition ("has seq 2's write
    actually reached fsync yet"), competing against a DIFFERENT clock (how long the fsync
    itself takes) with no ordering guarantee between them: a loaded machine could let the
    10ms window close before seq 2 ever reaches the fsync call (`during == []`), or let the
    whole 50ms fsync complete before the 10ms sleep even fires (`during == [1, 2]`) — either
    way a false pass or a false fail unrelated to the property under test. Replaced with a
    `threading.Event` the (already monkeypatched) `_slow_fsync` itself sets the INSTANT it is
    entered — waited on unboundedly (CI's own --timeout=120 is the kill switch) via
    `asyncio.to_thread`, since the event is set from the worker thread `_do_wal_write` runs on,
    not the event loop. This is a signal the TEST constructs and owns (not reyn's own private
    state), the same class `wait_until(lambda: bool(renderer.messages))` already uses
    elsewhere (#5156) — waiting on the ACTUAL condition (seq 2's write has reached the fsync
    call, `_inflight_seq` is genuinely set) rather than a duration standing in for it."""
    import asyncio
    import os
    import threading

    orig_fsync = os.fsync
    fsync_entered = threading.Event()
    release_fsync = threading.Event()

    def _slow_fsync(fd):  # runs off-loop in to_thread → holds the in-flight window open
        fsync_entered.set()
        release_fsync.wait()  # held open until the test's own check below releases it
        return orig_fsync(fd)

    p = tmp_path / "wal.jsonl"
    sl = StateLog(p)
    await sl.append("inbox_put", text="durable")  # seq 1 — durable, real fsync, unpatched
    # Patched only AFTER seq 1 lands: patching before would also hold seq 1's own
    # fsync open on `release_fsync`, which nothing releases until after this call
    # already returned — a self-deadlock, not the race this test means to force.
    monkeypatch.setattr(os, "fsync", _slow_fsync)
    appending = asyncio.create_task(sl.append("inbox_put", text="inflight"))  # seq 2
    # Wait for the REAL condition the assertion below depends on: seq 2's write has
    # genuinely reached the fsync call (so `_inflight_seq` is set) — not a duration.
    await asyncio.to_thread(fsync_entered.wait)

    during = [e["seq"] for e in sl.iter_from(1)]
    assert during == [1], "a concurrent read must exclude the in-flight (non-durable) entry seq 2"

    release_fsync.set()  # let the held-open fsync (and the append) complete
    await appending  # fsync completes → seq 2 is now durable
    after = [e["seq"] for e in sl.iter_from(1)]
    assert after == [1, 2], "after the fsync, the now-durable entry is readable"
    await sl.aclose()


@pytest.mark.asyncio
async def test_slow_file_open_does_not_freeze_the_event_loop(tmp_path, monkeypatch):
    """Tier 2: #1765 Step 1b. Step 1a moved only `fsync` off the loop; the preceding
    `open`/`write`/`flush` stayed synchronous on the loop (#5192 later removed the
    lead-newline guard that once ran before them — see `_do_wal_write`'s own docstring —
    but `open`/`write`/`flush` themselves are the subject of this test, unaffected by
    that removal). On a
    stalled filesystem (Windows AV re-scan / cloud-sync rehydration / a stale lock — all far
    more likely after the file has sat idle), those calls freeze the WHOLE event loop, not just
    this append (the reported symptom: the user's message echoes, but the "Working…" indicator
    never appears). RED against pre-Step-1b code: a concurrent counter task would NOT advance
    during a slow `open()`, since the synchronous open call itself blocked the only thread
    running the loop."""
    import asyncio

    p = tmp_path / "wal.jsonl"
    sl = StateLog(p)

    orig_do_wal_write = sl._do_wal_write

    def _slow_do_wal_write(payload):
        import time
        time.sleep(0.2)  # simulates a stalled `open()`/`stat()` — runs off-loop in to_thread
        return orig_do_wal_write(payload)

    monkeypatch.setattr(sl, "_do_wal_write", _slow_do_wal_write)

    ticks = 0

    async def _counter() -> None:
        nonlocal ticks
        for _ in range(50):
            await asyncio.sleep(0.005)
            ticks += 1

    counter_task = asyncio.create_task(_counter())
    await sl.append("inbox_put", text="slow-open")
    # snapshot BEFORE draining counter_task to completion: if the loop had frozen for the
    # 0.2s stall, ticks would still be near-zero here, only catching up afterward — awaiting
    # counter_task first (as a prior version of this test did) makes the assertion pass
    # unconditionally regardless of whether the loop froze.
    ticks_during_append = ticks
    await counter_task
    # threshold kept low (5, not e.g. 20): on Windows — the platform this fix targets — the
    # default ~15.6ms timer resolution makes each 5ms `asyncio.sleep` take ~15.6ms, so even a
    # correct implementation only accumulates ~12 ticks in the 200ms stall. A frozen loop still
    # produces ~0, so >= 5 keeps the discrimination margin without false-failing on Windows.
    assert ticks_during_append >= 5, (
        "the loop must keep making progress on OTHER coroutines DURING the stalled "
        "open/write/flush (off-loop) — a near-zero count at this point means the loop froze"
    )
    await sl.aclose()


@pytest.mark.asyncio
async def test_seq_order_equals_file_order_under_concurrency(tmp_path):
    """Tier 2: concurrent appends still land in seq order on disk (the seq is assigned + the
    worker write submitted under the lock, so submit order = file order). RED if a coroutine
    could be preempted between seq-assign and submit (reordering the file)."""
    p = tmp_path / "wal.jsonl"
    sl = StateLog(p)
    import asyncio
    await asyncio.gather(*(sl.append("inbox_put", text=str(i)) for i in range(20)))
    on_disk = [json.loads(ln)["seq"] for ln in p.read_text().splitlines() if ln.strip()]
    assert on_disk == list(range(1, 21)), "file order must equal seq order"
    await sl.aclose()
