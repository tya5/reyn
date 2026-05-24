"""Tier 2 tests — RouterLoop empty-response detection (ADR-0021 Option F).

Verifies the OS invariant that an empty LLM response (finish_reason=stop,
no content, no tool calls) produces:

  (a) an audit event "router_empty_response_detected" (P6)
  (b) a user-visible failure message in the outbox
  (c) no retry (call_llm_tools called exactly once)
  (d) P7-clean failure messages and event payloads (no skill/tool names)
  (e) i18n: output_language=ja yields Japanese failure text

Normal (non-empty) responses must NOT emit the new event (regression guard).

No unittest.mock.MagicMock / AsyncMock / patch-with-new_callable used.
_ScriptedLLM is a real callable — see testing policy.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from reyn.chat.router_loop import _EMPTY_RESPONSE_MSG, RouterLoop, _is_empty_router_response
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage

# ---------------------------------------------------------------------------
# Test doubles (copied from test_router_loop.py — shared infrastructure kept
# local so tests remain self-contained and don't couple to module internals)
# ---------------------------------------------------------------------------

class FakeEventLog:
    """Minimal events stub: records emitted events without subscribers."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, type: str, **data) -> None:
        self.emitted.append({"type": type, **data})


class FakeRouterHost:
    """In-memory RouterLoopHost for tests."""

    chat_id: str = "test-chat-id"
    agent_name: str = "test-agent"
    agent_role: str = "test role"
    output_language: str | None = "en"

    def __init__(
        self,
        skills: list[dict] | None = None,
        agents: list[dict] | None = None,
        output_language: str | None = "en",
    ):
        self._skills = skills or []
        self._agents = agents or []
        self.output_language = output_language

        self.outbox: list[dict] = []
        self._events = FakeEventLog()
        self._files: dict[str, str] = {}

    @property
    def events(self) -> FakeEventLog:
        return self._events

    def list_available_skills(self) -> list[dict]:
        return self._skills

    def list_available_agents(self) -> list[dict]:
        return self._agents

    def get_memory_index(self) -> dict:
        return {"status": "not_found", "content": ""}

    def get_file_permissions(self) -> dict | None:
        return None

    def get_mcp_servers(self) -> list[dict]:
        return []

    def get_web_fetch_allowed(self) -> bool:
        return False

    def get_project_context(self) -> str:
        return ""

    async def reyn_src_list(self, *, path: str) -> dict:
        return {"path": path, "entries": []}

    async def reyn_src_read(self, *, path: str) -> dict:
        return {"path": path, "content": ""}

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return {"kind": "web_search", "query": query, "results": []}

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        return {"kind": "web_fetch", "url": url, "status": "ok", "content": ""}

    def memory_path(self, layer: str, slug: str) -> str:
        return f"/memory/{layer}/{slug}.md"

    def memory_dir(self, layer: str) -> str:
        return f"/memory/{layer}"

    async def run_skill_awaitable(self, *, skill: str, input: dict, chain_id: str) -> dict:
        return {"status": "ok", "skill": skill}

    async def send_to_agent(self, *, to: str, request: str, depth: int, chain_id: str) -> None:
        pass

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    async def file_read(self, path: str) -> str:
        if path not in self._files:
            raise FileNotFoundError(f"not found: {path}")
        return self._files[path]

    async def file_write(self, path: str, content: str) -> dict:
        self._files[path] = content
        return {"status": "ok"}

    async def file_delete(self, path: str) -> dict:
        self._files.pop(path, None)
        return {"status": "ok"}

    async def file_list_directory(self, path: str) -> list[dict]:
        return []

    async def file_regenerate_index(self, path: str, output_path: str,
                                     entry_template: str, header: str) -> dict:
        return {"status": "ok"}

    async def mcp_list_servers(self) -> list[dict]:
        return []

    async def mcp_list_tools(self, server: str) -> list[dict]:
        return []

    async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        return {"status": "ok"}

    def resolve_model(self, name: str) -> str:
        return f"fake-model-{name}"


_BASE_USAGE = TokenUsage(prompt_tokens=100, completion_tokens=0)
_NORMAL_USAGE = TokenUsage(prompt_tokens=100, completion_tokens=10)


def empty_stop_result() -> LLMToolCallResult:
    """Simulates provider-level empty-stop glitch (ADR-0021 / B7-G12)."""
    return LLMToolCallResult(
        content=None,
        tool_calls=[],
        finish_reason="stop",
        usage=_BASE_USAGE,
    )


def text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_NORMAL_USAGE,
    )


def tool_result(calls: list[dict]) -> LLMToolCallResult:
    """calls: list of {id?, name, args?}"""
    tool_calls = [
        {
            "id": c.get("id", f"tc_{i}"),
            "type": "function",
            "function": {
                "name": c["name"],
                "arguments": json.dumps(c.get("args", {})),
            },
        }
        for i, c in enumerate(calls)
    ]
    return LLMToolCallResult(
        content=None,
        tool_calls=tool_calls,
        finish_reason="tool_calls",
        usage=_NORMAL_USAGE,
    )


class _ScriptedLLM:
    """Real callable (not AsyncMock) that replaces call_llm_tools with a
    scripted sequence.  Raises IndexError on over-call — makes test failures
    explicit rather than silent."""

    def __init__(self, script: list[LLMToolCallResult]) -> None:
        self._script = list(script)
        self.call_count: int = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        result = self._script[self.call_count]
        self.call_count += 1
        return result


def make_loop(host: FakeRouterHost, max_iterations: int = 5) -> RouterLoop:
    return RouterLoop(host=host, chain_id="chain-test", max_iterations=max_iterations)


# ---------------------------------------------------------------------------
# Unit test: _is_empty_router_response predicate
# ---------------------------------------------------------------------------

def test_is_empty_router_response_detects_empty_stop():
    """Tier 2: _is_empty_router_response returns True for finish=stop + no content + no tool_calls."""
    result = empty_stop_result()
    assert _is_empty_router_response(result) is True


def test_is_empty_router_response_ignores_normal_text_reply():
    """Tier 2: _is_empty_router_response returns False for a non-empty text reply."""
    result = text_result("hello world")
    assert _is_empty_router_response(result) is False


def test_is_empty_router_response_ignores_tool_call_result():
    """Tier 2: _is_empty_router_response returns False when tool_calls present."""
    result = tool_result([{"name": "list_skills", "args": {}}])
    assert _is_empty_router_response(result) is False


def test_is_empty_router_response_handles_none():
    """Tier 2: _is_empty_router_response returns True for None (safety)."""
    assert _is_empty_router_response(None) is True


def test_is_empty_router_response_whitespace_content_counts_as_empty():
    """Tier 2: content that is only whitespace is treated as empty."""
    result = LLMToolCallResult(
        content="   \n\t  ",
        tool_calls=[],
        finish_reason="stop",
        usage=_BASE_USAGE,
    )
    assert _is_empty_router_response(result) is True


# ---------------------------------------------------------------------------
# Integration tests: RouterLoop.run() behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_response_emits_audit_event(monkeypatch):
    """Tier 2: empty-stop response causes router_empty_response_detected event to be emitted (P6)."""
    host = FakeRouterHost()
    loop = make_loop(host)
    scripted = _ScriptedLLM([empty_stop_result()])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("do something", [])

    emitted_types = [e["type"] for e in host.events.emitted]
    assert "router_empty_response_detected" in emitted_types


@pytest.mark.asyncio
async def test_empty_response_event_payload_is_p7_clean(monkeypatch):
    """Tier 2: router_empty_response_detected event payload contains no skill/tool-specific strings (P7)."""
    host = FakeRouterHost()
    loop = make_loop(host)
    scripted = _ScriptedLLM([empty_stop_result()])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("do something", [])

    event = next(
        (e for e in host.events.emitted if e["type"] == "router_empty_response_detected"),
        None,
    )
    assert event is not None, "Event must be emitted"

    # P7: known router tool names must not appear in any event field value.
    # These are the tool names registered in router_tools.py.
    forbidden_tool_names = {
        "invoke_skill", "delegate_to_agent", "list_skills", "describe_skill",
        "list_agents", "describe_agent", "list_memory", "read_memory_body",
        "remember_shared", "remember_agent", "forget_memory",
        "read_file", "write_file", "delete_file", "list_directory",
        "list_mcp_servers", "list_mcp_tools", "call_mcp_tool",
    }
    for key, value in event.items():
        if key == "type":
            continue
        str_value = str(value)
        for tool_name in forbidden_tool_names:
            assert tool_name not in str_value, (
                f"P7 violation: skill/tool name {tool_name!r} found in "
                f"event field {key!r}={str_value!r}"
            )


@pytest.mark.asyncio
async def test_empty_response_event_has_expected_fields(monkeypatch):
    """Tier 2: router_empty_response_detected event carries finish_reason, completion_tokens, prompt_tokens, caller_hint."""
    host = FakeRouterHost()
    loop = make_loop(host)
    scripted = _ScriptedLLM([empty_stop_result()])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("do something", [])

    event = next(
        (e for e in host.events.emitted if e["type"] == "router_empty_response_detected"),
        None,
    )
    assert event is not None
    assert event.get("finish_reason") == "stop"
    assert event.get("completion_tokens") == 0
    assert event.get("caller_hint") == "router"
    assert "prompt_tokens" in event


@pytest.mark.asyncio
async def test_empty_response_puts_failure_message_in_outbox(monkeypatch):
    """Tier 2: empty-stop response causes a user-visible failure message in outbox with kind=agent."""
    host = FakeRouterHost()
    loop = make_loop(host)
    scripted = _ScriptedLLM([empty_stop_result()])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("do something", [])

    (msg,) = host.outbox
    assert msg["kind"] == "agent"
    assert len(msg["text"]) > 0, "Failure text must be non-empty"
    assert msg["meta"].get("source") == "router_empty_response"


@pytest.mark.asyncio
async def test_empty_response_failure_message_is_p7_clean(monkeypatch):
    """Tier 2: failure message sent to user contains no skill/tool-specific names (P7)."""
    host = FakeRouterHost()
    loop = make_loop(host)
    scripted = _ScriptedLLM([empty_stop_result()])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("do something", [])

    (msg,) = host.outbox
    failure_text = msg["text"]

    forbidden_tool_names = {
        "invoke_skill", "delegate_to_agent", "list_skills", "describe_skill",
        "list_agents", "describe_agent", "list_memory", "read_memory_body",
        "remember_shared", "remember_agent", "forget_memory",
        "read_file", "write_file", "delete_file", "list_directory",
        "list_mcp_servers", "list_mcp_tools", "call_mcp_tool",
    }
    for tool_name in forbidden_tool_names:
        assert tool_name not in failure_text, (
            f"P7 violation: skill/tool name {tool_name!r} found in failure message"
        )


@pytest.mark.asyncio
async def test_empty_response_no_retry(monkeypatch):
    """Tier 2: call_llm_tools is invoked exactly once — no retry on empty response (Option F principle)."""
    host = FakeRouterHost()
    loop = make_loop(host, max_iterations=5)
    scripted = _ScriptedLLM([empty_stop_result()])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("do something", [])

    assert scripted.call_count == 1, (
        f"call_llm_tools must be called exactly once (got {scripted.call_count}); "
        "retry is explicitly forbidden by Option F / user principle"
    )


@pytest.mark.asyncio
async def test_empty_response_ja_i18n(monkeypatch):
    """Tier 2: output_language=ja yields Japanese failure message."""
    host = FakeRouterHost(output_language="ja")
    loop = make_loop(host)
    scripted = _ScriptedLLM([empty_stop_result()])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("何かしてください", [])

    (msg,) = host.outbox
    failure_text = msg["text"]
    assert failure_text == _EMPTY_RESPONSE_MSG["ja"], (
        f"Expected Japanese message, got: {failure_text!r}"
    )


@pytest.mark.asyncio
async def test_empty_response_en_i18n(monkeypatch):
    """Tier 2: output_language=en yields English failure message."""
    host = FakeRouterHost(output_language="en")
    loop = make_loop(host)
    scripted = _ScriptedLLM([empty_stop_result()])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("do something", [])

    (msg,) = host.outbox
    failure_text = msg["text"]
    assert failure_text == _EMPTY_RESPONSE_MSG["en"]


@pytest.mark.asyncio
async def test_empty_response_unknown_language_falls_back_to_en(monkeypatch):
    """Tier 2: unknown output_language falls back to English failure message."""
    host = FakeRouterHost(output_language="zh")
    loop = make_loop(host)
    scripted = _ScriptedLLM([empty_stop_result()])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("do something", [])

    (msg,) = host.outbox
    failure_text = msg["text"]
    assert failure_text == _EMPTY_RESPONSE_MSG["en"]


# ---------------------------------------------------------------------------
# Regression guard: normal paths must NOT trigger empty-response handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normal_text_reply_does_not_emit_empty_response_event(monkeypatch):
    """Tier 2: normal (non-empty) text reply does NOT emit router_empty_response_detected event."""
    host = FakeRouterHost()
    loop = make_loop(host)
    scripted = _ScriptedLLM([text_result("Hello, I can help with that.")])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("hi", [])

    empty_events = [
        e for e in host.events.emitted
        if e["type"] == "router_empty_response_detected"
    ]
    assert not empty_events, (
        "No router_empty_response_detected event on normal text reply"
    )
    (msg,) = host.outbox
    assert msg["text"] == "Hello, I can help with that."


@pytest.mark.asyncio
async def test_tool_call_reply_does_not_emit_empty_response_event(monkeypatch):
    """Tier 2: tool_call response does NOT emit router_empty_response_detected event."""
    host = FakeRouterHost(skills=[{"name": "my_skill", "category": "general"}])
    loop = make_loop(host)
    scripted = _ScriptedLLM([
        tool_result([{"name": "invoke_skill", "args": {
            "name": "my_skill", "input": {"type": "T", "data": {}},
        }}]),
        text_result("Done."),
    ])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", scripted)

    await loop.run("run my skill", [])

    empty_events = [
        e for e in host.events.emitted
        if e["type"] == "router_empty_response_detected"
    ]
    assert not empty_events, (
        "No router_empty_response_detected event on normal tool-call path"
    )
    assert scripted.call_count == 2
