"""Tier 2: OS invariant — #2103 S2 agent-lifecycle rewind-reconstruction (consume).

On the merged #2114 drop-primitive foundation. S2 reconstructs agent EXISTENCE
as-of-cut from the lifecycle WAL events:
- agent_created → re-materialise (≤cut, absent) or drop (>cut, foundation).
- agent_archived → hide-as-of-cut (rewrite the .archived tombstone).
- agent_purged → PERMANENT (fork A, owner-decided): never re-materialised at ANY
  cut (preserves the #1954 irreversible-purge / privacy intent).

This is the CONSUME side — inert in production until S2b emits the events; here
driven by emitting the (now-real) lifecycle WAL kinds directly. Real AgentRegistry
+ StateLog + on-disk agents (no mocks). _materialize_rewind is driven directly for
precise as-of-cut control (the same path rewind_to + crash-recovery share).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import ARCHIVED_MARKER, AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    # default create_event_kinds → includes "agent_created" (_LIFECYCLE_CREATE_KINDS).
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str, role: str = "") -> None:
    AgentProfile.new(name, role=role).save(tmp_path / ".reyn" / "agents" / name)


def _agent_dir(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name


async def _emit_created(log: StateLog, name: str, *, role: str = "") -> int:
    return await log.append(
        "agent_created", entity_kind="agent", name=name, sid="",
        profile={"name": name, "role": role},
    )


async def _emit_archived(log: StateLog, name: str) -> int:
    return await log.append("agent_archived", entity_kind="agent", name=name)


async def _emit_purged(log: StateLog, name: str) -> int:
    return await log.append("agent_purged", entity_kind="agent", name=name)


@pytest.mark.asyncio
async def test_dropped_agent_rematerialised_from_agent_created(tmp_path):
    """Tier 2: a dropped agent is RE-MATERIALISED from its agent_created record when
    the cut is at-or-after its create-seq (the reversibility headline) — profile
    restored, so a forward-checkout-past-drop brings the agent back."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "helper", role="my role")
    log = reg.state_log
    await _emit_created(log, "helper", role="my role")   # seq 1

    reg._drop_agent("helper")                             # simulate prior-cut drop
    assert not _agent_dir(tmp_path, "helper").exists()

    await reg._materialize_rewind(
        reconstruct_seq=log.current_seq, workspace_at_or_below=1,  # cut ≥ create
    )

    assert _agent_dir(tmp_path, "helper").is_dir()        # re-materialised
    assert AgentProfile.load(_agent_dir(tmp_path, "helper")).role == "my role"


@pytest.mark.asyncio
async def test_archived_state_reconciled_as_of_cut(tmp_path):
    """Tier 2: the .archived tombstone is rewritten to the as-of-cut archived-state —
    rewind-before-archive → active (cleared); rewind-after → archived (present)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "a")
    log = reg.state_log
    await _emit_created(log, "a")        # seq 1
    await _emit_archived(log, "a")       # seq 2

    # cut before the archive → active (marker cleared).
    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=1)
    assert not (_agent_dir(tmp_path, "a") / ARCHIVED_MARKER).exists()

    # cut at/after the archive → archived (marker present).
    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=2)
    assert (_agent_dir(tmp_path, "a") / ARCHIVED_MARKER).is_file()


@pytest.mark.asyncio
async def test_purged_agent_is_permanent_never_rematerialised(tmp_path):
    """Tier 2: fork A — a purged agent is PERMANENT, not re-materialised at ANY cut,
    even rewind-to-before-the-purge (preserves the #1954 irreversible-purge intent)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "v")
    log = reg.state_log
    await _emit_created(log, "v")        # seq 1
    await _emit_purged(log, "v")         # seq 2

    # cut BEFORE the purge (1) — fork A still drops it (out-of-time-travel).
    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=1)
    assert not _agent_dir(tmp_path, "v").exists()


@pytest.mark.asyncio
async def test_agent_without_lifecycle_events_unaffected(tmp_path):
    """Tier 2: no-op — an agent with NO lifecycle events is neither dropped nor
    re-materialised — reconstruct is byte-identical to pre-S2."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "keep")
    log = reg.state_log
    await log.append("inbox_put", target="keep", msg_id="x", msg_kind="user",
                     payload={"text": "x"})           # seq 1 (non-lifecycle)

    await reg._materialize_rewind(reconstruct_seq=log.current_seq, workspace_at_or_below=1)

    assert _agent_dir(tmp_path, "keep").is_dir()       # untouched
