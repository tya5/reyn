"""Tier 2: #1593 PR-3 — CodeBlock arm coexists with the Execute path (design (a)).

Drives the real ``RouterLoop.run`` with a CodeAct scheme over two LLM rounds:
  round 1 → ``CodeBlock`` (the snippet) → the OS CodeBlock arm runs it via
    ``_run_codeblock_round`` and appends the **[assistant: code]** turn + the
    scheme's ``format_feedback`` observation message (a SEPARATE path from the
    Execute zip — no tool_calls, no synthetic tool_call), then ``continue``;
  round 2 → ``PlainText`` → the terminal text-reply path emits the final answer.

This pins the CodeBlock append sequence end-to-end. The Execute path is unchanged
(byte-identical) and covered by the broad router suite — so the two interpretation
arms coexist without breaking each other (separate inline ``case`` arms).

Real RouterLoop + a real Fake host / Fake scheme / real-coroutine call_llm_tools —
no mocks (per testing.ja.md).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.llm.llm import LLMToolCallResult
from reyn.tools.scheme import (
    CodeBlock,
    ExecutionResult,
    PlainText,
    Presentation,
)


class _FakeEvents:
    def emit(self, *args, **kwargs) -> None:
        pass


class _FakeHost:
    """A real Fake RouterLoopHost — the subset run()/run_loop touch. Records
    history entries + outbox so the test can assert the CodeBlock turn sequence."""

    agent_name = "test-agent"
    agent_role = "tester"
    output_language = "en"

    def __init__(self) -> None:
        self.outbox: list[dict] = []
        self.history: list[dict] = []
        self._events = _FakeEvents()

    @property
    def events(self) -> _FakeEvents:
        return self._events

    # context / catalog inputs (all empty — the CodeAct scheme ignores them)
    def list_available_skills(self) -> list[dict]:
        return []

    def list_available_agents(self) -> list[dict]:
        return []

    def get_memory_index(self) -> dict:
        return {"status": "not_found", "content": ""}

    def get_file_permissions(self):
        return None

    def get_mcp_servers(self) -> list[dict]:
        return []

    def get_web_fetch_allowed(self) -> bool:
        return False

    def get_project_context(self) -> str:
        return ""

    def get_universal_wrappers_enabled(self) -> bool:
        return False

    def get_action_usage_tracker(self):
        return None

    def get_action_embedding_index(self):
        return None

    def get_embedding_provider(self):
        return None

    def get_embedding_model_class(self):
        return None

    def get_action_retrieval_config(self):
        from reyn.config import ActionRetrievalConfig
        return ActionRetrievalConfig(hot_list_n=0)

    def resolve_model(self, name: str) -> str:
        return "fake-model"

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    def append_history_entry(self, *, role: str, content: str, meta: dict, **kw) -> None:
        self.history.append({"role": role, "content": content, "source": meta.get("source")})


class _FakeCodeActScheme:
    """A real Fake CodeAct scheme: round 1 → CodeBlock, round 2 → PlainText. execute
    returns a canned result; format_feedback shapes the observation message (the real
    CodeActScheme's shape) the OS CodeBlock arm appends."""

    name = "codeact"

    def __init__(self) -> None:
        self._round = 0

    async def build_presentation(self, available, layer_ctx, ops) -> Presentation:
        return Presentation(
            llm_tools_payload=[],
            sp_params={"universal_wrappers_enabled": False, "search_actions_enabled": False},
            sp_fragment="code-api",
        )

    def interpret(self, llm_response, *, tool_catalog, ops):
        self._round += 1
        return CodeBlock(code="result = tool('m')") if self._round == 1 else PlainText()

    async def execute(self, interp, exec_ctx, ops) -> ExecutionResult:
        return ExecutionResult(tool_results=[{"ok": True, "status": "ok", "result": "computed"}])

    def format_feedback(self, exec_result, ops) -> list[dict]:
        return [{"role": "user", "content": "[codeact result]\ncomputed"}]


def _result(content: str) -> LLMToolCallResult:
    return LLMToolCallResult(content=content, tool_calls=[], usage=None, finish_reason="stop")


@pytest.mark.asyncio
async def test_codeblock_arm_full_round_then_plaintext_exit(monkeypatch) -> None:
    """Tier 2: a CodeBlock round appends [assistant: code] + the observation message
    (separate from the Execute zip), then a PlainText round exits via the text-reply
    path — the CodeBlock arm drives a full round end-to-end and coexists with Execute."""
    host = _FakeHost()
    loop = RouterLoop(host=host, chain_id="c", max_iterations=5, system_prompt_override="SP")
    loop._scheme = _FakeCodeActScheme()

    turns = [_result("```python\nresult = tool('m')\n```"), _result("the final answer")]

    async def _fake_call_llm_tools(**kwargs) -> LLMToolCallResult:
        return turns.pop(0)

    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", _fake_call_llm_tools)
    await loop.run("hello", [])

    # CodeBlock arm appended the [assistant: code] turn + the observation message
    # (NOT via the Execute zip — no tool message / tool_call_id).
    roles = [(h["role"], h["source"]) for h in host.history]
    assert ("assistant", "router_codeact_turn") in roles
    assert ("user", "router_codeact_turn") in roles
    obs = next(h for h in host.history if h["source"] == "router_codeact_turn" and h["role"] == "user")
    assert "computed" in obs["content"]  # the scheme's observation reached the loop
    # PlainText round 2 exited via the terminal text-reply path.
    assert any("final answer" in o["text"] for o in host.outbox)
