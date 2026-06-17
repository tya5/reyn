"""Per-agent serialization lock registry — shared by ALL transport layers.

MCP (``reyn.mcp.server``) and A2A (``reyn.interfaces.web.routers.a2a``) both drive
``ChatSession.run_one_iteration`` for the same shared session instance. Without
cross-transport coordination a concurrent MCP request and an A2A drain loop on
the same agent race at every ``await`` inside ``run_one_iteration``, corrupting
``session.history`` (both callers mutate it concurrently).

The fix: every transport that drives ``run_one_iteration`` acquires the same
per-agent ``asyncio.Lock`` before entering the critical section.

This module is the single registry for those locks.  Both ``mcp_server`` and
``a2a`` import ``get_agent_lock`` from here; they therefore share the same
``_AGENT_LOCKS`` dict and thus the same lock object for any given agent name.

Design constraints:
- Module-level singleton dict: one process, one event-loop — the module is
  imported once and the dict is stable for the process lifetime.
- ``asyncio.Lock`` is not thread-safe, but Reyn runs on a single asyncio
  event loop, so this is fine.
- The lock is keyed by the *agent name string* — the same identity used by
  ``AgentRegistry.get_or_load``.  Transport callers must use the canonical
  name (not a URL slug or alias) so the key matches.
"""
from __future__ import annotations

import asyncio

# Module-level registry — intentionally a plain dict.
# Callers must NOT mutate this dict directly; use ``get_agent_lock`` only.
_AGENT_LOCKS: dict[str, asyncio.Lock] = {}


def get_agent_lock(agent_name: str) -> asyncio.Lock:
    """Return the per-agent ``asyncio.Lock`` for *agent_name*.

    Creates the lock on first access; subsequent calls with the same name
    return the **same** ``asyncio.Lock`` object (identity guarantee).  Locks
    for different names are distinct objects.

    Thread-safety: this function is NOT thread-safe (dict.setdefault is not
    atomic across threads), but Reyn is single-threaded asyncio — concurrent
    coroutines on the same loop are safe here because Python's GIL prevents
    true parallelism during the ``setdefault`` call.
    """
    return _AGENT_LOCKS.setdefault(agent_name, asyncio.Lock())
