"""Tier 3a: #1443 — run-once autonomy reaches the LIVE chat-router SP path.

#1440 added `non_interactive` to `build_system_prompt` and wired it into the
session-side `_build_router_system_prompt` (override/budget path) — but the LIVE
chat-router SP that `reyn run-once` actually renders is built separately inside
`RouterLoop.run()` (router_loop.py), which omitted the flag → run-once still
rendered the "ask ONE clarifying question" directive and dead-stopped (13398).
The original #1440 test called `build_system_prompt` directly, so it unit-passed
while the live path stayed broken.

This test drives `RouterLoop.run()` with a recording `call_llm_tools` and asserts
on the system message the loop ACTUALLY builds — exercising the live path the
#1440 unit test missed. sandbox_2's local-patch preview independently confirmed
the same live effect (`echo x | reyn run-once` renders the proceed-line, the
clarifying line gone).

Real RouterLoop + real `build_system_prompt`; the only injected double is a
recording coroutine for `call_llm_tools` (the Tier-3 LLM-replay seam) — no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop

_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=2)
_CLARIFY = "ask ONE"
_PROCEED = "no interactive user to ask"


class _FakeEventLog:
    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, type: str, **data) -> None:
        self.emitted.append({"type": type, **data})


class _FakeRouterHost:
    """Minimal real RouterLoopHost — enough for RouterLoop.run() to build the
    live SP and reach the first call_llm_tools."""

    agent_name: str = "test-agent"
    agent_role: str = "test role"
    output_language: str = "en"

    def __init__(self) -> None:
        self._events = _FakeEventLog()
        self.outbox: list[dict] = []

    @property
    def events(self) -> _FakeEventLog:
        return self._events

    def get_universal_wrappers_enabled(self) -> bool:
        return True

    def get_action_usage_tracker(self):  # type: ignore[return]
        return None

    def get_action_embedding_index(self):  # type: ignore[return]
        return None

    def get_embedding_provider(self):  # type: ignore[return]
        return None

    def get_embedding_model_class(self):  # type: ignore[return]
        return None

    def get_action_retrieval_config(self):  # type: ignore[return]
        return None

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

    def resolve_model(self, name: str) -> str:
        return "fake-model"

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        self.outbox.append({"kind": kind, "text": text, "meta": meta})


def _live_system_prompt(*, non_interactive: bool, monkeypatch: pytest.MonkeyPatch) -> str:
    """Drive RouterLoop.run() and return the system message it actually built."""
    captured: dict[str, str] = {}

    async def _recording_call_llm_tools(**kwargs: object) -> LLMToolCallResult:
        messages = kwargs.get("messages") or []
        if messages and messages[0].get("role") == "system":
            captured["sp"] = messages[0]["content"]
        return LLMToolCallResult(
            content="done", tool_calls=[], finish_reason="stop", usage=_EMPTY_USAGE,
        )

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", _recording_call_llm_tools)
    loop = RouterLoop(
        host=_FakeRouterHost(), chain_id="chain-1443", non_interactive=non_interactive,
    )
    asyncio.run(loop.run("hello", []))
    assert "sp" in captured, "the live router path did not build a system prompt"
    return captured["sp"]


def test_run_once_live_router_sp_proceeds_not_clarifies(monkeypatch):
    """Tier 3a: #1443 — a non_interactive RouterLoop's LIVE rendered SP omits the
    clarifying-question directive and carries the proceed directive. This is the
    path `reyn run-once` renders; the #1440 unit test never exercised it."""
    sp = _live_system_prompt(non_interactive=True, monkeypatch=monkeypatch)
    assert _CLARIFY not in sp, "live run-once SP must not tell the agent to ask a clarifying question"
    assert _PROCEED in sp


def test_interactive_live_router_sp_keeps_clarifying(monkeypatch):
    """Tier 3a: #1443 — the interactive default LIVE SP keeps the clarifying
    directive (byte-compatible). The differential proves the flag threads through
    RouterLoop into the live build_system_prompt call, not just the session path."""
    sp = _live_system_prompt(non_interactive=False, monkeypatch=monkeypatch)
    assert _CLARIFY in sp
    assert _PROCEED not in sp
