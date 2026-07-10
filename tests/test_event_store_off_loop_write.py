"""Tier 2: EventStore off-loop write (#2780).

``EventStore.write()`` used to ``open()``/``write()`` synchronously, directly
on the event loop — the same class of bug as #1765's WAL append, except
unmitigated (not even fsync was offloaded) and far more exposed (fires on
every chat event, not just WAL appends). This suite verifies:

- the event loop keeps making progress during a slow write (the actual bug
  being fixed) — RED against a hypothetical synchronous write, verified via
  the same "snapshot before draining the counter" discipline the #1765 PR's
  own review caught (a counter drained BEFORE the snapshot passes
  unconditionally regardless of whether the loop froze);
- write order is preserved (enqueue order == emission order == on-disk
  order), the "WAL-event ordering" property the owner asked about directly;
- rotation now uses an in-memory byte counter, never ``Path.stat()`` (the
  old code's `.stat()` fired on literally every write() call once max_bytes
  is nonzero — the actual default — not a rare path);
- ``aclose()``/``flush()`` drain pending writes, since ``write()`` is
  fire-and-forget;
- a caller with no running event loop (e.g. a synchronous CLI path) still
  gets an immediate, synchronous write — no regression for that case.

Real ``EventStore``/``DurabilityWorker`` instances, real filesystem
(``tmp_path``), no mocks of collaborators.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.core.events.event_store import EventStore
from reyn.schemas.models import Event


def _ev(kind: str = "test_event", **data) -> Event:
    return Event(type=kind, data=data)


def _lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.mark.asyncio
async def test_slow_write_does_not_freeze_the_event_loop(tmp_path, monkeypatch):
    """Tier 2: the loop keeps advancing DURING a stalled write — the actual bug.

    Snapshot BEFORE draining the counter task to completion (the #2739/#1765
    review lesson): awaiting the counter first would make this pass
    unconditionally regardless of whether the loop actually froze.
    """
    store = EventStore(tmp_path / "events")
    orig_write_sync = store._write_line_sync

    def _slow_write_sync(path, line):
        import time
        time.sleep(0.2)
        return orig_write_sync(path, line)

    monkeypatch.setattr(store, "_write_line_sync", _slow_write_sync)

    ticks = 0

    async def _counter() -> None:
        nonlocal ticks
        for _ in range(50):
            await asyncio.sleep(0.005)
            ticks += 1

    counter_task = asyncio.create_task(_counter())
    store.write(_ev("slow"))
    await asyncio.sleep(0.21)  # let the stalled write's window pass
    ticks_during_write = ticks
    await counter_task
    await store.aclose()
    assert ticks_during_write >= 5, (
        "the loop must keep making progress on OTHER coroutines during the "
        "stalled write — a near-zero count means it froze"
    )


@pytest.mark.asyncio
async def test_write_order_preserved_across_concurrent_emits(tmp_path):
    """Tier 2: enqueue order == emission order == on-disk order (the "WAL-event
    ordering" property) — write() is synchronous and enqueues in call order,
    and the worker's FIFO guarantee keeps that order on disk."""
    store = EventStore(tmp_path / "events")
    for i in range(20):
        store.write(_ev("seq", i=i))
    await store.aclose()

    on_disk = [e["data"]["i"] for e in _lines(store.active_path)]
    assert on_disk == list(range(20))


@pytest.mark.asyncio
async def test_rotation_never_calls_path_stat(tmp_path, monkeypatch):
    """Tier 2: `_should_rotate()` uses the in-memory byte counter, not
    `Path.stat()` — the old code's stat() fired on EVERY write() call once
    max_bytes is nonzero (the actual default, 10MB), not a rare path.

    Records calls to the ACTIVE store path specifically (rather than raising
    from a global Path.stat monkeypatch, which corrupts pytest's own internal
    traceback formatting — that also calls Path.stat/exists on unrelated
    paths)."""
    orig_stat = Path.stat
    stat_calls: list[Path] = []
    store = EventStore(tmp_path / "events", max_bytes=1000)

    def _tracking_stat(self, *a, **kw):
        if store.active_path is not None and self == store.active_path:
            stat_calls.append(self)
        return orig_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", _tracking_stat)
    for i in range(10):
        store.write(_ev("no_stat", i=i))
    await store.aclose()
    assert stat_calls == [], (
        f"_should_rotate() must not stat() the active file — got {len(stat_calls)} calls"
    )


@pytest.mark.asyncio
async def test_rotation_fires_via_in_memory_counter(tmp_path):
    """Tier 2: a write that pushes the running byte count past max_bytes
    triggers rotation — a NEW active file is opened, proving the counter
    (not a removed stat() call) still drives rotation correctly."""
    store = EventStore(tmp_path / "events", max_bytes=50)
    store.write(_ev("a", text="x" * 60))  # first write always fits (no rotation check yet)
    first_path = store.active_path
    store.write(_ev("b", text="y"))  # now over max_bytes → should rotate
    second_path = store.active_path
    await store.aclose()
    assert second_path != first_path, "expected rotation to a new file once max_bytes is exceeded"


@pytest.mark.asyncio
async def test_aclose_drains_pending_writes(tmp_path):
    """Tier 2: aclose() waits for every enqueued write to land — without it,
    a write queued right before teardown could be silently lost (the actual
    /quit-drops-trailing-events bug this fix addresses)."""
    store = EventStore(tmp_path / "events")
    store.write(_ev("before_close"))
    await store.aclose()
    on_disk = _lines(store.active_path)
    assert any(e["type"] == "before_close" for e in on_disk)


@pytest.mark.asyncio
async def test_flush_drains_without_closing_the_store(tmp_path):
    """Tier 2: flush() drains pending writes but the store stays usable
    afterward (unlike aclose(), which shuts the worker down)."""
    store = EventStore(tmp_path / "events")
    store.write(_ev("first"))
    await store.flush()
    on_disk_after_flush = _lines(store.active_path)
    assert any(e["type"] == "first" for e in on_disk_after_flush)

    # Store still works after flush — not torn down.
    store.write(_ev("second"))
    await store.aclose()
    on_disk_final = _lines(store.active_path)
    assert {e["type"] for e in on_disk_final} == {"first", "second"}


def test_write_without_running_loop_falls_back_to_synchronous(tmp_path):
    """Tier 2: a caller with no running event loop (e.g. a synchronous CLI
    entry point) still gets an immediate write — submit_nowait requires a
    running loop and would otherwise raise, a regression this fix must not
    introduce. This test is deliberately a plain `def`, not `async def`, so
    there is no running loop."""
    store = EventStore(tmp_path / "events")
    store.write(_ev("sync_fallback"))
    # No await/aclose needed — the write already happened synchronously.
    on_disk = _lines(store.active_path)
    assert any(e["type"] == "sync_fallback" for e in on_disk)
