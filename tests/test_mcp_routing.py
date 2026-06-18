"""Tier 2: FP-0043 S4b-6 (last transport) — MCP-server shared-session routing.

An external MCP client's invocation (Reyn-as-MCP-server) routes to the agent's
SHARED "mcp" session (one per agent, stdio = one connection per process), isolated
from the user's "main" conversation but shared across calls so the request-response
continuity (running_skills pumped on the next call) is preserved. Real
AgentRegistry + StateLog (no mocks).

Falsification (feedback_falsify_acceptance_test_before_proof): the not-main test
reds if routing falls back to "main"; the shared test reds if it becomes per-call.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.mcp_routing import mcp_session_id, resolve_mcp_session
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


def test_mcp_session_id():
    """Tier 2: the routing-key for the shared mcp session is the constant mcp:mcp."""
    assert mcp_session_id() == "mcp:mcp"


@pytest.mark.asyncio
async def test_resolve_mcp_routes_to_mcp_session_not_main(tmp_path):
    """Tier 2: an MCP-server invocation runs on the mcp session, NOT the user's main."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alice")

    s = resolve_mcp_session(reg, "alice")
    assert s is reg.get_session("alice", "mcp:mcp")     # the shared mcp session
    assert reg.get_session("alice", "main") is not s     # isolated from main


@pytest.mark.asyncio
async def test_resolve_mcp_is_shared_across_calls(tmp_path):
    """Tier 2: every MCP call resumes the SAME mcp session (continuity: a partial
    send_to_agent's running_skills are pumped by the next call)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alice")

    first = resolve_mcp_session(reg, "alice")
    second = resolve_mcp_session(reg, "alice")           # next MCP call
    assert second is first                               # shared, not per-call


@pytest.mark.asyncio
async def test_resolve_mcp_is_per_agent(tmp_path):
    """Tier 2: each agent has its OWN mcp session (cross-agent isolation)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alice")
    _seed(tmp_path, "bob")

    a = resolve_mcp_session(reg, "alice")
    b = resolve_mcp_session(reg, "bob")
    assert a is not b
