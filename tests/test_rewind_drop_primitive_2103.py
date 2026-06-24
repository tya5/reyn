"""Tier 2: OS invariant — #2103 as-of-cut DROP primitive (rewind-reconstruction).

The foundation for the spawn-primitive rewind-integration: on reconstruct-to-cut,
an entity whose create-event WAL seq > the rewind target DID NOT exist as-of-cut,
so it is torn down (dropped) instead of lingering as an empty-snapshot orphan on
disk. The create-side INVERSE of the #1954 archive (delete-side HIDE).

Standalone slice: driven by a SYNTHETIC create-event kind (no dependency on the
real session_spawned/agent_created, which register into the same seam in S1bc/S2).
Real AgentRegistry + StateLog + on-disk agents (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events import state_log as state_log_mod
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry

_SYNTH_CREATE = "test_create_event"


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
        create_event_kinds=frozenset({_SYNTH_CREATE}),
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def _allow_synth_kind(monkeypatch) -> None:
    # Permit appending the synthetic create-event (state_log.append gates on the
    # module-level WAL_EVENT_KINDS allowlist). pytest monkeypatch — not a mock.
    monkeypatch.setattr(
        state_log_mod, "WAL_EVENT_KINDS",
        tuple(state_log_mod.WAL_EVENT_KINDS) + (_SYNTH_CREATE,),
    )


async def _put(log: StateLog, agent: str, text: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


async def _emit_create(log: StateLog, name: str, *, sid: str = "") -> int:
    kind = "session" if sid else "agent"
    return await log.append(_SYNTH_CREATE, entity_kind=kind, name=name, sid=sid)


def _agent_dir(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name


@pytest.mark.asyncio
async def test_agent_created_after_cut_is_dropped(tmp_path, monkeypatch):
    """Tier 2: rewind-to-before an agent's create DROPS it (the headline) — it
    didn't exist as-of-cut, so it is torn down, not left as an empty-snapshot
    orphan. Asserted against the real rewind_to → _materialize_rewind path."""
    _allow_synth_kind(monkeypatch)
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "survivor")
    _seed_agent(tmp_path, "victim")
    log = reg.state_log
    await _put(log, "survivor", "s1")        # seq 1 (the rewind target)
    await _emit_create(log, "victim")        # seq 2 — victim created AFTER the cut
    await _put(log, "victim", "v1")          # seq 3

    result = await reg.rewind_to(1)          # cut = 1 < victim's create-seq 2

    assert not _agent_dir(tmp_path, "victim").exists()   # dropped
    assert "victim" not in reg.list_names()
    assert "survivor" in result["agents"]                # untouched
    assert _agent_dir(tmp_path, "survivor").is_dir()


@pytest.mark.asyncio
async def test_agent_drop_subsumes_its_sessions(tmp_path, monkeypatch):
    """Tier 2: dropping a post-cut agent tears down its sessions too (they nest
    under the agent dir) — no orphaned session left behind."""
    _allow_synth_kind(monkeypatch)
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "victim")
    sess_dir = _agent_dir(tmp_path, "victim") / "state" / "sessions" / "task1"
    sess_dir.mkdir(parents=True)
    (sess_dir / "snapshot.json").write_text("{}", encoding="utf-8")
    log = reg.state_log
    await _put(log, "victim", "pre")         # seq 1 (the rewind target)
    await _emit_create(log, "victim")        # seq 2 — created after the cut

    await reg.rewind_to(1)

    assert not _agent_dir(tmp_path, "victim").exists()   # agent gone
    assert not sess_dir.exists()                         # + its session subsumed


@pytest.mark.asyncio
async def test_agent_without_create_event_is_never_dropped(tmp_path, monkeypatch):
    """Tier 2: no-op safety — an agent with NO recorded create-event is never
    dropped — only entities with a create-seq > cut are torn down, so a
    pre-existing agent reconstructs as today."""
    _allow_synth_kind(monkeypatch)
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "kept")            # seeded; no create-event emitted
    log = reg.state_log
    await _put(log, "kept", "a")             # seq 1
    await _put(log, "kept", "b")             # seq 2

    result = await reg.rewind_to(1)

    assert _agent_dir(tmp_path, "kept").is_dir()   # not dropped (no create-event)
    assert "kept" in result["agents"]


@pytest.mark.asyncio
async def test_agent_created_at_or_before_cut_is_kept(tmp_path, monkeypatch):
    """Tier 2: boundary — an agent whose create-seq is at-or-below the cut existed
    as-of-cut → kept (reconstructed), not dropped."""
    _allow_synth_kind(monkeypatch)
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "victim")
    log = reg.state_log
    await _emit_create(log, "victim")        # seq 1 — created AT the cut
    await _put(log, "victim", "v1")          # seq 2

    result = await reg.rewind_to(1)          # cut == create-seq → existed as-of-cut

    assert _agent_dir(tmp_path, "victim").is_dir()   # kept
    assert "victim" in result["agents"]
