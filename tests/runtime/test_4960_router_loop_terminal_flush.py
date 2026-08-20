"""Tier 2: #4960 — RouterLoop.run() must call host.events.flush_agent_delta
(the durable-write terminal-flush half of the agent_delta coalescing
guarantee) on EVERY exit path, not just successful completion.

Real RouterLoop + FakeRouterHost + a real (scripted, not AsyncMock)
call_llm_tools replacement — this repo's own established idiom
(tests/_support/router_loop.py, already shared by test_router_cap.py and
others). Only the LLM boundary is faked.
"""
from __future__ import annotations

import pytest

from tests._support.router_loop import (
    FakeRouterHost,
    RaisingLLM,
    ScriptedLLM,
    make_loop,
    text_result,
)


@pytest.mark.asyncio
async def test_successful_run_flushes_agent_delta_for_its_chain(monkeypatch) -> None:
    """Tier 2: #4960 — a normal, successful turn still flushes at the end
    (covers the case where the last stream ended just under the fragment/
    interval thresholds — see backend.py's own docstring for why this
    matters even on success, not only failure)."""
    host = FakeRouterHost()
    loop = make_loop(host)
    scripted = ScriptedLLM([text_result("hello")])
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", scripted)

    await loop.run("do something", [])

    assert loop.chain_id in host.events.flush_agent_delta_calls


@pytest.mark.asyncio
async def test_a_raised_exception_still_flushes_agent_delta(monkeypatch) -> None:
    """Tier 1: #4960 — the terminal-flush guarantee's own core claim: an
    exception during the turn must NOT skip the flush (the `finally`
    guarantee, not a happy-path-only call). This is the exact gap
    lead-coder's review found for #4960's original N/T-only proposal."""
    host = FakeRouterHost()
    loop = make_loop(host)
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", RaisingLLM())

    with pytest.raises(RuntimeError, match="scripted LLM failure"):
        await loop.run("do something", [])

    assert loop.chain_id in host.events.flush_agent_delta_calls
