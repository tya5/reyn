"""Per-path in-process serialization lock registry — `file.py`'s same-path
cross-op race guard (#2782, the path-locking step).

#2794 offloaded ``edit_file``'s read-modify-write into ONE ``asyncio.to_thread``
job — atomic WITHIN a single op (no ``await`` between the read and the write;
see ``_execute_edit_sync``'s docstring in ``file.py``). But it removed the
implicit cross-op serialization that single-threaded, single-event-loop
execution used to provide: TWO concurrent same-path writer ops (e.g. two
``edit_file`` calls, or an ``edit_file``/``write_file`` pair) now each run
their read-modify-write independently — both read, both compute, both write —
and one write silently overwrites the other (a classic lost-update race).
Concrete reachable paths: pipeline ``parallel``/``for_each`` fan-out (see
``core/pipeline/executor.py``'s "read-modify-write race" docstring), concurrent
sub-agents, concurrent A2A sessions sharing a ``base_dir``.

The fix mirrors ``runtime/agent_locks.py``'s per-key, loop-aware
``asyncio.Lock`` registry (same family, no new mechanism): an **in-process**
(NOT flock/fcntl — deliberately single-process per ADR-0018; cross-process
mutual exclusion is deferred to the A2A-server model, a separate initiative)
``asyncio.Lock`` keyed by the RESOLVED absolute path, acquired by ``file.py``'s
async op handlers across the whole read-modify-write / mutate call, released
in ``finally`` (via ``async with``). Two concurrent ops on DIFFERENT paths
never contend; two concurrent ops on the SAME path serialize.

A dedicated registry (not a shared dict with ``agent_locks.py``) — paths and
agent names are different key domains; sharing one dict risks an accidental
collision and conflates two unrelated concerns. The MECHANISM is reused
(identical loop-aware-keying pattern); the registry is not.

Loop-aware keying (mirrors #1762's rationale in ``agent_locks.py``): an
``asyncio.Lock`` binds to the event loop it is first awaited on; keying the
registry by the running loop (not just the path) means each loop gets its own
lock objects, so a lock created under one pytest-asyncio test's loop is never
reused (and tripped) under a later test's loop. Production runs a single
process-wide loop, so this is transparent there.
"""
from __future__ import annotations

import asyncio
import weakref
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator

# Per-running-loop registries of per-path locks. Keyed by the event loop so
# each loop gets distinct lock objects (an asyncio.Lock is bound to the loop
# it is awaited on). WeakKeyDictionary → a dead loop's locks are GC'd with it.
# Callers must NOT mutate this directly; use ``get_path_lock`` /
# ``locked_paths`` only.
_LOCKS_BY_LOOP: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def get_path_lock(resolved_path: str) -> asyncio.Lock:
    """Return the per-path ``asyncio.Lock`` for *resolved_path* on the running loop.

    ``resolved_path`` MUST be the fully resolved absolute path (the same
    string ``Workspace._resolve_write``/``_resolve_read`` would compute, e.g.
    via ``file.py``'s ``_resolve_for_gate``) — two callers naming the same
    file via different spellings (relative vs absolute, unresolved symlink)
    would otherwise take out DIFFERENT locks and the guard would silently not
    apply.

    Identity guarantee: repeated calls with the same path **on the same loop**
    return the **same** ``asyncio.Lock`` object; different paths yield
    distinct objects. Different loops yield distinct objects by design
    (mirrors ``agent_locks.get_agent_lock``, #1762).
    """
    loop = asyncio.get_running_loop()
    per_loop = _LOCKS_BY_LOOP.get(loop)
    if per_loop is None:
        per_loop = {}
        _LOCKS_BY_LOOP[loop] = per_loop
    return per_loop.setdefault(resolved_path, asyncio.Lock())


@asynccontextmanager
async def locked_paths(*resolved_paths: str) -> AsyncIterator[None]:
    """Acquire the per-path locks for every path in *resolved_paths*, held for
    the duration of the ``async with`` block, released in reverse order on
    exit (``AsyncExitStack``) — including on exception.

    Multi-path callers (``move`` locks both source and destination) MUST go
    through this helper rather than nesting ``async with get_path_lock(...)``
    by hand: paths are deduplicated and then acquired in **sorted order**, a
    fixed global ordering that prevents the classic two-lock deadlock (task A
    locks src-then-dest while task B concurrently locks dest-then-src).
    """
    async with AsyncExitStack() as stack:
        for path in sorted(set(resolved_paths)):
            await stack.enter_async_context(get_path_lock(path))
        yield
