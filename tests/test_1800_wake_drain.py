"""Tests for #1800 slice 4a: `wake` flag + `_drain_to_wake` primitive.

Two test groups:

1. **Tier 2b (subsystem invariant) — equivalence property**: with NO
   ``wake=false`` messages ever enqueued, the run-loop is behaviorally
   identical to the previous single-get path (one message → one turn,
   same WAL events, same outbox output).  This is the safety assertion
   that the new drain primitive does not regress existing behaviour.

2. **Tier 1 (contract) — `_drain_to_wake` drain contract**: unit-level
   contract tests for the method itself, exercised through the public
   `_put_inbox` + `_drain_to_wake` surface (no mock, real Session).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock.MagicMock / AsyncMock / patch to fake collaborators.
  LLM is faked via a real async callable stub (Tier 2c policy).
- No private-state assertions.  Observations flow through:
  - StateLog.iter_from() on the on-disk WAL (inbox_put / inbox_consume events)
  - session.outbox (OutboxMessage kind / text)
  - AgentSnapshot.load() for fully external snapshot re-read
- Each test docstring first line declares its Tier.
- Count assertions use variable-binding idiom to pass tier audit
  (see feedback_test_audit_canonical_idioms.md).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.session import Session

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


def _text_result(text: str = "ok") -> LLMToolCallResult:
    """Real LLMToolCallResult that makes RouterLoop emit a text reply."""
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


def _make_session(tmp_path: Path, *, agent_name: str = "test_agent") -> Session:
    """Build a minimal Session with WAL + snapshot path."""
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _wal_events(tmp_path: Path) -> list[dict]:
    """Read all WAL events from the session state log."""
    log = StateLog(tmp_path / "state.wal")
    return list(log.iter_from(0))


async def _run_n_turns_then_shutdown(
    session: Session,
    n: int,
    timeout: float = 3.0,
) -> None:
    """Run the session loop until n turns complete, then shutdown."""
    turns_done = [0]
    original_run_one = session.run_one_iteration

    async def _counting_run_one() -> bool:
        result = await original_run_one()
        if result:
            turns_done[0] += 1
        return result

    session.run_one_iteration = _counting_run_one  # type: ignore[method-assign]

    run_task = asyncio.create_task(session.run())
    # Wait until n turns complete, then shutdown.
    deadline = asyncio.get_event_loop().time() + timeout
    while turns_done[0] < n:
        if asyncio.get_event_loop().time() > deadline:
            break
        await asyncio.sleep(0.005)
    await session.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except asyncio.TimeoutError:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Group 1 — Tier 2b: Equivalence property
# (no wake=false ever enqueued → run-loop identical to pre-slice-4a behaviour)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wake_drain_equivalence_single_message(tmp_path, monkeypatch) -> None:
    """Tier 2b: one wake=true (absent) message → one turn, WAL identical to pre-4a.

    With no wake=false messages ever enqueued, _drain_to_wake reduces to a
    single blocking get and the run-loop behaviour is byte-identical to the
    previous _consume_inbox-only path:
    - exactly one inbox_put WAL event
    - exactly one inbox_consume WAL event
    - snapshot inbox pruned to empty after consumption
    """
    monkeypatch.chdir(tmp_path)

    session = _make_session(tmp_path)
    session.is_attached = True

    stub = _make_llm_stub_fn(_text_result("hello back"))
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", stub)

    await session.submit_user_text("hello")
    await _run_n_turns_then_shutdown(session, n=1)

    events = _wal_events(tmp_path)
    put_events = [e for e in events if e.get("kind") == "inbox_put"]
    consume_events = [e for e in events if e.get("kind") == "inbox_consume"]

    # Exactly one put and one consume — variable-binding idiom per audit policy.
    n_put = len(put_events)
    assert n_put == 1, f"Expected 1 inbox_put WAL event; got {n_put}"

    n_consume = len(consume_events)
    assert n_consume == 1, f"Expected 1 inbox_consume WAL event; got {n_consume}"

    # The msg_id in put must match the msg_id in consume.
    (put,) = put_events
    (consume,) = consume_events
    assert put.get("msg_id") == consume.get("msg_id"), (
        "inbox_put msg_id must match inbox_consume msg_id"
    )

    # Snapshot inbox must be empty: message was consumed.
    snapshot = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    assert snapshot.inbox == [], (
        f"Snapshot inbox must be empty after consumption; got {snapshot.inbox}"
    )


@pytest.mark.asyncio
async def test_wake_drain_equivalence_three_messages(tmp_path, monkeypatch) -> None:
    """Tier 2b: three sequential wake=true (absent) messages → three turns, WAL intact.

    Safety property: with no wake=false ever enqueued, N messages produce
    exactly N inbox_put + N inbox_consume WAL events — same as before 4a.
    The run-loop does not accumulate or skip messages.
    """
    monkeypatch.chdir(tmp_path)

    session = _make_session(tmp_path)
    session.is_attached = True

    stubs = [_text_result(f"reply {i}") for i in range(3)]
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _make_llm_stub_fn(stubs),
    )

    for i in range(3):
        await session.submit_user_text(f"msg {i}")

    await _run_n_turns_then_shutdown(session, n=3)

    events = _wal_events(tmp_path)
    put_events = [e for e in events if e.get("kind") == "inbox_put"]
    consume_events = [e for e in events if e.get("kind") == "inbox_consume"]

    n_put = len(put_events)
    assert n_put == 3, f"Expected 3 inbox_put events; got {n_put}"

    n_consume = len(consume_events)
    assert n_consume == 3, f"Expected 3 inbox_consume events; got {n_consume}"

    # Each put msg_id must match a consume msg_id (set equality).
    put_ids = {e.get("msg_id") for e in put_events}
    consume_ids = {e.get("msg_id") for e in consume_events}
    assert put_ids == consume_ids, (
        f"inbox_put msg_ids must equal inbox_consume msg_ids;\n"
        f"  put:     {put_ids}\n"
        f"  consume: {consume_ids}"
    )


# ---------------------------------------------------------------------------
# Group 2 — Tier 1: _drain_to_wake contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_to_wake_wake_true_first_returns_immediately(
    tmp_path,
) -> None:
    """Tier 1: wake=true (explicit) as the first message → no ride-alongs, trigger returned.

    Contract: _drain_to_wake returns ([], (kind, payload)) with an empty
    ride-alongs list when the first message has wake=true (or absent).
    """
    session = _make_session(tmp_path)

    # Enqueue a wake=true message directly (explicit flag set to True).
    await session._put_inbox("user", {"text": "hi", "wake": True})

    ride_alongs, trigger = await session._drain_to_wake()

    assert ride_alongs == [], (
        f"Expected no ride-alongs for wake=true first message; got {ride_alongs}"
    )
    assert trigger is not None, "_drain_to_wake must return a trigger, not None"
    kind, payload = trigger
    assert kind == "user", f"Expected kind='user'; got {kind!r}"
    assert payload.get("text") == "hi", f"Expected text='hi'; got {payload!r}"


@pytest.mark.asyncio
async def test_drain_to_wake_absent_wake_treated_as_true(tmp_path) -> None:
    """Tier 1: absent wake field → treated as wake=true (back-compat default).

    Existing producers (submit_user_text, skill_completed, task_ready, etc.)
    never set the wake field.  Absent must equal wake=true so they are
    never treated as ride-alongs.
    """
    session = _make_session(tmp_path)

    # submit_user_text does NOT set wake — this is the back-compat case.
    await session.submit_user_text("back-compat")

    ride_alongs, trigger = await session._drain_to_wake()

    assert ride_alongs == [], (
        f"Absent wake must be treated as True; ride-alongs must be empty: {ride_alongs}"
    )
    assert trigger is not None
    kind, _ = trigger
    assert kind == "user"


@pytest.mark.asyncio
async def test_drain_to_wake_collects_ride_alongs_then_trigger(tmp_path) -> None:
    """Tier 1: [wake=false, wake=false, wake=true] → 2 ride-alongs + trigger.

    Contract: _drain_to_wake accumulates wake=false messages as ride-alongs
    and returns them together with the first wake=true trigger.
    """
    session = _make_session(tmp_path)

    # Two ride-alongs (wake=false) then one trigger (wake=true).
    await session._put_inbox("user", {"text": "ride1", "wake": False})
    await session._put_inbox("user", {"text": "ride2", "wake": False})
    await session._put_inbox("user", {"text": "trigger", "wake": True})

    ride_alongs, trigger = await session._drain_to_wake()

    # Exactly 2 ride-alongs — unpack-enforcement idiom.
    ra1, ra2 = ride_alongs

    assert ra1[1].get("text") == "ride1", (
        f"First ride-along must be 'ride1'; got {ra1}"
    )
    assert ra2[1].get("text") == "ride2", (
        f"Second ride-along must be 'ride2'; got {ra2}"
    )
    assert trigger is not None
    kind, payload = trigger
    assert payload.get("text") == "trigger", (
        f"Trigger must be 'trigger'; got {payload}"
    )


@pytest.mark.asyncio
async def test_drain_to_wake_shutdown_returns_none_none(tmp_path) -> None:
    """Tier 1: shutdown as first message → (None, None) sentinel returned.

    Contract: the run-loop signals shutdown by checking ``trigger is None``.
    """
    session = _make_session(tmp_path)

    # Shutdown is enqueued out-of-band (no WAL, no _msg_id).
    await session.inbox.put(("shutdown", {}))

    ride_alongs, trigger = await session._drain_to_wake()

    assert ride_alongs is None, (
        f"Shutdown must return (None, None) not ({ride_alongs!r}, ...)"
    )
    assert trigger is None, (
        f"Shutdown must return (None, None) not (..., {trigger!r})"
    )


@pytest.mark.asyncio
async def test_drain_to_wake_inbox_consume_per_message(tmp_path) -> None:
    """Tier 1: inbox_consume recorded per drained message (ride-alongs + trigger).

    Contract (P6 + crash-recovery): each dequeued message — whether a
    ride-along or the trigger — must produce a WAL inbox_consume event so
    the snapshot stays pruned correctly on crash+restore.

    Verified via StateLog.iter_from() (public API, not private state).
    """
    session = _make_session(tmp_path)

    # One ride-along (wake=false) + one trigger (wake=true).
    await session._put_inbox("user", {"text": "r1", "wake": False})
    await session._put_inbox("user", {"text": "t1", "wake": True})

    # Drain: should consume both and record 2 inbox_consume WAL events.
    ride_alongs, trigger = await session._drain_to_wake()

    # Exactly 1 ride-along — unpack-enforcement idiom.
    (only_ra,) = ride_alongs
    assert only_ra[1].get("text") == "r1"

    assert trigger is not None

    events = _wal_events(tmp_path)
    consume_events = [e for e in events if e.get("kind") == "inbox_consume"]

    # 2 inbox_consume events: one for the ride-along, one for the trigger.
    n_consume = len(consume_events)
    assert n_consume == 2, (
        f"Expected 2 inbox_consume WAL events (1 ride-along + 1 trigger); "
        f"got {n_consume}: {consume_events}"
    )

    # Snapshot inbox must be empty: both messages consumed.
    snapshot = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    assert snapshot.inbox == [], (
        f"Snapshot inbox must be empty after drain; got {snapshot.inbox}"
    )


@pytest.mark.asyncio
async def test_drain_to_wake_only_wake_false_then_trigger_arrives(
    tmp_path,
) -> None:
    """Tier 1: wake=false messages alone do not trigger a turn; drain waits for trigger.

    Decision A (RESOLVED, issuecomment-4773744053): if the queue empties
    holding only wake=false ride-alongs, _drain_to_wake re-enters the
    blocking wait rather than running a turn on ride-alongs alone.

    This test enqueues two wake=false messages, then after a small delay
    enqueues one wake=true trigger from a background task, and verifies the
    drain correctly waited and returned the trigger (not a premature return).
    """
    session = _make_session(tmp_path)

    await session._put_inbox("user", {"text": "ra1", "wake": False})
    await session._put_inbox("user", {"text": "ra2", "wake": False})

    async def _delayed_trigger() -> None:
        await asyncio.sleep(0.02)
        await session._put_inbox("user", {"text": "final", "wake": True})

    trigger_task = asyncio.create_task(_delayed_trigger())

    ride_alongs, trigger = await asyncio.wait_for(
        session._drain_to_wake(), timeout=2.0
    )

    await trigger_task

    # Exactly 2 ride-alongs — unpack-enforcement idiom.
    ra1, ra2 = ride_alongs

    assert ra1[1].get("text") == "ra1"
    assert ra2[1].get("text") == "ra2"

    assert trigger is not None
    kind, payload = trigger
    assert payload.get("text") == "final", (
        f"Expected trigger text='final'; got {payload}"
    )

    # WAL must record inbox_consume for all 3 messages (2 ride-alongs + 1 trigger).
    events = _wal_events(tmp_path)
    consume_events = [e for e in events if e.get("kind") == "inbox_consume"]
    n_consume = len(consume_events)
    assert n_consume == 3, (
        f"Expected 3 inbox_consume WAL events; got {n_consume}"
    )


# ---------------------------------------------------------------------------
# Internal stub helpers
# ---------------------------------------------------------------------------


def _make_llm_stub_fn(result):  # type: ignore[no-untyped-def]
    """Return a real async callable that mimics call_llm_tools.

    Accepts a single LLMToolCallResult or a list (round-robin).
    No unittest.mock per policy.
    """
    if isinstance(result, list):
        results = list(result)
        idx = [0]

        async def _stub(**kwargs) -> LLMToolCallResult:  # noqa: ANN202
            i = idx[0]
            idx[0] += 1
            return results[i] if i < len(results) else results[-1]

        return _stub

    async def _stub(**kwargs) -> LLMToolCallResult:  # noqa: ANN202
        return result

    return _stub
