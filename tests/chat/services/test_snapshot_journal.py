"""Unit tests for SnapshotJournal (wave 1A extraction).

Follows PR21 test philosophy: StateLog uses real instances via tmp_path;
no mocks for core persistence objects.
"""
from __future__ import annotations

import json

import pytest

from reyn.chat.services.snapshot_journal import SnapshotJournal
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog

# ── helpers ──────────────────────────────────────────────────────────────────


def make_journal(tmp_path, *, with_state_log: bool = True) -> SnapshotJournal:
    snapshot_path = tmp_path / "snapshot.json"
    state_log = StateLog(tmp_path / "state.wal") if with_state_log else None
    return SnapshotJournal(
        agent_name="test_agent",
        snapshot_path=snapshot_path,
        state_log=state_log,
    )


# ── tests: no-WAL mode (state_log=None) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_append_inbox_no_wal_skips_persist(tmp_path):
    """Tier 2b: With state_log=None, append_inbox still returns a msg_id but writes nothing."""
    j = make_journal(tmp_path, with_state_log=False)
    snapshot_path = tmp_path / "snapshot.json"

    msg_id = await j.append_inbox(kind="user_message", payload={"text": "hi"})

    assert isinstance(msg_id, str)
    # snapshot.inbox should be empty — no in-memory update without WAL
    assert j.snapshot.inbox == []
    # no snapshot file should have been written
    assert not snapshot_path.exists()


@pytest.mark.asyncio
async def test_consume_inbox_no_wal_is_noop(tmp_path):
    """Tier 2b: With state_log=None, consume_inbox is a silent no-op."""
    j = make_journal(tmp_path, with_state_log=False)
    # should not raise
    await j.consume_inbox(msg_id="deadbeef")
    assert j.snapshot.applied_seq == 0


@pytest.mark.asyncio
async def test_chain_methods_no_wal_are_noops(tmp_path):
    """Tier 2b: All chain methods are no-ops without a state_log."""
    j = make_journal(tmp_path, with_state_log=False)
    await j.record_chain_register(
        chain_id="c1",
        fields={
            "origin_agent": "parent",
            "origin_depth": 1,
            "original_request": "do x",
            "waiting_on": ["sub1"],
        },
    )
    assert j.snapshot.pending_chains == {}
    await j.record_chain_update(chain_id="c1", fields={"waiting_on": []})
    await j.record_chain_resolve(chain_id="c1")
    await j.record_chain_timeout_fired(chain_id="c1")
    assert j.snapshot.applied_seq == 0


# ── tests: WAL-backed mode ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_append_inbox_returns_msg_id_and_updates_snapshot(tmp_path):
    """Tier 2b: append_inbox writes WAL, updates snapshot.inbox, returns msg_id."""
    j = make_journal(tmp_path)

    msg_id = await j.append_inbox(kind="user_message", payload={"text": "hello"})

    assert isinstance(msg_id, str)
    entry = j.snapshot.inbox[0]
    assert entry["id"] == msg_id
    assert entry["kind"] == "user_message"
    assert entry["payload"]["_msg_id"] == msg_id
    assert j.snapshot.applied_seq >= 1


@pytest.mark.asyncio
async def test_consume_inbox_removes_entry_from_snapshot(tmp_path):
    """Tier 2b: consume_inbox prunes the matching inbox entry by msg_id."""
    j = make_journal(tmp_path)

    msg_id = await j.append_inbox(kind="user_message", payload={"text": "ping"})

    await j.consume_inbox(msg_id=msg_id)

    assert j.snapshot.inbox == []
    assert j.snapshot.applied_seq >= 2


@pytest.mark.asyncio
async def test_chain_register_adds_to_pending_chains(tmp_path):
    """Tier 2b: record_chain_register populates pending_chains with all metadata."""
    j = make_journal(tmp_path)
    fields = {
        "origin_agent": "root",
        "origin_depth": 0,
        "original_request": "please help",
        "waiting_on": ["child_a", "child_b"],
    }

    await j.record_chain_register(chain_id="chain-1", fields=fields)

    assert "chain-1" in j.snapshot.pending_chains
    chain = j.snapshot.pending_chains["chain-1"]
    assert chain["chain_id"] == "chain-1"
    assert chain["origin_agent"] == "root"
    assert chain["waiting_on"] == ["child_a", "child_b"]
    assert j.snapshot.applied_seq >= 1


@pytest.mark.asyncio
async def test_chain_update_modifies_waiting_on(tmp_path):
    """Tier 2b: record_chain_update replaces waiting_on in the pending_chains entry."""
    j = make_journal(tmp_path)
    await j.record_chain_register(
        chain_id="chain-2",
        fields={
            "origin_agent": "root",
            "origin_depth": 0,
            "original_request": "task",
            "waiting_on": ["a", "b"],
        },
    )

    await j.record_chain_update(chain_id="chain-2", fields={"waiting_on": ["b"]})

    assert j.snapshot.pending_chains["chain-2"]["waiting_on"] == ["b"]


@pytest.mark.asyncio
async def test_chain_resolve_removes_from_pending_chains(tmp_path):
    """Tier 2b: record_chain_resolve pops the chain entry."""
    j = make_journal(tmp_path)
    await j.record_chain_register(
        chain_id="chain-3",
        fields={
            "origin_agent": "root",
            "origin_depth": 0,
            "original_request": "work",
            "waiting_on": [],
        },
    )
    assert "chain-3" in j.snapshot.pending_chains

    await j.record_chain_resolve(chain_id="chain-3")

    assert "chain-3" not in j.snapshot.pending_chains


@pytest.mark.asyncio
async def test_chain_timeout_fired_removes_from_pending_chains(tmp_path):
    """Tier 2b: record_chain_timeout_fired pops the chain entry."""
    j = make_journal(tmp_path)
    await j.record_chain_register(
        chain_id="chain-4",
        fields={
            "origin_agent": "root",
            "origin_depth": 1,
            "original_request": "stale task",
            "waiting_on": ["slow_child"],
        },
    )

    await j.record_chain_timeout_fired(chain_id="chain-4")

    assert "chain-4" not in j.snapshot.pending_chains


@pytest.mark.asyncio
async def test_install_replaces_snapshot_and_persists(tmp_path):
    """Tier 2b: install() swaps in a recovered snapshot and writes it to disk."""
    j = make_journal(tmp_path)
    snapshot_path = tmp_path / "snapshot.json"

    external = AgentSnapshot(
        agent_name="test_agent",
        applied_seq=42,
        inbox=[{"id": "aabbccdd", "kind": "ping", "payload": {}}],
        pending_chains={},
    )

    j.install(external)

    assert j.snapshot.applied_seq == 42
    assert j.snapshot.inbox[0]["id"] == "aabbccdd"
    assert snapshot_path.exists()
    persisted = json.loads(snapshot_path.read_text())
    assert persisted["applied_seq"] == 42


@pytest.mark.asyncio
async def test_save_writes_snapshot_to_disk(tmp_path):
    """Tier 2b: save() persists current in-memory snapshot atomically."""
    j = make_journal(tmp_path)
    snapshot_path = tmp_path / "snapshot.json"

    await j.append_inbox(kind="user_message", payload={"x": 1})
    # save() is called internally; check it round-trips correctly
    persisted = json.loads(snapshot_path.read_text())
    assert persisted["inbox"] != []
    assert persisted["applied_seq"] >= 1


@pytest.mark.asyncio
async def test_applied_seq_monotonically_increases(tmp_path):
    """Tier 2b: Each WAL-recorded operation strictly increases applied_seq."""
    j = make_journal(tmp_path)
    seqs: list[int] = []

    await j.append_inbox(kind="msg", payload={})
    seqs.append(j.snapshot.applied_seq)

    msg_id = j.snapshot.inbox[0]["id"]
    await j.consume_inbox(msg_id=msg_id)
    seqs.append(j.snapshot.applied_seq)

    await j.record_chain_register(
        chain_id="cx",
        fields={
            "origin_agent": "root",
            "origin_depth": 0,
            "original_request": "go",
            "waiting_on": ["x"],
        },
    )
    seqs.append(j.snapshot.applied_seq)

    await j.record_chain_resolve(chain_id="cx")
    seqs.append(j.snapshot.applied_seq)

    # strict monotonic increase
    for i in range(1, len(seqs)):
        assert seqs[i] > seqs[i - 1], (
            f"seq did not increase between op {i-1} and {i}: {seqs}"
        )
