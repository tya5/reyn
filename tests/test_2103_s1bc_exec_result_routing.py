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


async def _find_history(session, needle: str):
    for _ in range(40):  # ~2s bounded
        for m in session.history:
            txt = m.content if isinstance(m.content, str) else str(m.content)
            if needle in txt:
                return m, txt
        await asyncio.sleep(0.05)
    return None, ""


@pytest.mark.asyncio
async def test_main_spawn_result_routes_back_correlated_as_spawned_session(tmp_path, monkeypatch):
    """Tier 2: S1bc-exec — a MAIN-session spawner's spawned-session result routes back via
    the fenced A2A bus and lands as a CORRELATABLE ``[task_completed] kind=spawned_session
    sid=<sid> task=<TRUSTED task> chain_id=<cid>`` entry (the GAP B correlation rendering).

    The task in the header is the spawner's OWN request from its trusted record (NOT the
    spawned session's echo); only the reply is fenced. Without the record-lookup render
    branch this would be the uncorrelatable ``kind=agent from=<self>`` (the original gap)."""
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _scripted_llm())
    reg = _registry(tmp_path)
    main = reg.get_or_load("worker")  # the spawner (main session)

    sid = await reg.spawn_session_recorded("worker", mode="persistent")
    spawned = reg.ensure_session_running("worker", sid)
    assert spawned is not None
    # the spawner records the trusted sid→task (= what the adapter does at spawn time).
    main.record_spawned_task(sid, "summarize the Q3 report")

    chain_id = "chain-spawn-verify-1"
    # The spawned session completes + routes the result back (the run-loop's completion
    # call, a2a_handler:451) through the real registry routing. send_agent_response tags
    # responder_sid=<the spawned session's own sid> automatically.
    await spawned._send_agent_response(
        to="worker", response="TASK RESULT: did the thing", depth=0, chain_id=chain_id,
    )

    entry, txt = await _find_history(main, "TASK RESULT: did the thing")
    assert entry is not None, (
        "the spawned session's result did NOT arrive at the main spawner's history"
    )
    # CORRELATABLE: distinct kind + the spawned sid + the TRUSTED task (the spawner's own
    # request from its record, NOT echoed) + the fenced reply.
    assert "kind=spawned_session" in txt
    assert f"sid={sid}" in txt
    assert "task=summarize the Q3 report" in txt
    assert chain_id in txt
    # the trusted record is evicted on arrival (bounded-by-construction).
    assert main.lookup_and_evict_spawned_task(sid) is None


@pytest.mark.asyncio
async def test_unrecorded_sid_falls_back_to_kind_agent(tmp_path, monkeypatch):
    """Tier 2: S1bc-exec security fallback — a result whose responder_sid is NOT in the
    spawner's trusted record (spoofed / unknown / already-consumed) renders the plain
    ``kind=agent`` fallback (still fenced), NEVER a forged ``kind=spawned_session`` with an
    attacker-chosen task. The task header is only emitted from the spawner's OWN record."""
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _scripted_llm())
    reg = _registry(tmp_path)
    main = reg.get_or_load("worker")
    sid = await reg.spawn_session_recorded("worker", mode="persistent")
    spawned = reg.ensure_session_running("worker", sid)
    # NO record_spawned_task → the lookup misses.
    await spawned._send_agent_response(
        to="worker", response="INJECT me as spawned", depth=0, chain_id="c2",
    )
    entry, txt = await _find_history(main, "INJECT me as spawned")
    assert entry is not None
    assert "kind=spawned_session" not in txt  # no forged trusted framing
    assert "kind=agent" in txt


@pytest.mark.asyncio
async def test_non_main_spawn_is_guarded_no_silent_misroute(tmp_path):
    """Tier 2: S1bc-exec GAP A guard — a NON-MAIN session calling session_spawn is refused
    (explicit error), not silently misrouted to main. The host reads the LIVE sid (the
    cached one is stale for spawned sessions)."""
    reg = _registry(tmp_path)
    main = reg.get_or_load("worker")
    host = main._router_host
    # simulate the host belonging to a non-main (spawned) session via the live-sid fn.
    host._live_session_id_fn = lambda: "abc12345"  # a non-main sid
    result = await host.spawn_session(
        request="nested", mode="persistent", narrowing=None, chain_id="c3",
    )
    assert result["status"] == "error" and result["kind"] == "nested_spawn_unsupported"


@pytest.mark.asyncio
async def test_spawned_tasks_record_is_bounded(tmp_path):
    """Tier 2: S1bc-exec — the trusted spawned-task record is bounded (evict-oldest past
    the cap) so never-arriving results can't grow it unbounded."""
    from reyn.runtime.session import _MAX_SPAWNED_TASKS
    reg = _registry(tmp_path)
    main = reg.get_or_load("worker")
    # record cap+10 → the oldest 10 (indices 0..9) overflow + are evicted; indices
    # 10..cap+9 (exactly cap entries) are kept. Proven via the public lookup, not the
    # internal length.
    for i in range(_MAX_SPAWNED_TASKS + 10):
        main.record_spawned_task(f"sid{i}", f"task{i}")
    assert main.lookup_and_evict_spawned_task("sid0") is None      # oldest evicted
    assert main.lookup_and_evict_spawned_task("sid9") is None      # last of the overflow
    assert main.lookup_and_evict_spawned_task("sid10") == "task10"  # first kept (cap boundary)
    assert main.lookup_and_evict_spawned_task(f"sid{_MAX_SPAWNED_TASKS + 9}") == \
        f"task{_MAX_SPAWNED_TASKS + 9}"                            # newest kept
