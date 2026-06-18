"""Tier 2: OS invariant — ChainManager.find_chain read-only lookup (R-D14).

Background: cross-agent chain notification (R-D14) needs a way to ask
"does this ChainManager track chain X?" without mutating state. This
is the read-only counterpart to ``resolve`` (which pops + WALs).

Pinned invariants:
  - find_chain returns the live _PendingChain when the chain is tracked
  - find_chain returns None when the chain is unknown
  - find_chain does NOT mutate state — repeat lookups are stable
  - find_chain does NOT emit WAL events

Reference: PR-discard-chain-notify (R-D14) D14.2 in the active plan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.services.chain_manager import ChainManager, _PendingChain
from reyn.runtime.services.snapshot_journal import SnapshotJournal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NullEvents:
    def emit(self, *_args, **_kwargs) -> None:
        pass


def _make_manager(tmp_path: Path) -> tuple[ChainManager, StateLog]:
    log = StateLog(tmp_path / "wal.jsonl")
    journal = SnapshotJournal(
        agent_name="alpha",
        snapshot_path=tmp_path / "snap.json",
        state_log=log,
    )
    mgr = ChainManager(
        journal=journal,
        events=_NullEvents(),
        chain_timeout_seconds=0,  # disable watchdog
        max_hop_depth=10,
    )
    return mgr, log


# ---------------------------------------------------------------------------
# Read-only lookup behaviour
# ---------------------------------------------------------------------------


def test_find_chain_returns_pending_when_registered(tmp_path: Path):
    """Tier 2: a registered chain is returned by find_chain."""
    mgr, _ = _make_manager(tmp_path)

    async def go():
        await mgr.register(
            chain_id="X-001",
            from_user=False,
            depth=1,
            original_text="hello",
            sender="caller",
            waiting_on={"B"},
            origin_agent="A",
            origin_depth=1,
        )

    asyncio.run(go())
    found = mgr.find_chain("X-001")
    assert isinstance(found, _PendingChain)
    assert found.chain_id == "X-001"
    assert found.origin_agent == "A"
    assert found.waiting_on == {"B"}


def test_find_chain_returns_none_when_unknown(tmp_path: Path):
    """Tier 2: an unknown chain_id returns None (no exception)."""
    mgr, _ = _make_manager(tmp_path)
    assert mgr.find_chain("unknown-chain") is None


def test_find_chain_is_read_only(tmp_path: Path):
    """Tier 2: repeat find_chain calls are stable; nothing is mutated."""
    mgr, log = _make_manager(tmp_path)

    async def go():
        await mgr.register(
            chain_id="X-002",
            from_user=False,
            depth=1,
            original_text="hi",
            sender="caller",
            waiting_on={"B"},
            origin_agent="A",
            origin_depth=1,
        )

    asyncio.run(go())
    # Capture the WAL state after register
    events_before = list(log.iter_from(0))
    # Repeat find_chain — must not change anything
    for _ in range(5):
        found = mgr.find_chain("X-002")
        assert found is not None
    events_after = list(log.iter_from(0))
    assert events_after == events_before, (
        "find_chain must not append WAL events"
    )
    # Chain still tracked
    assert mgr.find_chain("X-002") is not None


def test_find_chain_returns_none_after_resolve(tmp_path: Path):
    """Tier 2: resolved chains drop out of find_chain."""
    mgr, _ = _make_manager(tmp_path)

    async def go():
        await mgr.register(
            chain_id="X-003",
            from_user=False,
            depth=1,
            original_text="hi",
            sender="caller",
            waiting_on={"B"},
            origin_agent="A",
            origin_depth=1,
        )
        await mgr.resolve("X-003")

    asyncio.run(go())
    assert mgr.find_chain("X-003") is None
