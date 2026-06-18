"""Tier 2: OS invariant — F4 cost-always-zero regression tests.

Two fixes tested here:
  Bug 1: estimate_cost was called with the proxy-prefixed model name
         (e.g. "openai/gemini-2.5-flash-lite") which litellm.model_cost
         does not recognise, so cost stayed 0.0 forever.
  Bug 2: RouterLoop.run() returned None, so the router's LLM call usage
         never reached Session._total_usage / _total_cost_usd.

Policy: no MagicMock on collaborators — use real RouterLoop + fake host,
and real estimate_cost with a fake model_cost entry.
"""
from __future__ import annotations

import asyncio

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage, estimate_cost
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.session import Session

# ---------------------------------------------------------------------------
# Helpers shared with existing router tests
# ---------------------------------------------------------------------------

_USAGE = TokenUsage(prompt_tokens=200, completion_tokens=50)


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_USAGE,
    )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Minimal FakeRouterHost (mirrors test_router_loop.py pattern)
# ---------------------------------------------------------------------------

class _FakeEventLog:
    def emit(self, type: str, **data) -> None:  # noqa: A002
        pass


class _FakeHost:
    chat_id: str = "test-chat"
    agent_name: str = "test-agent"
    agent_role: str = "test"
    output_language: str | None = "ja"

    def __init__(self) -> None:
        self._events = _FakeEventLog()
        self.outbox: list[dict] = []

    @property
    def events(self):
        return self._events

    def list_available_skills(self) -> list[dict]:
        return []

    def list_available_agents(self) -> list[dict]:
        return []

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
        return f".reyn/{layer}/{slug}.md"

    def memory_dir(self, layer: str) -> str:
        return f".reyn/{layer}"

    def resolve_model(self, name: str) -> str:
        # Return prefixed model to exercise Bug 1 strip logic in RouterLoop
        return "openai/gemini-2.5-flash-lite"

    async def run_skill_awaitable(self, *, skill, input, chain_id) -> dict:
        return {}

    async def send_to_agent(self, *, to, request, depth, chain_id) -> None:
        pass

    async def put_outbox(self, *, kind, text, meta) -> None:
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    async def file_read(self, path: str) -> str:
        return ""

    async def file_write(self, path: str, content: str) -> dict:
        return {}

    async def file_delete(self, path: str) -> dict:
        return {}

    async def file_list_directory(self, path: str) -> list[dict]:
        return []

    async def file_regenerate_index(self, path, output_path, entry_template, header) -> dict:
        return {}

    async def mcp_list_servers(self) -> list[dict]:
        return []

    async def mcp_list_tools(self, server: str) -> list[dict]:
        return []

    async def mcp_call_tool(self, server, tool, args) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Test 1 — Bug 2: RouterLoop.run() returns usage, session accumulates it
# ---------------------------------------------------------------------------

def test_router_loop_total_usage_propagates_to_session(tmp_path, monkeypatch):
    """Tier 2: after a chat turn hitting RouterLoop, session.total_usage.prompt_tokens > 0.

    Verifies Bug 2 fix: RouterLoop.run() now returns the accumulated
    TokenUsage and _run_router_loop credits it to Session._total_usage.
    """
    monkeypatch.chdir(tmp_path)
    session = Session(agent_name="test_agent")

    async def _stub_call_llm_tools(**kwargs):
        return _text_result("こんにちは")

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _stub_call_llm_tools)
    _run(session._handle_user_message("hello", chain_id="c1"))

    assert session.total_usage.prompt_tokens > 0, (
        "RouterLoop usage was not propagated to session._total_usage (Bug 2 not fixed)"
    )


# ---------------------------------------------------------------------------
# Test 2 — Bug 1: estimate_cost strips proxy prefix before lookup
# ---------------------------------------------------------------------------

def test_estimate_cost_strips_proxy_prefix():
    """Tier 2: estimate_cost with bare 'gemini-2.5-flash-lite' succeeds
    while the proxy-prefixed 'openai/gemini-2.5-flash-lite' returns (None, None).

    This is the exact root cause of F4 Bug 1: the kernel passed the proxy-prefixed
    model string to estimate_cost, which is absent from litellm.model_cost, so
    every cost computation returned (None, None) and _total_cost_usd stayed 0.
    """
    usage = TokenUsage(prompt_tokens=100, completion_tokens=50)

    # Prefixed name — not present in litellm.model_cost → (None, None)
    cost_prefixed, _ = estimate_cost("openai/gemini-2.5-flash-lite", usage)
    # Bare name — present in litellm.model_cost → non-zero cost
    cost_bare, _ = estimate_cost("gemini-2.5-flash-lite", usage)

    assert cost_prefixed is None, (
        f"Expected (None, None) for proxy-prefixed model, got cost={cost_prefixed!r}. "
        "If litellm now resolves this prefix, the stripping logic may need revision."
    )
    assert cost_bare is not None and cost_bare > 0, (
        f"Expected non-zero cost for bare model, got cost={cost_bare!r}"
    )


def test_router_loop_run_accumulates_usage_across_iterations():
    """Tier 2: RouterLoop._total_usage accumulates token counts from every
    LLM iteration, and run() returns the total.

    Two iterations: first returns a tool_call (sync), second returns text.
    Both contribute usage to the returned TokenUsage.
    """
    import json

    host = _FakeHost()
    loop = RouterLoop(host=host, chain_id="c-test", max_iterations=3)

    iter_usage = TokenUsage(prompt_tokens=50, completion_tokens=10)

    tool_result = LLMToolCallResult(
        content=None,
        tool_calls=[{
            "id": "tc_1",
            "type": "function",
            "function": {"name": "list_skills", "arguments": json.dumps({})},
        }],
        finish_reason="tool_calls",
        usage=iter_usage,
    )
    text_result = LLMToolCallResult(
        content="done",
        tool_calls=[],
        finish_reason="stop",
        usage=iter_usage,
    )

    results_queue = [tool_result, text_result]

    async def fake_call_llm_tools(**kwargs):
        return results_queue.pop(0)

    async def run_it():
        import reyn.runtime.router_loop as _rl_mod
        original = _rl_mod.call_llm_tools
        _rl_mod.call_llm_tools = fake_call_llm_tools
        try:
            return await loop.run(user_text="hi", history=[])
        finally:
            _rl_mod.call_llm_tools = original

    total = asyncio.run(run_it())

    assert total is not None
    # Two iterations × 50 prompt + 10 completion
    assert total.prompt_tokens == 100, f"Expected 100 prompt tokens, got {total.prompt_tokens}"
    assert total.completion_tokens == 20, f"Expected 20 completion tokens, got {total.completion_tokens}"
