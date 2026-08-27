"""Tier 2: #5291 — RouterCallerState.available_agents removed; the
registry-dispatch path never reads .reyn/agents/ for it any more.

Owner observation (py-spy, reyn-self): stat calls dominating the main loop,
traced to list_available_agents() being called from build_tools()'s (now-
removed) available_agents param and — the 9th, previously undocumented
path found investigating this issue — RouterLoopDriver._invoke_via_registry,
which rebuilt a full RouterCallerState (including the disk-reading
available_agents field) on EVERY registry-dispatched tool_call, regardless
of whether that tool had anything to do with agents.

This is the witness architect/lead-coder specified for that 9th path:
patch RouterHostAdapter.list_available_agents with a call-counter and
drive a real registry-dispatched tool_call through a real Session. A bare
"== 0" assertion is wrong here — the turn's ONE remaining legitimate
reader (the system-prompt build, #187/#1563) still calls it once,
unaffected by #5291 — so the witness instead asserts the count does NOT
SCALE with the number of registry-dispatched tool_calls in the turn (1
vs 3 tool_calls must produce the SAME total), isolating specifically the
per-tool_call read this issue is about.
"""
from __future__ import annotations

import asyncio
import json

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.services.router_host_adapter import RouterHostAdapter
from tests._support.agent_session import make_session

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _tool_call_result(name: str, args: dict, *, call_id: str = "tc_0") -> LLMToolCallResult:
    return LLMToolCallResult(
        content=None,
        tool_calls=[{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
        finish_reason="tool_calls",
        usage=_EMPTY_USAGE,
    )


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text, tool_calls=[], finish_reason="stop", usage=_EMPTY_USAGE,
    )


def _run_turn_with_n_registry_dispatched_tool_calls(monkeypatch, n: int) -> int:
    """Drive one turn whose model response calls ``list_memory`` (a
    REGISTRY_DISPATCH_TOOLS member unrelated to agents) *n* times before
    finishing, counting every ``RouterHostAdapter.list_available_agents()``
    call across the WHOLE turn (this legitimately includes the ONE real,
    live consumer left — the system-prompt's own ``available_agents``
    rendering, #187/#1563 — unaffected by #5291). Returns that count."""
    call_count = 0
    real_list_available_agents = RouterHostAdapter.list_available_agents

    def _counting_list_available_agents(self):
        nonlocal call_count
        call_count += 1
        return real_list_available_agents(self)

    monkeypatch.setattr(
        RouterHostAdapter, "list_available_agents", _counting_list_available_agents,
    )

    session = make_session(agent_name="avail_agents_removed_test")

    calls = (
        [_tool_call_result("list_memory", {"path": ""}) for _ in range(n)]
        + [_text_result("done")]
    )
    call_iter = iter(calls)

    async def _fake_call_llm_tools(*args, **kwargs):
        return next(call_iter)

    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _fake_call_llm_tools,
    )

    asyncio.run(session._handle_inbox_text("what's in memory?", chain_id="chain-5291"))
    return call_count


def test_registry_dispatched_tool_call_never_reads_available_agents(monkeypatch) -> None:
    """Tier 1: #5291 — a REGISTRY_DISPATCH_TOOLS tool call (list_memory,
    which has nothing to do with agents) drives all the way through
    RouterLoopDriver._invoke_via_registry -> build_resource_caller_state ->
    (formerly) list_available_agents(). Witness: driving 1 vs 3 such
    tool_calls in the same turn must produce the SAME total call count —
    the turn's one remaining legitimate reader (the system-prompt build,
    fired once regardless of how many tool_calls follow) is unaffected,
    but the count must NOT scale with the number of tool_calls. A bare
    "== 0" assertion would be wrong here (the SP build is a real, live
    consumer, #5291 does not touch it) — this delta form isolates
    specifically the registry-dispatch path this issue is about."""
    count_with_1 = _run_turn_with_n_registry_dispatched_tool_calls(monkeypatch, 1)
    count_with_3 = _run_turn_with_n_registry_dispatched_tool_calls(monkeypatch, 3)

    assert count_with_1 == count_with_3, (
        f"list_available_agents() call count scaled with the number of "
        f"registry-dispatched tool_calls ({count_with_1} vs {count_with_3}) "
        "— the #5291 per-tool_call read is not gone"
    )
