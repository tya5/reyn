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

#4275: ``wait_until`` used to take a ``timeout`` and return ``bool`` — a bounded
poll whose own docstring argued "the generous timeout only guards against a
genuinely-stuck operation." Per the #4145 owner ruling (no exception for
time-dependent tests unless reyn's own logic is genuinely under test — a bounded
failure constant is a wait duration rewritten as a number, and it duplicates
CI's own kill switch), the wait is now unconditional: it returns ``None`` and
blocks until ``predicate()`` holds. A predicate that never holds hangs the test,
surfaced by CI's `--timeout=120`, not by a local number chosen for this call.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

_DEFAULT_INTERVAL = 0.005


async def wait_until(
    predicate: Callable[[], bool],
    *,
    interval: float = _DEFAULT_INTERVAL,
) -> None:
    """Poll ``predicate()`` until it is truthy. Unbounded — see module docstring.

    Returns nothing: by the time this returns, ``predicate()`` holds, so the
    caller has nothing left to check on this axis. A predicate that never
    becomes true hangs the test, surfaced by CI's own timeout kill switch
    rather than a bounded failure constant local to this call.
    """
    while not predicate():
        await asyncio.sleep(interval)
