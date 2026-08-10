"""Tier 2: OS invariant tests for the shared per-(agent, sid) lock registry (#1128, #3978).

Pins the cross-transport serialization guarantee introduced in PR-b of
issue #1128: MCP and A2A must acquire the SAME ``asyncio.Lock`` for a
given (agent, sid) session so concurrent MCP+A2A calls to the same session
serialize rather than racing on ``session.history``.

Invariants exercised (identity invariants hold WITHIN a running loop — the
registry is loop-aware since #1762, see ``agent_locks`` docstring):
  (a) ``get_agent_lock("x")`` is idempotent: repeated calls on the same loop
      return the identical lock object (``is`` identity).
  (b) Different agent names yield distinct lock objects.
  (d) Concurrent coroutines acquiring the same lock are serialized:
      critical sections do not overlap (behavioral, not count-pin).
  (e) #1762 regression: a contended lock used across distinct event loops
      (= pytest-asyncio's per-test fresh loops) must NOT raise "bound to a
      different event loop" — loop-aware keying gives each loop its own lock.
  (f) Proposal 0067 P1 (#3978, architect A-2 axis-mismatch finding): two
      different ``sid``s for the SAME agent name now yield DISTINCT locks —
      this lock protects one Session's ``history``, not everything sharing
      an agent name, and an agent can have more than one live session
      (a per-delegation ``a2a:<id>`` session alongside "main").
  (g) ``sid=None`` (the default, meaning "main") is the SAME key as an
      explicit ``sid="main"`` — a bare pre-#3978-style call still serializes
      against an explicit-sid caller for the same agent's main session.

Policy compliance (docs/deep-dives/contributing/testing.ja.md):
- No unittest.mock / MagicMock / AsyncMock / patch.
- Real ``asyncio.Lock`` instances via the public ``get_agent_lock`` surface.
- No private-state assertions (``_LOCKS_BY_LOOP`` internals not touched).
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


@pytest.mark.asyncio
async def test_same_name_returns_same_lock() -> None:
    """Tier 2: get_agent_lock returns the identical lock object on repeated calls (same loop)."""
    lock_first = get_agent_lock("agent-alpha")
    lock_second = get_agent_lock("agent-alpha")
    assert lock_first is lock_second, (
        "get_agent_lock must return the same asyncio.Lock instance for the same "
        "agent_name on every call (idempotency / identity guarantee)"
    )


# ---------------------------------------------------------------------------
# (b) Different names yield distinct locks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_names_return_distinct_locks() -> None:
    """Tier 2: get_agent_lock returns distinct lock objects for different agent names."""
    lock_a = get_agent_lock("agent-one")
    lock_b = get_agent_lock("agent-two")
    assert lock_a is not lock_b, (
        "get_agent_lock must return distinct asyncio.Lock objects for different "
        "agent names — sharing a lock across agents would over-serialize"
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


# ---------------------------------------------------------------------------
# (e) #1762 regression: a contended lock is reusable across distinct loops
# ---------------------------------------------------------------------------


def test_agent_lock_reusable_across_event_loops() -> None:
    """Tier 2: a contended agent lock survives distinct event loops (#1762).

    Before #1762 the registry keyed locks by name only, so a lock created under
    one event loop was cached and reused under the next. An ``asyncio.Lock``
    binds to the loop a *waiter* registers on, so the second loop's waiter raised
    ``"... is bound to a different event loop"``. This is exactly pytest-asyncio's
    pattern (a fresh loop per test) → an order-dependent flake.

    This test drives the SAME agent name through two separate ``asyncio.run``
    loops, each with a contended acquire (a holder + a waiter, so the lock's
    loop-binding path is exercised). Loop-aware keying must make both runs pass.
    Run sync (two real loops via ``asyncio.run``) since the bug is precisely a
    cross-loop one — a single ``@pytest.mark.asyncio`` loop could not surface it.
    """
    agent = "cross-loop-regression-agent-1762"

    async def contended_once() -> None:
        lock = get_agent_lock(agent)
        first_in = asyncio.Event()

        async def holder() -> None:
            async with lock:
                first_in.set()
                await asyncio.sleep(0.01)

        async def waiter() -> None:
            await first_in.wait()  # ensure holder holds → this acquire must WAIT
            async with lock:       # the waiting path is what binds the lock to the loop
                pass

        await asyncio.gather(holder(), waiter())

    # Two separate event loops, same agent name. Pre-#1762 the second run raised
    # RuntimeError("... bound to a different event loop"); loop-aware keying passes.
    asyncio.run(contended_once())
    asyncio.run(contended_once())


# ---------------------------------------------------------------------------
# (f)/(g) Proposal 0067 P1 (#3978): (agent_name, sid) axis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_sids_for_same_agent_return_distinct_locks() -> None:
    """Tier 2: get_agent_lock("x", "main") and get_agent_lock("x", "a2a:1")
    return DISTINCT lock objects (#3978 architect A-2: the lock protects one
    Session's history, and one agent name can have more than one live
    session — a per-delegation sid must not serialize against "main").

    Falsify-verified: reverting ``get_agent_lock`` to key by ``agent_name``
    alone (the pre-#3978 shape) makes this go RED — both calls would return
    the same object.
    """
    lock_main = get_agent_lock("shared-agent", "main")
    lock_delegation = get_agent_lock("shared-agent", "a2a:peer-1")
    assert lock_main is not lock_delegation, (
        "get_agent_lock must return distinct asyncio.Lock objects for "
        "different sids of the SAME agent — sharing one lock across "
        "sessions that share no state over-serializes them"
    )


@pytest.mark.asyncio
async def test_absent_sid_is_the_same_key_as_explicit_main() -> None:
    """Tier 2: get_agent_lock("x") (sid omitted) and get_agent_lock("x", "main")
    return the SAME lock object — the omitted-sid convention canonicalizes
    to "main", so a caller that never passes sid still serializes correctly
    against a caller that explicitly names the main session.
    """
    lock_bare = get_agent_lock("bare-vs-main-agent")
    lock_explicit_main = get_agent_lock("bare-vs-main-agent", "main")
    assert lock_bare is lock_explicit_main, (
        "get_agent_lock(name) and get_agent_lock(name, \"main\") must be the "
        "same key — omitted sid means the main session, same as explicit "
        "sid=\"main\""
    )


@pytest.mark.asyncio
async def test_different_sid_locks_do_not_serialize_against_each_other() -> None:
    """Tier 2: concurrent coroutines holding locks for DIFFERENT sids of the
    same agent do NOT block each other — the behavioral counterpart to (f).
    Mirrors (d)'s serialization test, but proves the opposite: distinct
    sessions run freely in parallel.
    """
    agent = "parallel-sid-agent"
    main_entered = asyncio.Event()
    delegation_entered = asyncio.Event()
    entered: set[str] = set()

    async def hold(sid: str, own: "asyncio.Event", other: "asyncio.Event") -> None:
        async with get_agent_lock(agent, sid):
            entered.add(sid)
            own.set()
            # Wait for the OTHER sid's coroutine to also be inside its own
            # critical section concurrently — if they were serialized by a
            # shared lock, this would deadlock (the other could never enter
            # while this one waits on an event only the other sets) and the
            # test would time out rather than pass.
            await asyncio.wait_for(other.wait(), timeout=2.0)

    await asyncio.gather(
        hold("main", main_entered, delegation_entered),
        hold("a2a:peer-2", delegation_entered, main_entered),
    )
    assert entered == {"main", "a2a:peer-2"}
