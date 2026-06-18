"""Tier 2: OS invariant tests for the shared per-agent lock registry (#1128).

Pins the cross-transport serialization guarantee introduced in PR-b of
issue #1128: MCP and A2A must acquire the SAME ``asyncio.Lock`` for a
given agent name so concurrent MCP+A2A calls to the same session serialize
rather than racing on ``session.history``.

Invariants exercised:
  (a) ``get_agent_lock("x")`` is idempotent: repeated calls return the
      identical lock object (``is`` identity).
  (b) Different agent names yield distinct lock objects.
  (c) MCP and A2A obtain the SAME lock object for the same agent_name —
      both import from ``reyn.runtime.agent_locks``; the module-level dict
      ensures identity.
  (d) Concurrent coroutines acquiring the same lock are serialized:
      critical sections do not overlap (behavioral, not count-pin).

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- No unittest.mock / MagicMock / AsyncMock / patch.
- Real ``asyncio.Lock`` instances via the public ``get_agent_lock`` surface.
- No private-state assertions (``_AGENT_LOCKS`` internals not touched).
- No ``len(x) == N`` count pins; behavioral / identity assertions only.
- Each test docstring first line is exactly ``Tier 2: ...``.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.runtime.agent_locks import get_agent_lock

# ---------------------------------------------------------------------------
# (a) Idempotency — same name → same lock object
# ---------------------------------------------------------------------------


def test_same_name_returns_same_lock() -> None:
    """Tier 2: get_agent_lock returns the identical lock object on repeated calls."""
    lock_first = get_agent_lock("agent-alpha")
    lock_second = get_agent_lock("agent-alpha")
    assert lock_first is lock_second, (
        "get_agent_lock must return the same asyncio.Lock instance for the same "
        "agent_name on every call (idempotency / identity guarantee)"
    )


# ---------------------------------------------------------------------------
# (b) Different names yield distinct locks
# ---------------------------------------------------------------------------


def test_different_names_return_distinct_locks() -> None:
    """Tier 2: get_agent_lock returns distinct lock objects for different agent names."""
    lock_a = get_agent_lock("agent-one")
    lock_b = get_agent_lock("agent-two")
    assert lock_a is not lock_b, (
        "get_agent_lock must return distinct asyncio.Lock objects for different "
        "agent names — sharing a lock across agents would over-serialize"
    )


# ---------------------------------------------------------------------------
# (c) Cross-transport sharing: MCP and A2A import from the same registry
# ---------------------------------------------------------------------------


def test_mcp_and_a2a_share_same_lock_registry() -> None:
    """Tier 2: MCP and A2A obtain the same lock object for the same agent_name.

    This is the central cross-transport guarantee of #1128 PR-b: both
    mcp_server and a2a import ``get_agent_lock`` (aliased as
    ``_get_agent_lock`` in mcp_server) from ``reyn.runtime.agent_locks``.
    Because Python module imports are singletons, the module-level
    ``_AGENT_LOCKS`` dict is shared, so the same agent_name yields the
    same ``asyncio.Lock`` object regardless of which transport calls it.
    """
    # Import MCP's accessor — it is aliased as _get_agent_lock in mcp_server
    # but the underlying function is the same object from agent_locks.
    from reyn.mcp.server import (
        _get_agent_lock as mcp_get_lock,  # type: ignore[attr-defined]  # noqa: PLC0415
    )

    agent = "cross-transport-agent"
    lock_via_agent_locks = get_agent_lock(agent)
    lock_via_mcp = mcp_get_lock(agent)

    assert lock_via_agent_locks is lock_via_mcp, (
        "MCP's _get_agent_lock and reyn.runtime.agent_locks.get_agent_lock must "
        "return the SAME asyncio.Lock object for the same agent_name. "
        "If they differ, a concurrent MCP+A2A pair on the same agent bypasses "
        "the serialization guarantee."
    )


# ---------------------------------------------------------------------------
# (d) Behavioral: concurrent coroutines are serialized (no overlap)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_lock_acquirers_are_serialized() -> None:
    """Tier 2: concurrent coroutines acquiring the same agent lock are serialized.

    Two coroutines race to enter a critical section guarded by
    ``get_agent_lock``.  The invariant: their execution windows must not
    overlap — the second coroutine must not enter before the first exits.
    Verified by recording entry/exit times and asserting non-overlap.
    """
    agent = "serialization-test-agent"
    # Track whether the lock was observed as already held when the second
    # coroutine reached the acquire site.  A real asyncio.Lock serializes:
    # the second coroutine blocks until the first releases.
    inside_flag: list[bool] = []  # True if critical section was entered while other held it
    lock_held = asyncio.Event()  # signals "first is inside the section"
    first_released = asyncio.Event()

    async def first_holder() -> None:
        async with get_agent_lock(agent):
            lock_held.set()
            # Hold lock long enough for second to try to acquire.
            await asyncio.sleep(0.02)
            first_released.set()

    async def second_waiter() -> None:
        # Wait until first has entered, then try to acquire — this races
        # with first_holder holding the lock.
        await lock_held.wait()
        async with get_agent_lock(agent):
            # If we reach here before first released, the lock didn't serialize.
            inside_flag.append(first_released.is_set())

    await asyncio.gather(first_holder(), second_waiter())

    assert inside_flag, "second_waiter must have entered the critical section at least once"
    assert all(inside_flag), (
        "second_waiter entered the critical section before first_holder released "
        "the lock — the per-agent lock is not serializing concurrent coroutines"
    )
