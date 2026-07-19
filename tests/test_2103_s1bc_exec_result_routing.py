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
from reyn.runtime.session_params import PresentationWiring
from tests._support.agent_session import make_session


def _registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        # wire the registry back-reference (= what production frontends pass) so the
        # A2A response callback (_a2a_send_response) can route — without it the
        # response is silently dropped (registry is None).
        return make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
            presentation_wiring=PresentationWiring(presentation_consumer=presentation_consumer, intervention_bridge=intervention_bridge),         )

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

    sid = await reg.spawn_session_recorded("worker", mode="persistent", presentation_consumer=None, intervention_bridge=None)
    spawned = reg.ensure_session_running("worker", sid)
    assert spawned is not None
    # the spawner records the trusted sid→task (= what the adapter does at spawn time).
    main.record_spawned_task(sid, "summarize the Q3 report")

    chain_id = "chain-spawn-verify-1"
    # The spawned session completes + routes the result back (the run-loop's completion
    # call, inter_agent_messaging:451) through the real registry routing. send_agent_response tags
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
    sid = await reg.spawn_session_recorded("worker", mode="persistent", presentation_consumer=None, intervention_bridge=None)
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
async def test_response_routes_to_non_main_spawner_sid_not_main(tmp_path):
    """Tier 2: (LOAD-BEARING, #2130) a response addressed to a NON-MAIN (spawner) sid
    routes to THAT specific session, NOT the agent's main. RED on the name-only delivery
    (get_or_load(to) → main): main would get it + the spawner would not."""
    reg = _registry(tmp_path)
    main = reg.get_or_load("worker")  # the agent's main (must NOT receive a non-main reply)
    x_sid = await reg.spawn_session_recorded("worker", mode="persistent", presentation_consumer=None, intervention_bridge=None)
    spawner_x = reg.ensure_session_running("worker", x_sid)  # the non-main spawner, loaded
    assert spawner_x is not None and x_sid != "main"

    # route a reply to (worker, x_sid) — the #2130 (agent, sid) delivery (main is just the
    # test's sender vehicle; only to_sid selects the target session).
    await main._send_agent_response(
        to="worker", response="RESULT FOR X", depth=0, chain_id="cx", to_sid=x_sid,
    )

    entry_x, _ = await _find_history(spawner_x, "RESULT FOR X")
    assert entry_x is not None, "the reply did NOT reach the non-main spawner session"
    assert not any(
        "RESULT FOR X" in (m.content if isinstance(m.content, str) else str(m.content))
        for m in main.history
    ), "the reply leaked to main (the misroute #2130 fixes)"


@pytest.mark.asyncio
async def test_non_main_spawn_is_now_allowed_guard_lifted(tmp_path):
    """Tier 2: (#2130) the #2103 S1bc-exec non-main-spawn GUARD is LIFTED — a non-main
    session may now spawn (its result routes back to (agent, from_sid)). RED if the guard
    still refuses with nested_spawn_unsupported."""
    reg = _registry(tmp_path)
    main = reg.get_or_load("worker")
    host = main._router_host
    host._live_session_id_fn = lambda: "abc12345"  # a non-main sid
    result = await host.spawn_session(
        request="nested", mode="persistent", narrowing=None, chain_id="c3",
    )
    assert result["status"] == "spawned" and "sid" in result
    assert result.get("kind") != "nested_spawn_unsupported"


@pytest.mark.asyncio
async def test_non_main_delegation_reply_routes_to_delegating_sid_not_main(tmp_path, monkeypatch):
    """Tier 2: (LOAD-BEARING, #2130 delegation leg) a NON-MAIN session that DELEGATES to a
    peer (not spawns) gets the peer's reply routed back to ITS (agent, sid), NOT the agent's
    main — _a2a_send_request threads the delegating session's sid as from_sid. RED if the
    delegation path is name-only (the reply lands on main)."""
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _scripted_llm())
    reg = _registry(tmp_path)
    reg.create("peer")
    main = reg.get_or_load("worker")  # the delegator agent's main (must NOT get the reply)
    x_sid = await reg.spawn_session_recorded("worker", mode="persistent", presentation_consumer=None, intervention_bridge=None)
    x = reg.ensure_session_running("worker", x_sid)  # the NON-MAIN delegating session
    assert x is not None and x_sid != "main"

    # X delegates to peer (from the non-main session) → peer replies → routes to (worker, x_sid).
    await x._a2a_send_request(
        to="peer", from_agent="worker", request="do the thing", depth=1, chain_id="cdel",
    )

    # the peer's reply (an inbound agent_response, framed "from=peer") lands on X, not main.
    entry_x, _ = await _find_history(x, "from=peer")
    assert entry_x is not None, "the peer's reply did NOT reach the non-main delegating session"
    assert not any(
        "from=peer" in (m.content if isinstance(m.content, str) else str(m.content))
        for m in main.history
    ), "the peer's reply leaked to main (the delegation misroute #2130 also fixes)"


@pytest.mark.asyncio
async def test_default_path_loads_cold_main_and_starts_forwarder(tmp_path):
    """Tier 2: (LOAD-BEARING byte-identical leg, #2130) a reply with NO to_sid (the
    default/main case) keeps the existing get_or_load + ensure_running semantics — it
    LOADS a COLD/unloaded main from disk and starts its forwarder. RED if the default path
    were switched to the get-only get_session (a cold/unloaded main would be silently
    DROPPED, never loaded — a warm-main test would not catch this)."""
    reg = _registry(tmp_path)
    reg.create("sender")
    sender = reg.get_or_load("sender")
    assert reg.get_session("worker") is None  # worker's main is COLD (not loaded)

    await sender._send_agent_response(
        to="worker", response="COLD MAIN DELIVERY", depth=0, chain_id="cm",  # to_sid=None → default
    )

    # the cold main was LOADED (get_or_load) — get_session-only would have DROPPED it.
    # Loading proves the DEFAULT branch (get_or_load(to) + ensure_running(to)) executed as
    # one unit, so ensure_running (which starts the run-loop + the user-facing forwarder)
    # ran too — the forwarder-start is covered transitively (no private-state assert).
    target_main = reg.get_session("worker")
    assert target_main is not None, "the cold main was NOT loaded (it would be dropped)"
    entry, _ = await _find_history(target_main, "COLD MAIN DELIVERY")
    assert entry is not None


@pytest.mark.asyncio
async def test_gone_spawner_sid_drops_does_not_fallback_to_main(tmp_path, caplog):
    """Tier 2: (the FAIL-SAFE, #2130) a reply to a non-main sid whose session is NOT loaded
    (gone spawner) is DROPPED (logged), NOT routed to main — a fallback-to-main would
    re-introduce the very misroute #2130 fixes. The fail-safe warning is the drop evidence;
    main's run-loop runs so a fallback WOULD land in its history. RED if it falls back to
    main (the warning wouldn't fire + the result would reach main)."""
    import logging
    reg = _registry(tmp_path)
    main = await reg.ensure_running("worker")  # main's run-loop active → a fallback WOULD process
    with caplog.at_level(logging.WARNING, logger="reyn.runtime.session"):
        await main._send_agent_response(
            to="worker", response="ORPHAN RESULT", depth=0, chain_id="co", to_sid="goneSid999",
        )
        await asyncio.sleep(0.2)  # give a (hypothetical) fallback time to land in history
    # the fail-safe fired (drop), NOT a fallback-to-main delivery:
    assert any(
        "the spawner session is no longer loaded" in r.getMessage() for r in caplog.records
    ), "the fail-safe drop warning did not fire (a fallback-to-main would skip it)"
    assert not any(
        "ORPHAN RESULT" in (m.content if isinstance(m.content, str) else str(m.content))
        for m in main.history
    ), "the orphan reply reached main (a silent misroute — the fail-safe must DROP)"


@pytest.mark.asyncio
async def test_spawned_tasks_record_is_bounded(tmp_path):
    """Tier 2: S1bc-exec — the trusted spawned-task record is bounded (evict-oldest past
    the cap) so never-arriving results can't grow it unbounded."""
    from reyn.runtime.spawn_tracker import _MAX_SPAWNED_TASKS
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
