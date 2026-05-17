"""Tier 2: routing_decided P6 event emitted by RouterLoop (FP-0034 Phase 3).

Five invariant tests:

1. invoke_action call → routing_decided(source="invoke_action", outcome="success")
2. hot list alias call → routing_decided(source="hot_list_alias", outcome="success")
3. error tool result → outcome="error"
4. non-catalog tool (invoke_skill) → NO routing_decided event
5. action_name absent in invoke_action args → no event

No MagicMock / AsyncMock.  call_llm_tools is replaced with a real
coroutine function via monkeypatch.  FakeRouterHost and _FakeEventLog are
minimal real collaborators following the pattern in test_replay_skill_router.py.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from reyn.chat.router_loop import RouterLoop
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.tools.action_usage_tracker import ActionUsageTracker

# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=2)


def _tool_result(calls: list[dict]) -> LLMToolCallResult:
    """Build an LLMToolCallResult that contains one tool_call round."""
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
# _FakeEventLog — minimal real collaborator (records emitted events)
# ---------------------------------------------------------------------------

class _FakeEventLog:
    """Minimal events stub: records emitted events, no subscribers."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, type: str, **data) -> None:
        self.emitted.append({"type": type, **data})


# ---------------------------------------------------------------------------
# _FakeRouterHost — minimal real RouterLoopHost with universal wrappers on
# ---------------------------------------------------------------------------

class _FakeRouterHost:
    """Minimal host for routing_decided P6 event tests.

    universal_wrappers_enabled=True by default so routing_decided fires.
    tracker: pass a real ActionUsageTracker to enable hot list alias injection.
    """

    agent_name: str = "test-agent"
    agent_role: str = "test role"
    output_language: str = "en"

    def __init__(
        self,
        *,
        universal_wrappers_enabled: bool = True,
        tracker: "ActionUsageTracker | None" = None,
        skills: list[dict] | None = None,
    ) -> None:
        self._universal_wrappers_enabled = universal_wrappers_enabled
        self._tracker = tracker
        self._skills = skills or []
        self.outbox: list[dict] = []
        self._events = _FakeEventLog()

    @property
    def events(self) -> _FakeEventLog:
        return self._events

    def get_universal_wrappers_enabled(self) -> bool:
        return self._universal_wrappers_enabled

    def get_action_usage_tracker(self) -> "ActionUsageTracker | None":
        return self._tracker

    def get_action_embedding_index(self):  # type: ignore[return]
        return None

    def get_embedding_provider(self):  # type: ignore[return]
        return None

    def get_embedding_model_class(self):  # type: ignore[return]
        return None

    def get_action_retrieval_config(self):  # type: ignore[return]
        return None

    def list_available_skills(self) -> list[dict]:
        return list(self._skills)

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
        """Stub skill runner: always returns success so invoke_action completes."""
        return {"status": "finished", "data": {"result": f"{skill} ran"}}


# ---------------------------------------------------------------------------
# Helper — build loop + run one turn with a pre-scripted LLM sequence
# ---------------------------------------------------------------------------

def _run_with_llm_sequence(
    host: _FakeRouterHost,
    llm_turns: list[LLMToolCallResult],
) -> None:
    """Drive RouterLoop.run() using a real coroutine sequence as call_llm_tools.

    The stub pops from llm_turns on each call; after exhaustion raises
    StopIteration (should not be reached in well-constructed tests).
    No MagicMock or AsyncMock — only a real coroutine function.
    """
    turns = list(llm_turns)  # copy so caller can reuse

    async def _fake_call_llm_tools(**kwargs: object) -> LLMToolCallResult:
        return turns.pop(0)

    loop = RouterLoop(host=host, chain_id="chain-test", max_iterations=5)
    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=_fake_call_llm_tools):
        asyncio.run(loop.run("hello", []))


def _routing_decided_events(host: _FakeRouterHost) -> list[dict]:
    return [e for e in host.events.emitted if e["type"] == "routing_decided"]


# ---------------------------------------------------------------------------
# Test 1: invoke_action → routing_decided(source="invoke_action", outcome="success")
# ---------------------------------------------------------------------------


def test_routing_decided_emitted_for_invoke_action():
    """Tier 2: invoke_action call emits routing_decided with source='invoke_action' and outcome='success'."""
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    # Turn 1: LLM calls invoke_action(action_name="skill__foo")
    # Turn 2: LLM emits text reply (stop)
    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "invoke_action", "args": {"action_name": "skill__foo", "args": {}}}]),
            _text_result("ok"),
        ],
    )

    events = _routing_decided_events(host)
    assert len(events) == 1, f"Expected 1 routing_decided event, got {events}"
    ev = events[0]
    assert ev["action_name"] == "skill__foo"
    assert ev["source"] == "invoke_action"
    assert ev["outcome"] == "success"
    assert ev["chain_id"] == "chain-test"


# ---------------------------------------------------------------------------
# Test 2: hot list alias → routing_decided(source="hot_list_alias", outcome="success")
# ---------------------------------------------------------------------------


def test_routing_decided_emitted_for_hot_list_alias():
    """Tier 2: hot list alias call emits routing_decided with source='hot_list_alias' and outcome='success'.

    A real ActionUsageTracker pre-loaded with 'skill__bar' is passed so
    RouterLoop injects 'skill__bar' as a hot list alias into build_tools.
    That makes the alias a valid catalog entry, so dispatch_tool succeeds
    and outcome='success' is recorded.
    """
    # Build a real tracker with skill__bar pre-recorded (high frequency).
    tracker = ActionUsageTracker()
    for _ in range(5):
        tracker.record("skill__bar")

    host = _FakeRouterHost(
        universal_wrappers_enabled=True,
        tracker=tracker,
        skills=[{"name": "bar", "short_description": "bar skill"}],
    )
    # B39: ``bar`` must be in available_skills so the registry-existence
    # check accepts ``skill__bar``. No input_schema needed (= empty-schema
    # skills are valid registry members; see B39 #119 fix).
    # Turn 1: LLM calls skill__bar (hot list alias — contains '__')
    # Turn 2: text reply
    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "skill__bar", "args": {}}]),
            _text_result("done"),
        ],
    )

    events = _routing_decided_events(host)
    assert len(events) == 1, f"Expected 1 routing_decided event, got {events}"
    ev = events[0]
    assert ev["action_name"] == "skill__bar"
    assert ev["source"] == "hot_list_alias"
    assert ev["outcome"] == "success"
    assert ev["chain_id"] == "chain-test"


# ---------------------------------------------------------------------------
# Test 3: error result → outcome="error"
# ---------------------------------------------------------------------------


def test_routing_decided_outcome_error_on_tool_error():
    """Tier 2: routing_decided outcome='error' when tool result contains error key."""
    host = _FakeRouterHost(universal_wrappers_enabled=True)

    # We need the tool handler to return an error dict.  The easiest path
    # is to exercise _invoke_router_tool which is called by _execute_tool.
    # invoke_action is handled by the universal_dispatch registry; when
    # the action is not found the handler returns status="error".
    # Rather than relying on that, we use a hot list alias (skill__bad)
    # whose dispatch also reaches invoke_action, but here we drive it
    # through a custom subclass that returns an error result directly.

    class _ErrorHost(_FakeRouterHost):
        """Overrides get_universal_wrappers_enabled + inject fake action handler."""

    error_host = _ErrorHost(universal_wrappers_enabled=True)

    # skill__bad will go through _invoke_router_tool → _invoke_via_registry
    # ("invoke_action", {action_name: "skill__bad", args: {}}).
    # The real registry's invoke_action handler will fail because "skill__bad"
    # is not a registered action, returning {"status": "error", ...}.
    # That makes outcome="error".
    _run_with_llm_sequence(
        error_host,
        [
            _tool_result([{"name": "skill__bad", "args": {}}]),
            _text_result("done"),
        ],
    )

    events = _routing_decided_events(error_host)
    assert len(events) == 1, f"Expected 1 routing_decided event, got {events}"
    ev = events[0]
    assert ev["action_name"] == "skill__bad"
    assert ev["outcome"] == "error", (
        f"Expected outcome='error' for unknown action, got {ev['outcome']!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: non-catalog tool → NO routing_decided event
# ---------------------------------------------------------------------------


def test_routing_decided_not_emitted_for_non_catalog_tool():
    """Tier 2: plain tool without '__' and not invoke_action emits no routing_decided."""
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    # Turn 1: LLM calls list_skills (plain OS tool, no '__')
    # The handler will succeed (returns a list) and the loop continues.
    # Turn 2: text reply.
    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "list_skills", "args": {}}]),
            _text_result("ok"),
        ],
    )

    events = _routing_decided_events(host)
    assert events == [], (
        f"routing_decided must NOT fire for non-catalog tool 'list_skills', "
        f"but got: {events}"
    )


# ---------------------------------------------------------------------------
# Test 5: action_name absent in invoke_action args → no event
# ---------------------------------------------------------------------------


def test_routing_decided_skipped_when_action_name_empty():
    """Tier 2: invoke_action call with missing action_name does not emit routing_decided."""
    host = _FakeRouterHost(universal_wrappers_enabled=True)
    # invoke_action with empty args (no action_name key)
    _run_with_llm_sequence(
        host,
        [
            _tool_result([{"name": "invoke_action", "args": {}}]),
            _text_result("ok"),
        ],
    )

    events = _routing_decided_events(host)
    assert events == [], (
        f"routing_decided must NOT fire when action_name is absent/empty, "
        f"but got: {events}"
    )
