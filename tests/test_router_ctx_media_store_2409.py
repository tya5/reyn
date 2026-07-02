"""Tier 2: #2409 — the chat-router op context forwards media_store (parity with the public twin).

`Session._make_router_op_context` → `build_router_op_context` omitted `media_store`, so a chat-router
MCP op got `ctx.media_store=None`: MCP ImageContent could not be saved as a `.reyn/media/` path-ref
and a large image was inlined as base64 into the LLM body (tui's ~400KB repro). The public twin
`RouterHostAdapter.make_router_op_context` already forwards it — this closes the construction-forwarding
parity gap. Real AgentRegistry + Session (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


async def _session(tmp_path) -> Session:
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")
    sid = await reg.spawn_session_recorded("alice")
    return reg.get_session("alice", sid)


@pytest.mark.asyncio
async def test_make_router_op_context_forwards_media_store(tmp_path, monkeypatch):
    """Tier 2: CORE — the router op context carries the session's media_store (not None). RED on the
    pre-fix code: `_make_router_op_context` never passed `media_store` → ctx.media_store was None
    regardless of the session's store → MCP images inlined as base64 instead of a path-ref."""
    monkeypatch.chdir(tmp_path)
    sess = await _session(tmp_path)

    sentinel = object()
    sess._media_store = sentinel  # type: ignore[assignment]
    ctx = sess._make_router_op_context()
    assert ctx.media_store is sentinel, "router op context forwards the session's media_store"


@pytest.mark.asyncio
async def test_router_ctx_media_store_none_when_session_has_none(tmp_path, monkeypatch):
    """Tier 2: parity/no-regression — when the session has no media store, the ctx's is None too
    (the forward is faithful, not a fabricated store)."""
    monkeypatch.chdir(tmp_path)
    sess = await _session(tmp_path)

    sess._media_store = None  # type: ignore[assignment]
    ctx = sess._make_router_op_context()
    assert ctx.media_store is None
