"""Tier 2: #2169 — the dogfood per-scenario reset wipes spawn-arc-created agents.

Spawn-arc scenarios (#2103 B/C) are the first dogfood scenarios that CREATE agents:
each writes ``.reyn/agents/<name>/profile.yaml`` (``researcher``/``alice``/``A``/…).
In single-run mode the project ``.reyn/`` is REUSED across runs, so those created
agents previously leaked — a re-run of an agent-creating scenario hit ``agent already
exists`` (``AgentRegistry.create`` raises when ``profile.yaml`` is present) → ``refuted``.
The fresh per-scenario reset now removes every created SIBLING agent dir while sparing
the dogfood TARGET agent (whose ``profile.yaml`` is a load precondition —
``get_or_load`` raises ``FileNotFoundError`` without it, which would block every
scenario). Real ``AgentProfile.save`` / ``PROFILE_FILENAME`` — the exact marker
``AgentRegistry.exists`` keys off — no mocks.
"""
from __future__ import annotations

from pathlib import Path

from reyn.interfaces.cli.commands.dogfood import (
    _leaked_agent_dirs,
    _wipe_leaked_agents,
)
from reyn.runtime.profile import PROFILE_FILENAME, AgentProfile


def _seed_agent(agents_root: Path, name: str) -> Path:
    """Write a real profile.yaml for ``name`` the way AgentRegistry.create does."""
    agent_dir = agents_root / name
    AgentProfile.new(name).save(agent_dir)
    assert (agent_dir / PROFILE_FILENAME).is_file()  # precondition: it exists now
    return agent_dir


def test_leaked_agent_dirs_selects_siblings_spares_target(tmp_path):
    """Tier 2: #2169 — the reset targets every created SIBLING agent dir and never
    the dogfood target (order-agnostic membership, so no sort-order pin)."""
    agents_root = tmp_path / ".reyn" / "agents"
    for name in ("default", "researcher", "alice", "child"):
        _seed_agent(agents_root, name)

    leaked = _leaked_agent_dirs(tmp_path, "default")

    assert {d.name for d in leaked} == {"researcher", "alice", "child"}
    assert all(d.parent == agents_root for d in leaked)
    # the target is never selected for removal
    assert agents_root / "default" not in leaked


def test_wipe_removes_created_agents_preserves_target(tmp_path):
    """Tier 2: #2169 — after the wipe, created siblings' profile.yaml is gone (so a
    re-run no longer hits "agent already exists") while the target's profile.yaml
    survives (so get_or_load does not raise FileNotFoundError). RED before the fix:
    the sibling profiles were never removed and leaked across single-runs."""
    agents_root = tmp_path / ".reyn" / "agents"
    _seed_agent(agents_root, "default")
    _seed_agent(agents_root, "researcher")
    _seed_agent(agents_root, "alice")

    _wipe_leaked_agents(tmp_path, "default")

    # created agents are gone — the "already exists" marker no longer present
    assert not (agents_root / "researcher" / PROFILE_FILENAME).is_file()
    assert not (agents_root / "researcher").exists()
    assert not (agents_root / "alice").exists()
    # the dogfood target is spared — its profile (a load precondition) still loads
    assert (agents_root / "default" / PROFILE_FILENAME).is_file()
    assert AgentProfile.load(agents_root / "default").name == "default"


def test_wipe_is_idempotent_with_no_agents_dir(tmp_path):
    """Tier 2: #2169 — the reset is a no-op (never raises) when nothing has leaked,
    including when the .reyn/agents/ dir does not exist yet."""
    _wipe_leaked_agents(tmp_path, "default")  # no .reyn/agents/ at all
    assert _leaked_agent_dirs(tmp_path, "default") == []
