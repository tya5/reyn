"""DurabilityWorker — a single serial worker that runs durable writes off the event loop.

#1765 Step 1a. A blocking ``os.fsync`` does not yield, so it freezes the WHOLE event loop
(TUI repaint + every concurrent session) for its duration — on slow storage that is hundreds
of ms per fsync. This worker moves the fsync OFF the loop: a substrate submits a durable-write
task (a coroutine that writes + ``await asyncio.to_thread(os.fsync, …)``), the worker runs it
serially, and ``submit`` awaits its completion — so the caller's durability contract is
UNCHANGED (it returns only once durable) while the loop stays free DURING the fsync.

Substrate-agnostic by construction (P7): the worker holds no WAL / snapshot / workspace
knowledge — the injected write callable carries all of it (including any post-write bookkeeping
such as a durable-seq watermark). Its one structural guarantee is **serial FIFO**: tasks run
one at a time in submit order, so *enqueue order = durability order*. That single ordering point
is what later steps build on — the cross-substrate write-ahead ordering (a depended-upon
substrate submitted before the WAL event that references it) and, in #1765 Step 2, non-blocking
writes (``submit`` returns before the task runs, with a barrier awaiting only where an external
effect is gated). For Step 1a ``submit`` always awaits (no relaxed-durability window).
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

DurableWrite = Callable[[], Awaitable[None]]


class DurabilityWorker:
    """A single serialisation point for off-loop durable writes (see module docstring).

    Step 1a uses a fair ``asyncio.Lock`` as the serial point: ``submit`` acquires it (FIFO —
    asyncio locks grant in acquisition order, so submit order = durability order), runs the
    task (its ``await to_thread(os.fsync)`` keeps the loop free), and releases. No background
    task → nothing to leak between event loops. Step 2 (non-blocking writes) evolves the
    INTERNALS to a queue + drainer behind this SAME ``submit`` contract — so the substrate
    routing built on it is not throwaway."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def submit(self, do_durable_write: DurableWrite) -> None:
        """Run a durable-write task, serialised with every other submit (FIFO = durability
        order), and AWAIT it (#1765 Step 1a — blocking: no relaxed-durability window). The
        task's off-loop fsync keeps the event loop free for non-durability work; other submits
        wait their turn. A failure inside the task is re-raised here (same as an inline write)."""
        async with self._lock:
            await do_durable_write()

    async def aclose(self) -> None:
        """Graceful shutdown. No background task in Step 1a, so this only waits for any
        in-flight submit to finish (acquire→release the lock); a no-op otherwise."""
        async with self._lock:
            pass
