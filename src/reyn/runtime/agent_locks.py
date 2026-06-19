"""Per-agent serialization lock registry — shared by ALL transport layers.

MCP (``reyn.mcp.server``) and A2A (``reyn.interfaces.web.routers.a2a``) both drive
``Session.run_one_iteration`` for the same shared session instance. Without
cross-transport coordination a concurrent MCP request and an A2A drain loop on
the same agent race at every ``await`` inside ``run_one_iteration``, corrupting
``session.history`` (both callers mutate it concurrently).

The fix: every transport that drives ``run_one_iteration`` acquires the same
per-agent ``asyncio.Lock`` before entering the critical section.

This module is the single registry for those locks.  Both ``mcp_server`` and
``a2a`` import ``get_agent_lock`` from here; on a given event loop they therefore
share the same lock object for any given agent name.

Design constraints:
- The lock is keyed by the *agent name string* — the same identity used by
  ``AgentRegistry.get_or_load``.  Transport callers must use the canonical
  name (not a URL slug or alias) so the key matches.
- ``asyncio.Lock`` is not thread-safe, but Reyn runs on a single asyncio
  event loop, so this is fine.
- **Loop-aware keying (#1762).** An ``asyncio.Lock`` binds to the event loop it
  is first awaited on; caching one lock across loops raises ``"... is bound to a
  different event loop"`` the moment a *second* loop has a waiter on it. In
  production this never happens (single process-wide loop), but a registry keyed
  *only* by name leaks that assumption into any multi-loop context — most
  visibly pytest-asyncio, which gives every test a fresh loop, so a lock created
  under one test's loop is reused (and trips) under the next. Keying the registry
  by the *running loop* makes each loop get its own lock object: identity holds
  WITHIN a loop (all the serialization guarantee needs — production is one loop)
  while cross-loop reuse can no longer occur. The per-loop maps live in a
  ``WeakKeyDictionary`` so a finished loop's locks are garbage-collected with it.
"""
from __future__ import annotations

import asyncio
import weakref

# Per-running-loop registries of per-agent locks (#1762). Keyed by the event loop
# so each loop gets distinct lock objects (an asyncio.Lock is bound to the loop it
# is awaited on). WeakKeyDictionary → a dead loop's locks are GC'd with it.
# Callers must NOT mutate this directly; use ``get_agent_lock`` only.
_LOCKS_BY_LOOP: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def get_agent_lock(agent_name: str) -> asyncio.Lock:
    """Return the per-agent ``asyncio.Lock`` for *agent_name* on the running loop.

    Must be called from within a running event loop — every transport driver
    acquires the lock inside its ``async with`` critical section, so a running
    loop is always present at the call site.

    Identity guarantee: repeated calls with the same name **on the same loop**
    return the **same** ``asyncio.Lock`` object; different names yield distinct
    objects. Different loops yield distinct objects by design — an
    ``asyncio.Lock`` is bound to the loop it is awaited on, so cross-loop reuse
    would raise ``"bound to a different event loop"`` (#1762). Production runs on
    a single loop, so this is transparent there.

    Thread-safety: NOT thread-safe (``dict.setdefault`` is not atomic across
    threads), but Reyn is single-threaded asyncio — concurrent coroutines on the
    same loop are safe because the GIL serializes the ``setdefault`` call.
    """
    loop = asyncio.get_running_loop()
    per_loop = _LOCKS_BY_LOOP.get(loop)
    if per_loop is None:
        per_loop = {}
        _LOCKS_BY_LOOP[loop] = per_loop
    return per_loop.setdefault(agent_name, asyncio.Lock())
