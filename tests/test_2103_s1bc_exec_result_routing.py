"""S1bc-exec verification (lead-gated step 1): EXECUTE the main-case spawn result
round-trip — a spawned session's completion-time response routes back to the spawner
via the fenced A2A response bus and lands as a correlatable fenced inbound.

This is the verification the de-scope was gated on: the trace shows the result-routing
largely EXISTS (handle_agent_response unconditionally appends a fenced ``[task_completed]
kind=agent`` history entry), but it must be EXECUTED end-to-end, not just code-read.

It also pins the concrete GAP B finding: spawn submits ``from_agent=<the agent's own
name>``, so the rendered header is "from=<self>" with NO spawned sid / task ref — not
LLM-correlatable as "the session you spawned for <task>". The PRIMARY S1bc-exec work is
the correlation rendering, not the routing.

Real AgentRegistry + a real Session factory + a scripted call_llm_tools (no network) —
the actual registry routing path (_a2a_send_response → submit_agent_response → the
session run-loop's handle_agent_response).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


def _registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        # wire the registry back-reference (= what production frontends pass) so the
        # A2A response callback (_a2a_send_response) can route — without it the
        # response is silently dropped (registry is None).
        return Session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg  # set BEFORE any session is constructed (get_or_load / spawn)
    reg.create("worker")
    return reg


def _scripted_llm():
    async def _fake_llm(*args, **kwargs) -> LLMToolCallResult:
        return LLMToolCallResult(
            content="ack", tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
        )
    return _fake_llm


@pytest.mark.asyncio
async def test_main_spawn_result_routes_back_as_fenced_inbound(tmp_path, monkeypatch):
    """Tier 2: S1bc-exec verification — a MAIN-session spawner's spawned-session result
    routes back via the fenced A2A response bus and lands in the spawner's history as a fenced
    ``[task_completed] kind=agent`` entry carrying the chain_id (the correlation channel).

    GAP B is asserted in-line: the entry's ``from`` is the agent's OWN name (spawn passes
    from_agent=agent_name), NOT the spawned sid — so it is not yet correlatable to the
    specific spawn. That is the primary S1bc-exec work (the rendering)."""
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _scripted_llm())
    reg = _registry(tmp_path)
    main = reg.get_or_load("worker")  # the spawner (main session)

    # main spawns a session for a task (the real action-layer seam).
    sid = await reg.spawn_session_recorded("worker", mode="persistent")
    spawned = reg.ensure_session_running("worker", sid)
    assert spawned is not None

    chain_id = "chain-spawn-verify-1"
    # The spawned session completes its task and routes the result back — this is the
    # EXACT call the run-loop's agent_request handler makes on completion
    # (a2a_handler.py:451). It goes through the real registry routing:
    # _a2a_send_response → registry.get_or_load(agent) + ensure_running → submit_agent_response.
    await spawned._send_agent_response(
        to="worker", response="TASK RESULT: did the thing", depth=0, chain_id=chain_id,
    )

    # main's run-loop processes the routed agent_response → handle_agent_response appends
    # the fenced [task_completed] entry (pre-LLM-branch, so it lands regardless of the
    # scripted continuation). Poll the spawner's history for it (bounded).
    async def _find_entry():
        for _ in range(40):  # ~2s bounded
            for m in main.history:
                txt = m.content if isinstance(m.content, str) else str(m.content)
                if "[task_completed] kind=agent" in txt and "TASK RESULT: did the thing" in txt:
                    return m
            await asyncio.sleep(0.05)
        return None

    entry = await _find_entry()
    assert entry is not None, (
        "S1bc-exec verification FAILED: the spawned session's result did NOT arrive at the "
        "main spawner's history via the fenced A2A response bus"
    )
    txt = entry.content if isinstance(entry.content, str) else str(entry.content)
    # the result is FENCED + carries the chain_id (the correlation channel).
    assert chain_id in txt
    # GAP B (the primary S1bc-exec work): the header identifies "from=worker" (the agent's
    # OWN name), NOT the spawned sid — so the spawner's LLM cannot yet correlate the
    # unsolicited result to the specific session it spawned.
    assert "from=worker" in txt and sid not in txt
