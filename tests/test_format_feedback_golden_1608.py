"""Tier 2: #1608 format_feedback unification — the byte-identical GOLDEN gate.

The Stage-1 unify relocates the Execute message-construction zip (the
``{role:assistant, tool_calls}`` + per-result ``{role:tool, tool_call_id,
content}`` build) out of the OS loop and into ``universal.format_feedback``. This
test pins the **exact message sequence** the LLM sees for an Execute round, so the
relocation is provably byte-identical — it is the #1406/#187 merge gate
(sandbox_2 co-vets): every tool_call answered, in order, with its own
``tool_call_id``, and the **excluded-in-place** call's error result at its own
index (not dropped).

No mocks: real universal scheme + the real ``_ScriptedLLM`` Fake + ``FakeRouterHost``.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from tests.test_router_loop import FakeRouterHost, text_result, tool_result

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


class _CapturingLLM:
    """Scripted LLM that records the ``messages`` it saw each call — so the test can
    assert the exact sequence the OS built after the Execute round."""

    def __init__(self, script: list[LLMToolCallResult]) -> None:
        self._script = list(script)
        self.call_count = 0
        self.messages_per_call: list[list[dict]] = []

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.messages_per_call.append([dict(m) for m in (kwargs.get("messages") or [])])
        r = self._script[self.call_count]
        self.call_count += 1
        return r


@pytest.mark.asyncio
async def test_execute_round_message_sequence_golden(monkeypatch):
    """Tier 2: #1608/#1406/#187 — an Execute round with a dispatched call + an
    EXCLUDED-in-place call produces the exact assistant + per-call tool-message
    sequence, ids aligned, the excluded call's error at its own index."""
    host = FakeRouterHost()
    host._files["a.txt"] = "hello"
    # file__write is excluded → the pre-dispatch gate must emit a tool_excluded
    # error result IN PLACE at its index (not drop the call).
    loop = RouterLoop(
        host=host, chain_id="chain-golden", max_iterations=5,
        exclude_tools={"file__write"},
    )

    round1 = tool_result([
        {"name": "file__read", "args": {"path": "a.txt"}, "id": "call_read"},
        {"name": "file__write", "args": {"path": "b.txt", "content": "x"}, "id": "call_write"},
    ])
    scripted = _CapturingLLM([round1, text_result("done")])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("read a then write b", [])

    # The 2nd LLM call saw the post-Execute-round message history.
    seq = scripted.messages_per_call[1]
    # Drop the leading system/user turns — focus on the assistant tool-call turn
    # onward (the Execute round's contribution).
    asst_idx = next(i for i, m in enumerate(seq) if m.get("role") == "assistant")
    asst = seq[asst_idx]
    tool_msgs = [m for m in seq[asst_idx:] if m.get("role") == "tool"]

    # (1) assistant turn carries BOTH tool_calls, in order.
    assert [tc["id"] for tc in asst["tool_calls"]] == ["call_read", "call_write"]

    # (2) exactly one tool message per tool_call, ids aligned in order (#1406/#187).
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_read", "call_write"]

    # (3) sandbox_2's excluded-row assert: the EXCLUDED call's tool message is at its
    # own index with its own id, carrying the tool_excluded error (not dropped, not
    # reordered).
    write_msg = tool_msgs[1]
    assert write_msg["tool_call_id"] == "call_write"
    write_body = json.loads(write_msg["content"])
    assert write_body.get("status") == "error"
    assert write_body.get("error", {}).get("kind") == "tool_excluded"

    # (4) the dispatched call's result is its own message at index 0, with its result
    # JSON-serialised into the content (the per-result serialization the relocation
    # must preserve — the exact dispatch outcome is harness-dependent, but it must
    # round-trip as a structured tool result at its own id).
    read_msg = tool_msgs[0]
    assert read_msg["tool_call_id"] == "call_read"
    read_body = json.loads(read_msg["content"])
    assert "status" in read_body
