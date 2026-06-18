"""Shared async wait helpers for #1751 test adaptation.

After #1751, ``StateLog.append`` fsyncs via ``asyncio.to_thread`` — so a WAL append
(and the snapshot mutation / pending-iv registration that follows it inside a
fire-and-forget dispatch coroutine) no longer completes within a fixed
``await asyncio.sleep(0)`` yield loop. Tests that took that shortcut must instead
wait EXPLICITLY for the observable condition they depend on.

These helpers are the explicit, deterministic replacement (NOT a global fixture and
NOT a to_thread-sync monkeypatch — those would mask the real async timing). Each
call site passes the exact predicate it needs (pending iv registered, snapshot
mutated, WAL event durable, …), so the assertion the test makes is preserved.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

_DEFAULT_TIMEOUT = 5.0
_DEFAULT_INTERVAL = 0.005


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    interval: float = _DEFAULT_INTERVAL,
) -> bool:
    """Poll ``predicate()`` until it is truthy or ``timeout`` elapses.

    Returns True if the predicate became truthy, False on timeout (so the caller's
    ``assert`` still pins the real condition rather than a fixed sleep). Bounded —
    the awaited WAL/to_thread work lands within a few ms; the generous timeout only
    guards against a genuinely-stuck operation."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while True:
        if predicate():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval)
