"""Tier 2: OS invariant — #2103 S2b agent-lifecycle EMIT seam (action layer).

The emit side that feeds the S2a consume side (#2117): the action-layer seams
`AgentRegistry.create_agent()` / `archive_agent()` emit agent_created /
agent_archived / agent_purged so rewind can track / reconstruct / drop the agent
across its full lifecycle. create()/remove() stay SYNC (the mechanism); the seams
are the ONE place the emit lives (every creation/deletion surface routes through
them — no scatter). Real AgentRegistry + StateLog (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _put(log: StateLog, agent: str, text: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


def _events(reg: AgentRegistry, kind: str) -> list[dict]:
    return [e for e in reg.state_log.iter_from(0) if e.get("kind") == kind]


def _agent_dir(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name


@pytest.mark.asyncio
async def test_create_agent_emits_agent_created_with_config(tmp_path):
    """Tier 2: create_agent creates the profile (on disk) AND emits agent_created
    carrying the profile config — the record rewind re-materialises from."""
    reg = _make_registry(tmp_path)
    await reg.create_agent("helper", role="my role")
    await reg._state_log.flush()  # #2259 PR-2b: create_agent's agent_created append is async

    assert _agent_dir(tmp_path, "helper").is_dir()
    created = _events(reg, "agent_created")
    assert [e.get("name") for e in created] == ["helper"]
    assert created[0]["profile"]["role"] == "my role"


@pytest.mark.asyncio
async def test_created_agent_dropped_on_rewind_before_create(tmp_path):
    """Tier 2: an agent created via create_agent (emit) is dropped by a rewind to
    before its create (consume) — the emit→consume path end-to-end."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "survivor")
    await _put(reg.state_log, "survivor", "s1")     # seq 1 (the cut target)
    await reg.create_agent("ephemeral", role="task")  # seq 2 — agent_created

    assert _agent_dir(tmp_path, "ephemeral").is_dir()
    await reg.rewind_to(1)                            # cut 1 < create-seq 2

    assert not _agent_dir(tmp_path, "ephemeral").exists()   # dropped
    assert "survivor" in reg.list_names()


@pytest.mark.asyncio
async def test_archive_agent_emits_agent_archived(tmp_path):
    """Tier 2: archive_agent emits agent_archived (the rewind-reconstruction
    source) and the agent is archived (hidden from the active listing, kept on
    disk)."""
    reg = _make_registry(tmp_path)
    await reg.create_agent("a")
    await reg.archive_agent("a")

    assert [e.get("name") for e in _events(reg, "agent_archived")] == ["a"]
    assert "a" not in reg.list_active_names()
    assert "a" in reg.list_names()


@pytest.mark.asyncio
async def test_archive_agent_purge_emits_agent_purged(tmp_path):
    """Tier 2: archive_agent(purge=True) emits agent_purged and hard-deletes the
    agent dir (the permanent-purge action, fork A)."""
    reg = _make_registry(tmp_path)
    await reg.create_agent("a")
    await reg.archive_agent("a", purge=True)

    assert [e.get("name") for e in _events(reg, "agent_purged")] == ["a"]
    assert not _agent_dir(tmp_path, "a").exists()
