"""Tier 2: #1814 — a2a PER-CONTEXTID session routing.

Peer (A2A) delegations route to a per-``contextId`` ``a2a`` session
(``a2a:<contextId>``), isolated from "main" AND from other contextIds — so
different callers' conversations never interfere, and the same contextId continues
the same conversation. The A2A layer owns the ``contextId ↔ session_id`` mapping.
Real AgentRegistry + StateLog (no mocks).

Falsification: the different-contextIds test reds if the old shared ``a2a:a2a``
session is used (the bug); the same-contextId test reds if a fresh id is minted
per call.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.a2a_routing import (
    a2a_context_id,
    a2a_session_id,
    resolve_a2a_session,
)
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


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


def test_a2a_session_id_is_per_contextid():
    """Tier 2: the routing-key is a2a:<contextId>, and a2a_context_id is its inverse."""
    assert a2a_session_id("ctx-7") == "a2a:ctx-7"
    assert a2a_context_id("a2a:ctx-7") == "ctx-7"
    # round-trip (the A2A layer's contextId↔session_id mapping)
    assert a2a_context_id(a2a_session_id("zzz")) == "zzz"
    # empty contextId falls back to the legacy constant (defensive)
    assert a2a_session_id("") == "a2a:a2a"


@pytest.mark.asyncio
async def test_resolve_a2a_routes_to_contextid_session_not_main(tmp_path):
    """Tier 2: a peer delegation runs on its a2a:<contextId> session, NOT "main"."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alice")

    s = resolve_a2a_session(reg, "alice", "ctx1")
    assert s is reg.get_session("alice", "a2a:ctx1")   # the per-contextId session
    assert reg.get_session("alice", "main") is not s   # isolated from main


@pytest.mark.asyncio
async def test_different_contextids_are_isolated(tmp_path):
    """Tier 2: #1814 fix — different contextIds get DIFFERENT sessions (no cross-talk).

    Falsify: with the old constant native-id both calls returned the shared
    ``a2a:a2a`` session — caller A's conversation leaked into caller B's.
    """
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alice")

    a = resolve_a2a_session(reg, "alice", "caller-A")
    b = resolve_a2a_session(reg, "alice", "caller-B")
    assert a is not b


@pytest.mark.asyncio
async def test_same_contextid_continues_conversation(tmp_path):
    """Tier 2: the SAME contextId resumes the SAME session (continuation)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alice")

    first = resolve_a2a_session(reg, "alice", "ctx1")
    second = resolve_a2a_session(reg, "alice", "ctx1")
    assert second is first


@pytest.mark.asyncio
async def test_resolve_a2a_is_per_agent(tmp_path):
    """Tier 2: each agent has its OWN a2a session per contextId (cross-agent isolation)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alice")
    _seed(tmp_path, "bob")

    a = resolve_a2a_session(reg, "alice", "ctx1")
    b = resolve_a2a_session(reg, "bob", "ctx1")
    assert a is not b
