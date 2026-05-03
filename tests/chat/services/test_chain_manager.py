"""Unit tests for ChainManager (wave 1B extraction).

Journal interactions are verified via AsyncMock spies; the snapshot is backed
by a MagicMock so no real WAL or filesystem is needed.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from reyn.chat.services.chain_manager import ChainManager, _PendingChain
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.events import EventLog


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_journal(pending_chains: dict | None = None) -> MagicMock:
    """Return a mock that satisfies _JournalLike.

    ``record_chain_*`` methods are AsyncMock so ``await`` works.
    ``snapshot`` returns a real AgentSnapshot with controllable pending_chains.
    """
    snapshot = AgentSnapshot(agent_name="test-agent")
    if pending_chains:
        snapshot.pending_chains = pending_chains

    journal = MagicMock()
    journal.snapshot = snapshot
    journal.record_chain_register = AsyncMock()
    journal.record_chain_update = AsyncMock()
    journal.record_chain_resolve = AsyncMock()
    journal.record_chain_timeout_fired = AsyncMock()
    return journal


def _make_manager(
    *,
    journal=None,
    timeout: float = 60.0,
    max_hop_depth: int = 5,
) -> ChainManager:
    if journal is None:
        journal = _make_journal()
    events = EventLog()
    return ChainManager(
        journal=journal,
        events=events,
        chain_timeout_seconds=timeout,
        max_hop_depth=max_hop_depth,
    )


# ── register ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_adds_chain_to_internal_dict():
    mgr = _make_manager()
    chain = await mgr.register(
        chain_id="c1",
        from_user=False,
        depth=1,
        original_text="hello",
        sender="agent-a",
        waiting_on={"agent-b"},
        origin_agent="agent-a",
        origin_depth=1,
    )
    assert isinstance(chain, _PendingChain)
    assert mgr.has("c1")
    assert mgr.get("c1") is chain
    assert "agent-b" in chain.waiting_on


@pytest.mark.asyncio
async def test_register_calls_journal_record_chain_register():
    journal = _make_journal()
    mgr = _make_manager(journal=journal)
    await mgr.register(
        chain_id="c1",
        from_user=True,
        depth=0,
        original_text="hi",
        sender=None,
        waiting_on={"x"},
        origin_agent="upstream",
        origin_depth=0,
    )
    journal.record_chain_register.assert_awaited_once()
    kwargs = journal.record_chain_register.call_args.kwargs
    assert kwargs["chain_id"] == "c1"
    assert "origin_agent" in kwargs["fields"]


# ── has / get / all_chain_ids ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_has_returns_false_for_missing_chain():
    mgr = _make_manager()
    assert not mgr.has("nonexistent")


@pytest.mark.asyncio
async def test_get_returns_none_for_missing_chain():
    mgr = _make_manager()
    assert mgr.get("nonexistent") is None


@pytest.mark.asyncio
async def test_all_chain_ids_reflects_registered_chains():
    mgr = _make_manager()
    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", origin_agent="a", origin_depth=1)
    await mgr.register(chain_id="c2", from_user=False, depth=1,
                       original_text="t", sender="b", origin_agent="b", origin_depth=1)
    ids = mgr.all_chain_ids()
    assert set(ids) == {"c1", "c2"}


# ── update ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_mutates_waiting_on_in_memory():
    mgr = _make_manager()
    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", waiting_on={"x", "y"},
                       origin_agent="a", origin_depth=1)
    await mgr.update("c1", waiting_on={"x"})
    assert mgr.get("c1").waiting_on == {"x"}


@pytest.mark.asyncio
async def test_update_calls_journal_record_chain_update():
    journal = _make_journal()
    mgr = _make_manager(journal=journal)
    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", waiting_on={"x"},
                       origin_agent="a", origin_depth=1)
    journal.record_chain_update.reset_mock()

    await mgr.update("c1", waiting_on={"y"})
    journal.record_chain_update.assert_awaited_once()
    kwargs = journal.record_chain_update.call_args.kwargs
    assert kwargs["chain_id"] == "c1"
    assert "waiting_on" in kwargs["fields"]


@pytest.mark.asyncio
async def test_update_noop_for_missing_chain():
    journal = _make_journal()
    mgr = _make_manager(journal=journal)
    # Should not raise; journal should not be called.
    await mgr.update("no-such-chain", waiting_on={"z"})
    journal.record_chain_update.assert_not_awaited()


# ── resolve ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_removes_chain_and_returns_it():
    mgr = _make_manager()
    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", origin_agent="a", origin_depth=1)
    resolved = await mgr.resolve("c1")
    assert isinstance(resolved, _PendingChain)
    assert resolved.chain_id == "c1"
    assert not mgr.has("c1")


@pytest.mark.asyncio
async def test_resolve_calls_journal_record_chain_resolve():
    journal = _make_journal()
    mgr = _make_manager(journal=journal)
    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", origin_agent="a", origin_depth=1)
    await mgr.resolve("c1")
    journal.record_chain_resolve.assert_awaited_once_with(chain_id="c1")


@pytest.mark.asyncio
async def test_resolve_cancels_timer():
    mgr = _make_manager(timeout=60.0)
    fired: list[str] = []

    async def on_fire(cid: str) -> None:
        fired.append(cid)

    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", origin_agent="a", origin_depth=1)
    mgr.arm_timeout("c1", on_fire=on_fire)
    assert "c1" in mgr._timers
    await mgr.resolve("c1")
    # Timer should be gone after resolve.
    assert "c1" not in mgr._timers
    assert fired == []


@pytest.mark.asyncio
async def test_resolve_returns_none_for_missing_chain():
    mgr = _make_manager()
    result = await mgr.resolve("nonexistent")
    assert result is None


# ── fire_timeout ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_timeout_removes_chain_and_returns_it():
    mgr = _make_manager()
    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", origin_agent="a", origin_depth=1)
    result = await mgr.fire_timeout("c1")
    assert isinstance(result, _PendingChain)
    assert not mgr.has("c1")


@pytest.mark.asyncio
async def test_fire_timeout_calls_journal_record_chain_timeout_fired():
    journal = _make_journal()
    mgr = _make_manager(journal=journal)
    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", origin_agent="a", origin_depth=1)
    await mgr.fire_timeout("c1")
    journal.record_chain_timeout_fired.assert_awaited_once_with(chain_id="c1")


# ── arm_timeout / cancel_timeout ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_arm_timeout_fires_callback_after_delay():
    mgr = _make_manager(timeout=0.05)
    fired: list[str] = []

    async def on_fire(cid: str) -> None:
        fired.append(cid)
        # Simulate the typical fire_timeout call inside the callback.
        await mgr.fire_timeout(cid)

    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", origin_agent="a", origin_depth=1)
    mgr.arm_timeout("c1", on_fire=on_fire)

    await asyncio.wait_for(
        _wait_until(lambda: "c1" in fired),
        timeout=2.0,
    )
    assert fired == ["c1"]


@pytest.mark.asyncio
async def test_cancel_timeout_prevents_callback():
    mgr = _make_manager(timeout=0.05)
    fired: list[str] = []

    async def on_fire(cid: str) -> None:
        fired.append(cid)

    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", origin_agent="a", origin_depth=1)
    mgr.arm_timeout("c1", on_fire=on_fire)
    mgr.cancel_timeout("c1")

    # Wait a bit longer than the timeout to be sure it didn't fire.
    await asyncio.sleep(0.15)
    assert fired == []


@pytest.mark.asyncio
async def test_arm_timeout_noop_when_timeout_disabled():
    mgr = _make_manager(timeout=0.0)
    fired: list[str] = []

    async def on_fire(cid: str) -> None:
        fired.append(cid)

    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", origin_agent="a", origin_depth=1)
    mgr.arm_timeout("c1", on_fire=on_fire)
    assert "c1" not in mgr._timers

    await asyncio.sleep(0.05)
    assert fired == []


@pytest.mark.asyncio
async def test_rearm_cancels_previous_timer():
    """Re-arming the same chain_id cancels the old timer before creating a new one."""
    mgr = _make_manager(timeout=60.0)
    fired: list[str] = []

    async def on_fire(cid: str) -> None:
        fired.append(cid)

    await mgr.register(chain_id="c1", from_user=False, depth=1,
                       original_text="t", sender="a", origin_agent="a", origin_depth=1)
    mgr.arm_timeout("c1", on_fire=on_fire)
    task1 = mgr._timers["c1"]

    mgr.arm_timeout("c1", on_fire=on_fire)
    task2 = mgr._timers["c1"]

    assert task1 is not task2
    # Give the event loop a tick so the CancelledError propagates.
    await asyncio.sleep(0)
    assert task1.cancelled()


# ── restore ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restore_reconstructs_pending_chains_from_snapshot():
    pending_chains = {
        "c1": {
            "chain_id": "c1",
            "origin_agent": "upstream",
            "origin_depth": 2,
            "original_request": "do stuff",
            "waiting_on": ["agent-x", "agent-y"],
        }
    }
    journal = _make_journal(pending_chains=pending_chains)
    mgr = _make_manager(journal=journal, timeout=0.0)

    async def on_fire(cid: str) -> None:
        pass

    mgr.restore(on_fire=on_fire)

    assert mgr.has("c1")
    chain = mgr.get("c1")
    assert chain.origin_agent == "upstream"
    assert chain.origin_depth == 2
    assert chain.waiting_on == {"agent-x", "agent-y"}


@pytest.mark.asyncio
async def test_restore_arms_timeout_for_each_chain():
    pending_chains = {
        "c1": {
            "chain_id": "c1",
            "origin_agent": "up",
            "origin_depth": 1,
            "original_request": "req",
            "waiting_on": [],
        }
    }
    journal = _make_journal(pending_chains=pending_chains)
    # Use non-zero timeout so arm_timeout actually creates a task.
    mgr = _make_manager(journal=journal, timeout=60.0)

    fired: list[str] = []

    async def on_fire(cid: str) -> None:
        fired.append(cid)

    mgr.restore(on_fire=on_fire)
    assert "c1" in mgr._timers
    # Cleanup
    mgr.cancel_timeout("c1")


# ── helpers ───────────────────────────────────────────────────────────────────


async def _wait_until(predicate, *, poll_interval: float = 0.01) -> None:
    """Poll until predicate() returns True."""
    while not predicate():
        await asyncio.sleep(poll_interval)
