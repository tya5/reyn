"""Tier 3a: the OS RePresent arm in run_loop (#1593 PR-4) — the generic re-present
mechanism, exercised with a generic Fake RePresent scheme (NOT retrieval, so the
test does not depend on an embedding index — the arm is scheme-agnostic by design).

The arm, when the active scheme's ``interpret`` returns ``RePresent``:
  1. records the assistant turn + a synthetic tool-response (``_REPRESENT_ACK``)
     for each intercepted tool_call (OpenAI requires every tool_call answered),
  2. re-calls ``build_presentation`` with the refinement + the OS loop-local
     ``presented`` accumulator,
  3. swaps the advertised ``tools`` (+ the dispatch ``_catalog`` mirror),
  4. ``continue``s the main iteration (re-query) — so the next LLM turn sees the
     re-presented tools.
Convergence is the SCHEME's (it self-determines terminal); the OS arm holds a
defensive round backstop (``_MAX_REPRESENT_ROUNDS``) that fires only for a scheme
that never terminates. These pin both.

No mocks — a real Fake scheme + the real ``_ScriptedLLM`` Fake (testing.ja.md).
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.chat.router_loop import _MAX_REPRESENT_ROUNDS, _REPRESENT_ACK
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.tools.scheme import (
    Execute,
    ExecutionResult,
    PlainText,
    Presentation,
    RePresent,
)
from tests.test_router_loop import FakeRouterHost, make_loop, text_result, tool_result

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _search_tool() -> dict:
    return {"type": "function", "function": {"name": "search", "description": "",
                                             "parameters": {"type": "object", "properties": {}}}}


def _matched_tool() -> dict:
    return {"type": "function", "function": {"name": "matched_action", "description": "",
                                             "parameters": {"type": "object", "properties": {}}}}


class _FakeRepresentScheme:
    """A minimal scheme that emits RePresent on a ``search`` call (so the OS arm
    runs) and PlainText otherwise. ``build_presentation`` presents the search tool
    initially and a matched tool (+ candidates) on refinement, converging when the
    match is already in ``presented``."""

    name = "fake-represent"

    def __init__(self, *, never_converge: bool = False) -> None:
        self.never_converge = never_converge
        self.represent_calls: list[Any] = []

    async def build_presentation(self, available, layer_ctx, ops) -> Presentation:
        sp_params = {"universal_wrappers_enabled": False, "search_actions_enabled": False}
        refinement = layer_ctx.get("refinement")
        if not refinement:
            return Presentation(llm_tools_payload=[_search_tool()], sp_params=sp_params)
        self.represent_calls.append({
            "refinement": refinement, "presented": layer_ctx.get("presented"),
        })
        if self.never_converge:
            # Always advertise a fresh candidate → the OS accumulator keeps growing
            # but the scheme never drops the search tool (a misbehaving scheme).
            tag = f"action_{len(self.represent_calls)}"
            return Presentation(
                llm_tools_payload=[_matched_tool(), _search_tool()],
                sp_params=sp_params, candidates=(tag,),
            )
        return Presentation(
            llm_tools_payload=[_matched_tool()], sp_params=sp_params,
            candidates=("matched_action",),
        )

    def interpret(self, llm_response, *, tool_catalog, ops):
        calls = getattr(llm_response, "tool_calls", None) or []
        if not calls:
            return PlainText()
        for tc in calls:
            if tc["function"]["name"] == "search":
                return RePresent(refinement={"query": "x"})
        return Execute(actions=[])

    async def execute(self, interp, exec_ctx, ops):
        return ExecutionResult(tool_results=[])

    def format_feedback(self, result, ops):
        return []


class _CapturingScriptedLLM:
    """A scripted LLM Fake that records the ``messages`` and ``tools`` each call saw
    (so the test can assert the re-presentation reached the next LLM turn)."""

    def __init__(self, script: list[LLMToolCallResult]) -> None:
        self._script = list(script)
        self.call_count = 0
        self.tools_per_call: list[list[dict]] = []
        self.messages_per_call: list[list[dict]] = []

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.tools_per_call.append(list(kwargs.get("tools") or []))
        self.messages_per_call.append([dict(m) for m in (kwargs.get("messages") or [])])
        result = self._script[self.call_count]
        self.call_count += 1
        return result


@pytest.mark.asyncio
async def test_represent_round_appends_ack_swaps_tools_and_requeries(monkeypatch):
    """Tier 3a: a RePresent round appends the synthetic ack + swaps tools, and the
    NEXT LLM turn sees the re-presented tools — then a no-tool-call (PlainText)
    reply exits the loop. Pins the full re-present → re-query → exit cycle."""
    host = FakeRouterHost()
    loop = make_loop(host)
    scheme = _FakeRepresentScheme()
    loop._scheme = scheme

    scripted = _CapturingScriptedLLM([
        tool_result([{"name": "search", "id": "s1"}]),   # → RePresent
        text_result("here is your answer"),              # → PlainText → exit
    ])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    messages = [{"role": "user", "content": "find a tool for me"}]
    await loop.run_loop(messages, [_search_tool()], False)

    # Re-queried once after the re-present.
    assert scripted.call_count == 2
    # build_presentation was re-called with the refinement + the (empty-on-first)
    # presented accumulator.
    assert scheme.represent_calls == [{"refinement": {"query": "x"}, "presented": ()}]
    # The synthetic ack answers the intercepted search tool_call in the history.
    assert any(
        m.get("role") == "tool" and m.get("content") == _REPRESENT_ACK
        and m.get("tool_call_id") == "s1"
        for m in messages
    )
    # The SECOND LLM turn saw the re-presented (swapped) tools — matched_action, not
    # the original search tool.
    second_tool_names = {t["function"]["name"] for t in scripted.tools_per_call[1]}
    assert second_tool_names == {"matched_action"}
    # The loop exited via the PlainText text-reply path.
    assert host.outbox and host.outbox[-1]["text"] == "here is your answer"
    # A P6 audit event was emitted for the re-present round.
    assert any(e["type"] == "router_represent_round" for e in host.events.emitted)


@pytest.mark.asyncio
async def test_represent_backstop_raises_on_nonconverging_scheme(monkeypatch):
    """Tier 3a: the defensive backstop — a scheme that NEVER converges (always
    RePresents, never drops the search tool) + an LLM that always re-searches is
    stopped at ``_MAX_REPRESENT_ROUNDS`` with a clear error, rather than looping
    unboundedly. (Normal schemes converge by construction; this valve is for a
    misbehaving one.)"""
    host = FakeRouterHost()
    # max_iterations above the backstop so the backstop (not iteration exhaustion)
    # is what fires.
    loop = make_loop(host, max_iterations=_MAX_REPRESENT_ROUNDS + 50)
    loop._scheme = _FakeRepresentScheme(never_converge=True)

    always_search = tool_result([{"name": "search", "id": "s1"}])
    scripted = _CapturingScriptedLLM([always_search] * (_MAX_REPRESENT_ROUNDS + 10))
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    messages = [{"role": "user", "content": "loop forever"}]
    with pytest.raises(RuntimeError, match="did not converge"):
        await loop.run_loop(messages, [_search_tool()], False)
    # Stopped at the backstop, not after unbounded rounds.
    assert scripted.call_count == _MAX_REPRESENT_ROUNDS + 1
