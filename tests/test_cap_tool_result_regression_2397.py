"""Tier 2: #2397-followup — uniform capper signature (removes the backward-compat split that crashed).

#2397 threaded ``clean_value`` / ``payload_field`` through the cap chain but left a backward-compat
SPLIT: the router called ``_cap(...)`` WITH the kwargs only for a clean-payload envelope, else the
plain ``_cap(content_str)`` — and it MISSED ``Session._cap_tool_result`` (the capper actually WIRED
into the router host, session.py:1750). So the interactive/MCP path raised ``TypeError: got an
unexpected keyword argument 'clean_value'`` → router fail (owner-facing).

Clean fix (owner: backward-compat = debt): EVERY capper takes the UNIFORM signature
``(content_str, *, clean_value=None, payload_field=None)`` and the router ALWAYS passes the kwargs —
no if/else branch. A missed capper is then a signature mismatch caught structurally, not a runtime
crash on one path. Real AgentRegistry + real Session; + a signature-sweep completeness guard.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.context_budget_advisor import ContextBudgetAdvisor
from reyn.runtime.services.router_host_adapter import RouterHostAdapter
from reyn.runtime.services.tool_result_cap import cap_tool_result_content
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
async def test_session_cap_tool_result_accepts_clean_payload_kwargs(tmp_path, monkeypatch):
    """Tier 2: CORE regression — the REAL wired capper ``Session._cap_tool_result`` accepts the
    clean-payload kwargs the router now always passes. RED on the pre-fix main: ``TypeError:
    unexpected keyword argument 'clean_value'`` → router fail on the interactive/MCP path."""
    monkeypatch.chdir(tmp_path)
    sess = await _session(tmp_path)

    out = sess._cap_tool_result(
        "small tool result",
        clean_value={"content": "small tool result", "_offload_payload_field": "content"},
        payload_field="content",
    )
    assert out == "small tool result", (
        "under-cap → identity, and (critically) NO TypeError on the clean-payload kwargs"
    )


@pytest.mark.asyncio
async def test_session_cap_tool_result_uniform_call_non_envelope(tmp_path, monkeypatch):
    """Tier 2: the router's UNIFORM call with both kwargs None (non-envelope result) behaves like a
    plain cap — no branch needed. Mirrors what router_loop now always does."""
    monkeypatch.chdir(tmp_path)
    sess = await _session(tmp_path)

    out = sess._cap_tool_result("plain result", clean_value=None, payload_field=None)
    assert out == "plain result"


def test_all_cappers_share_uniform_clean_payload_signature():
    """Tier 2: sweep-completeness — EVERY capper accepts ``clean_value`` + ``payload_field``, so the
    router's uniform call can never hit a capper that rejects the kwargs (the #2397 missed-capper
    regression class, eliminated structurally). RED on main: ``Session._cap_tool_result`` lacked them."""
    cappers = (
        Session._cap_tool_result,
        ContextBudgetAdvisor.cap_tool_result,
        RouterHostAdapter.cap_tool_result,
        cap_tool_result_content,
    )
    for fn in cappers:
        params = inspect.signature(fn).parameters
        assert "clean_value" in params, f"{fn.__qualname__} missing clean_value kwarg"
        assert "payload_field" in params, f"{fn.__qualname__} missing payload_field kwarg"
