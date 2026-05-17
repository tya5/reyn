"""Tests for chat_turn_completed_inline event (B28-Q2 Case A).

Five tests covering:

1. Tier 1 — schema: EVENT_AUDIT_REQUIREMENTS declares chat_turn_completed_inline
   with the correct field set {chain_id, decision, tool_calls_attempted}.

2. Tier 2 — emit logic (pure inline turn): router LLM returns text-only reply
   on round 1. chat_turn_completed_inline is in the event log,
   routing_decided is NOT.

3. Tier 2 — mutual exclusivity: router LLM calls invoke_action then returns
   text. routing_decided fires, chat_turn_completed_inline does NOT.

4. Tier 2 — verifier must_emit_any passes when either listed type fires.

5. Tier 1 — scenario loader parses must_emit_any without error and surfaces
   the assertions on ExpectedEvents.

Policy: NEVER use MagicMock / AsyncMock / patch (except monkeypatching
call_llm_tools as a real coroutine, following test_router_loop_routing_decided.py
convention). All tests use real instances or _FakeRouterHost / _FakeEventLog.
"""
from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.events.event_schema import EVENT_AUDIT_REQUIREMENTS
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage

# ---------------------------------------------------------------------------
# Shared primitives (mirror test_router_loop_routing_decided.py pattern)
# ---------------------------------------------------------------------------

_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=2)


def _tool_result(calls: list[dict]) -> LLMToolCallResult:
    """Build an LLMToolCallResult containing one tool_call round."""
    tool_calls = [
        {
            "id": c.get("id", f"tc_{i}"),
            "type": "function",
            "function": {
                "name": c["name"],
                "arguments": (
                    json.dumps(c["args"]) if isinstance(c.get("args"), dict)
                    else c.get("args", "{}")
                ),
            },
        }
        for i, c in enumerate(calls)
    ]
    return LLMToolCallResult(
        content=None,
        tool_calls=tool_calls,
        finish_reason="tool_calls",
        usage=_EMPTY_USAGE,
    )


def _text_result(text: str = "done") -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


# ---------------------------------------------------------------------------
# _FakeEventLog + _FakeRouterHost (real collaborators, no mocks)
# ---------------------------------------------------------------------------


class _FakeEventLog:
    """Minimal events stub: records emitted events, no subscribers."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, type: str, **data) -> None:
        self.emitted.append({"type": type, **data})


class _FakeRouterHost:
    """Minimal host for B28-Q2 inline event tests.

    universal_wrappers_enabled=True by default so both routing_decided and
    chat_turn_completed_inline guards can fire.
    """

    agent_name: str = "test-agent"
    agent_role: str = "test role"
    output_language: str = "en"

    def __init__(self, *, universal_wrappers_enabled: bool = True) -> None:
        self._universal_wrappers_enabled = universal_wrappers_enabled
        self.outbox: list[dict] = []
        self._events = _FakeEventLog()

    @property
    def events(self) -> _FakeEventLog:
        return self._events

    def get_universal_wrappers_enabled(self) -> bool:
        return self._universal_wrappers_enabled

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

    async def reyn_src_list(self, *, path: str) -> dict:
        return {"path": path, "entries": []}

    async def reyn_src_read(self, *, path: str) -> dict:
        return {"path": path, "content": ""}

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return {"kind": "web_search", "query": query, "results": []}

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        return {"kind": "web_fetch", "url": url, "status": "ok", "content": ""}

    async def run_skill_awaitable(
        self, *, skill: str, input: dict, chain_id: str
    ) -> dict:
        return {"status": "finished", "data": {"result": f"{skill} ran"}}


def _run_with_llm_sequence(
    host: _FakeRouterHost,
    llm_turns: list[LLMToolCallResult],
) -> None:
    """Drive RouterLoop.run() using a real coroutine sequence as call_llm_tools.

    No MagicMock — only a real coroutine function popping from llm_turns.
    """
    turns = list(llm_turns)

    async def _fake_call_llm_tools(**kwargs: object) -> LLMToolCallResult:
        return turns.pop(0)

    loop = RouterLoop(host=host, chain_id="chain-b28q2", max_iterations=5)
    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=_fake_call_llm_tools):
        asyncio.run(loop.run("hello", []))


def _events_of_type(host: _FakeRouterHost, event_type: str) -> list[dict]:
    return [e for e in host.events.emitted if e["type"] == event_type]


# ---------------------------------------------------------------------------
# Test 1: Tier 1 — schema declaration
# ---------------------------------------------------------------------------


def test_chat_turn_completed_inline_declared_in_event_schema() -> None:
    """Tier 1: EVENT_AUDIT_REQUIREMENTS declares chat_turn_completed_inline
    with the required field set {chain_id, decision, tool_calls_attempted}.
    """
    assert "chat_turn_completed_inline" in EVENT_AUDIT_REQUIREMENTS, (
        "chat_turn_completed_inline must be declared in EVENT_AUDIT_REQUIREMENTS"
    )
    required = EVENT_AUDIT_REQUIREMENTS["chat_turn_completed_inline"]
    assert required == frozenset({"chain_id", "decision", "tool_calls_attempted"}), (
        f"Expected frozenset({{chain_id, decision, tool_calls_attempted}}), got {required!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: Tier 2 — pure inline turn emits chat_turn_completed_inline, NOT routing_decided
# ---------------------------------------------------------------------------


def test_inline_reply_emits_chat_turn_completed_inline() -> None:
    """Tier 2: LLM returns text-only on round 1 (no tool_calls).
    chat_turn_completed_inline must be emitted; routing_decided must NOT be.
    """
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    # Single text reply — no tool calls at all
    _run_with_llm_sequence(host, [_text_result("Here is my inline answer.")])

    inline_events = _events_of_type(host, "chat_turn_completed_inline")
    routing_events = _events_of_type(host, "routing_decided")

    assert len(inline_events) == 1, (
        f"Expected exactly 1 chat_turn_completed_inline event, got {inline_events}"
    )
    ev = inline_events[0]
    assert ev["chain_id"] == "chain-b28q2"
    assert ev["decision"] == "inline_reply"
    assert ev["tool_calls_attempted"] == 0

    assert routing_events == [], (
        f"routing_decided must NOT fire on an inline-only turn, got {routing_events}"
    )


# ---------------------------------------------------------------------------
# Test 3: Tier 2 — mutual exclusivity: invoke_action path fires routing_decided, NOT inline
# ---------------------------------------------------------------------------


def test_invoke_action_emits_routing_decided_not_inline() -> None:
    """Tier 2: LLM calls invoke_action then returns text. routing_decided fires;
    chat_turn_completed_inline does NOT (mutual exclusivity per turn).
    """
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "invoke_action", "args": {"action_name": "skill__foo", "args": {}}}]),
            _text_result("done"),
        ],
    )

    routing_events = _events_of_type(host, "routing_decided")
    inline_events = _events_of_type(host, "chat_turn_completed_inline")

    assert len(routing_events) >= 1, (
        f"routing_decided must fire when invoke_action is called, got {routing_events}"
    )
    assert inline_events == [], (
        f"chat_turn_completed_inline must NOT fire when routing_decided fires, "
        f"got {inline_events}"
    )


# ---------------------------------------------------------------------------
# Test 4: Tier 2 — verifier must_emit_any passes when one listed type fires
# ---------------------------------------------------------------------------


def test_verifier_must_emit_any_passes_when_any_fires() -> None:
    """Tier 2: verify_events with must_emit_any passes when chat_turn_completed_inline
    fires (routing_decided absent), and also passes when routing_decided fires.
    """
    from reyn.dogfood.scenarios import EventAssertion, ExpectedEvents
    from reyn.dogfood.verifiers.events import verify_events

    # Build expected with must_emit_any listing both event types
    expected = ExpectedEvents(
        must_emit_any=[
            EventAssertion(type="routing_decided", count=">=1"),
            EventAssertion(type="chat_turn_completed_inline", count=">=1"),
        ],
    )

    # Case A: only chat_turn_completed_inline fires → should pass
    events_inline = [
        {"type": "chat_turn_completed_inline", "data": {"chain_id": "c1", "decision": "inline_reply", "tool_calls_attempted": 0}}
    ]
    result_inline = verify_events(expected, events_inline)
    assert result_inline.outcome == "verified", (
        f"must_emit_any should pass when chat_turn_completed_inline fires, "
        f"got outcome={result_inline.outcome!r}, detail={result_inline.detail}"
    )

    # Case B: only routing_decided fires → should also pass
    events_routing = [
        {"type": "routing_decided", "data": {"action_name": "skill__x", "source": "invoke_action", "outcome": "success", "chain_id": "c1"}}
    ]
    result_routing = verify_events(expected, events_routing)
    assert result_routing.outcome == "verified", (
        f"must_emit_any should pass when routing_decided fires, "
        f"got outcome={result_routing.outcome!r}, detail={result_routing.detail}"
    )

    # Case C: neither fires → should refute
    result_neither = verify_events(expected, [])
    assert result_neither.outcome in ("refuted", "inconclusive"), (
        f"must_emit_any should not verify when neither event fires, "
        f"got outcome={result_neither.outcome!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: Tier 1 — scenario loader parses must_emit_any without error
# ---------------------------------------------------------------------------


def test_scenario_loader_parses_must_emit_any(tmp_path: Path) -> None:
    """Tier 1: load_scenario_set() parses must_emit_any into ExpectedEvents.must_emit_any
    without error and exposes the correct EventAssertion objects.
    """
    from reyn.dogfood.scenarios import load_scenario_set

    yaml_text = textwrap.dedent("""\
        type: dogfood_scenario_set
        name: b28_q2_must_emit_any_test
        scenarios:
          - id: inline_or_routing
            input: "some prompt"
            expected:
              events:
                must_emit_any:
                  - { type: routing_decided, count: ">=1" }
                  - { type: chat_turn_completed_inline, count: ">=1" }
    """)
    p = tmp_path / "test_must_emit_any.yaml"
    p.write_text(yaml_text, encoding="utf-8")

    ss = load_scenario_set(p)
    assert len(ss.scenarios) == 1
    scenario = ss.scenarios[0]
    assert scenario.expected_events is not None

    ev = scenario.expected_events
    assert ev.must_emit == [], f"must_emit should be empty, got {ev.must_emit}"
    assert len(ev.must_emit_any) == 2, (
        f"Expected 2 must_emit_any entries, got {len(ev.must_emit_any)}: {ev.must_emit_any}"
    )
    types = {a.type for a in ev.must_emit_any}
    assert types == {"routing_decided", "chat_turn_completed_inline"}, (
        f"Unexpected must_emit_any types: {types}"
    )
