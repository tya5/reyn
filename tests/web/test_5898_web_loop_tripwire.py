"""Tier 2: #5898 acceptance on the real ``reyn:web`` app — (a) the loop
tripwire is armed by the shipped lifespan, no setting required; (b) an
HTTP request served on the SAME event loop returns while a session's turn
with a multi-megabyte history is in flight, parked at the real LLM
boundary after request assembly (#5894 ②: "その間どの HTTP も返らない").

(b) is the "応答が返る" form the ruling asks for, never a wall-clock: the
request is awaited on the test's own loop (``httpx.ASGITransport``, not
``TestClient``'s portal thread) while the turn task is parked at the gated
``LLMStub`` (``control="gated"``: hangs at the real ``litellm.acompletion``
boundary until released, #5450). What makes the loop free DURING assembly
is the off-loop census gate (``test_5898_off_loop_census.py``); this test
witnesses the user-facing consequence end to end with the real app, the
real registry deps and a real ``Session``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reyn.core.events.state_log import StateLog
from reyn.runtime.chat_message import ChatMessage
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.web_auth import local_operator_asgi


def _reset_singletons() -> None:
    import reyn.interfaces.web.deps as deps

    deps._get_project_root.cache_clear()
    deps._load_config.cache_clear()
    deps._state_log = None
    deps._budget_tracker = None
    deps._perm_resolver = None
    deps._registry = None


@pytest.fixture()
def tmp_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The same minimal project the auth-gate tests bring the real app up in
    (``test_auth_gate_middleware.py``): a ``reyn.yaml``, one agent profile,
    the project root patched and the cwd isolated (#2845/#2860)."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    agent_dir = tmp_path / ".reyn" / "agents" / "default"
    agent_dir.mkdir(parents=True)
    (agent_dir / "profile.yaml").write_text(
        "name: default\nrole: ''\ncreated_at: '2026-01-01T00:00:00+00:00'\n",
        encoding="utf-8",
    )
    _reset_singletons()
    monkeypatch.setattr("reyn.config._find_project_root", lambda _: tmp_path)
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    _reset_singletons()


def test_the_lifespan_arms_the_loop_tripwire_with_the_shipped_config(tmp_project: Path) -> None:
    """Tier 2: acceptance ③ — ``reyn:web``'s lifespan starts the tripwire
    task (a real ``LoopTripwire`` on ``app.state``) with NO setting, and
    stops it at shutdown. Strip-falsify: remove the ``create_task`` in
    ``_lifespan`` → no task on ``app.state`` → red."""
    from reyn.interfaces.web.server import app
    from reyn.runtime.loop_tripwire import LoopTripwire

    app.dependency_overrides.clear()
    with TestClient(app, raise_server_exceptions=False):
        task = getattr(app.state, "loop_tripwire_task", None)
        assert isinstance(task, asyncio.Task), "the shipped lifespan must arm the tripwire"
        assert not task.done(), "armed means running for the app's whole life"
        assert isinstance(app.state.loop_tripwire, LoopTripwire)
    assert task.done(), "shutdown stops it (and releases the process-wide faulthandler timer)"


_BIG = "\n".join(f"line {i}: " + "y" * 60 for i in range(60_000))  # ~4 MB tool body in history


@pytest.mark.asyncio
@pytest.mark.llm_stub(control="gated")
async def test_an_http_request_returns_while_a_big_history_turn_is_in_flight(
    tmp_project: Path, _llm_stub,
) -> None:
    """Tier 2: acceptance ② in its "returns" form — with a ~4 MB tool body
    already in the session's history, a turn is started; once request
    assembly has reached the (gated) LLM boundary, ``GET /api/agents``
    on the SAME loop returns 200. Then the stub is released and the turn
    completes normally (the agent was never harmed by the concurrent
    request). Not a duration: the request is simply awaited.

    Q4: the ``call_started`` wait proves the turn genuinely reached the
    LLM boundary through the real driver (request assembly ran), so the
    GET is served with a real turn in flight, not before it started."""
    import httpx

    from reyn.interfaces.web.server import app

    app.dependency_overrides.clear()
    session = make_session(
        agent_name="default",
        state_log=StateLog(tmp_project / "state.wal"),
        snapshot_path=tmp_project / "snap.json",
    )
    session._append_history(ChatMessage(role="user", content="earlier question"))
    session._append_history(ChatMessage(role="assistant", content="", tool_calls=[
        {"id": "tc-big", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
    ]))
    session._append_history(ChatMessage(role="tool", content=_BIG, tool_call_id="tc-big", name="exec"))

    await session._put_inbox("user", {"text": "and now?", "chain_id": "c-5898"})
    turn = asyncio.create_task(session.run_one_iteration())
    await _llm_stub.call_started.wait()  # request assembly done, parked at the LLM boundary

    transport = httpx.ASGITransport(app=local_operator_asgi(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://reyn.test") as client:
        response = await client.get("/api/agents")
    assert response.status_code == 200, response.text
    assert not turn.done(), "the turn is still parked — the request was served alongside it"

    _llm_stub.release.set()
    assert await turn is True
