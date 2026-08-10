"""Tier 2: RunRegistry contract — create / update / get round-trips.

Extracted from test_a2a_sync_async_escalation.py when the A2A sync→task
escalation path (B42-NF-W6-2) was removed along with the skill-execution
machinery in stage3.5. RunRegistry itself is still live (used by async-mode
A2A tasks).
"""
from __future__ import annotations

from reyn.interfaces.web.run_registry import RunEntry, RunRegistry


def test_run_registry_create_and_update_terminal_status():
    """Tier 2: RunRegistry.update() transitions entry from running → completed."""
    reg = RunRegistry()
    entry = reg.create(agent_name="a", chain_id="c1")
    assert entry.status == "running"
    assert entry.result is None

    reg.update(entry.run_id, status="completed", result="all done")

    fresh = reg.get(entry.run_id)
    assert fresh is not None
    assert fresh.status == "completed"
    assert fresh.result == "all done"


def test_run_registry_update_failure_path_sets_error():
    """Tier 2: RunRegistry.update() with status=failed records the error string."""
    reg = RunRegistry()
    entry = reg.create(agent_name="a", chain_id="c2")
    reg.update(entry.run_id, status="failed", error="something exploded")

    fresh = reg.get(entry.run_id)
    assert fresh is not None
    assert fresh.status == "failed"
    assert fresh.error == "something exploded"


def test_run_entry_session_id_persist_round_trip():
    """Tier 2: RunEntry.session_id survives a persist round-trip with a
    NON-DEFAULT value (so a silently-dropped field can't pass), and pre-#1814
    snapshots without the key load gracefully (None).
    """
    reg = RunRegistry()
    entry = reg.create(agent_name="a", chain_id="c", session_id="a2a:ctx-7")
    restored = RunEntry.from_persist_dict(entry.to_persist_dict())
    assert restored.session_id == "a2a:ctx-7"  # non-default preserved
    legacy = RunEntry.from_persist_dict(
        {"run_id": "r", "agent_name": "a", "chain_id": "c"}
    )
    assert legacy.session_id is None  # graceful for pre-#1814 snapshots
