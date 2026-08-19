"""Tier 2: proposal 0067 P5 (#3978) — the send_to_session tool, end-to-end.

Drives the REAL production chain: reyn.tools.send_to_session._handle ->
RouterCallerState.send_to_session_fn -> Session._router_host.send_to_session
(RouterHostAdapter) -> Session._deliver_cross_session_message (proven
separately, 4 falsify-verified tests in
test_deliver_cross_session_message_3978_p5.py) -> the target agent's
inbox/history.

Mirrors test_3556_session_spawn_narrowing_inheritance.py's harness shape
(real AgentRegistry + real Session + a ToolContext wired the way RouterLoop
wires one for this tool — the handler and the adapter method under test are
both the production objects, not stand-ins).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.turn_origin import TurnOrigin
from reyn.tools.send_to_session import _handle as _handle_send_to_session
from reyn.tools.types import RouterCallerState, ToolContext
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    return reg


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def _ctx_for(caller: Session, reg: AgentRegistry) -> ToolContext:
    """A ToolContext wired the way RouterLoop wires one for this tool
    (router_loop._send_to_session_bound_impl's exact shape)."""
    host = caller.router_host

    async def _send_to_session_bound(
        *, agent: str, session: str, text: str, wake: bool = False,
    ) -> dict:
        return await host.send_to_session(
            agent=agent, session=session, text=text, wake=wake,
        )

    return ToolContext(
        events=host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=RouterCallerState(
            agent_registry=reg, host=host,
            send_to_session_fn=_send_to_session_bound,
        ),
    )


@pytest.mark.asyncio
async def test_delivers_and_reports_status(tmp_path):
    """Tier 2: the tool handler delivers to a peer's session and reports
    status='delivered' — the LLM-visible contract."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    alpha = reg.get_or_load("alpha")
    beta = reg.get_or_load("beta")

    result = await _handle_send_to_session(
        {"agent": "beta", "session": "main", "text": "hi from alpha", "wake": False},
        _ctx_for(alpha, reg),
    )

    assert result == {
        "status": "delivered", "agent": "beta", "session": "main", "wake": False,
    }
    assert beta.inbox.qsize() == 1
    kind, payload = beta.inbox.get_nowait()
    assert kind == TurnOrigin.PEER_SESSION
    assert payload["text"] == "hi from alpha"
    assert payload["from_agent"] == "alpha"
    # architect review (#4101): without this, a wake=false ride-along's
    # flush attribution falls back to the entry's own KIND for its label
    # ("[peer_session:peer_session]"), naming no peer at all — the
    # formatter's fallback is correct by construction, but this producer
    # must actually supply `name` for it to say anything useful. Falsify-
    # verified: dropping the `"name": ...` line from
    # RouterHostAdapter.send_to_session's payload makes this go RED.
    assert payload["name"] == "alpha/main"


@pytest.mark.asyncio
async def test_target_not_found_returns_error_not_a_fabricated_success(tmp_path):
    """Tier 2: B33 W5 F2 precedent (delegate_to_agent) — a target naming no
    live session returns an error-shaped result, never a success-shaped
    envelope that would invite the LLM to fabricate a reply on the peer's
    behalf."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    alpha = reg.get_or_load("alpha")

    result = await _handle_send_to_session(
        {"agent": "alpha", "session": "never-spawned", "text": "hello?", "wake": False},
        _ctx_for(alpha, reg),
    )

    assert result["status"] == "error"
    assert result["kind"] == "target_session_not_found"


@pytest.mark.asyncio
async def test_wake_true_starts_the_target_run_loop(tmp_path):
    """Tier 2: wake=True reaches through to booting the target's run-loop —
    the end-to-end counterpart to
    test_deliver_cross_session_message_3978_p5.py's substrate-level test."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    alpha = reg.get_or_load("alpha")
    reg.get_or_load("beta")

    before = len(reg.running_tasks())
    result = await _handle_send_to_session(
        {"agent": "beta", "session": "main", "text": "wake up", "wake": True},
        _ctx_for(alpha, reg),
    )
    after = len(reg.running_tasks())

    assert result["status"] == "delivered"
    assert after == before + 1
