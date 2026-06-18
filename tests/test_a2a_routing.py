"""Tier 2: FP-0043 S4b-4 (B) — a2a shared-session routing.

Peer delegations route to the agent's SHARED ``a2a`` session (one per agent),
isolated from the user's "main" conversation but shared across delegations (so the
existing sync→Task escalation / continuation, which assumes a single per-agent
session, keeps working). Real AgentRegistry + StateLog (no mocks).

Falsification (feedback_falsify_acceptance_test_before_proof): the not-main test
reds if routing falls back to "main"; the shared test reds if it becomes
per-delegation (a fresh id per call). Per-delegation (option A) is future.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.a2a_routing import a2a_session_id, resolve_a2a_session
from reyn.chat.profile import AgentProfile
from reyn.chat.registry import AgentRegistry
from reyn.chat.session import Session
from reyn.core.events.state_log import StateLog


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        return s

    return AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def test_a2a_session_id():
    """Tier 2: the routing-key for the shared a2a session is the constant a2a:a2a."""
    assert a2a_session_id() == "a2a:a2a"


@pytest.mark.asyncio
async def test_resolve_a2a_routes_to_a2a_session_not_main(tmp_path):
    """Tier 2: peer delegations run on the a2a session, NOT the user's "main"."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alice")

    s = resolve_a2a_session(reg, "alice")
    assert s is reg.get_session("alice", "a2a:a2a")    # the shared a2a session
    assert reg.get_session("alice", "main") is not s   # isolated from main


@pytest.mark.asyncio
async def test_resolve_a2a_is_shared_across_delegations(tmp_path):
    """Tier 2: option (B) — every delegation resumes the SAME a2a session (so the
    escalation/continuation machinery, single-session by design, is preserved)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alice")

    first = resolve_a2a_session(reg, "alice")
    second = resolve_a2a_session(reg, "alice")          # another peer delegation
    assert second is first                              # shared, not per-delegation


@pytest.mark.asyncio
async def test_resolve_a2a_is_per_agent(tmp_path):
    """Tier 2: each agent has its OWN a2a session (cross-agent isolation)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alice")
    _seed(tmp_path, "bob")

    a = resolve_a2a_session(reg, "alice")
    b = resolve_a2a_session(reg, "bob")
    assert a is not b
