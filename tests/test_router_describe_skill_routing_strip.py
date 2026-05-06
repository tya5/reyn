"""Tier 2c behavioral tests — RouterLoop describe_skill routing-strip (G12 Pattern D).

Background (B11-R2 finding):
  After the B7 Option G fix (list_skills + system prompt truncation), the G12
  empty-stop attractor persisted on the describe_skill → invoke_skill path.
  Root cause: describe_skill returned the full catalogue entry including the
  ``routing`` block (~780-1200 chars of when_to_use / when_not_to_use / examples).
  This pushed the last tool_response past the P-b attractor threshold (~1000 chars).

  B11-R2 N-shot result (using B10-S5b trace via llm_replay --patch):
    Full routing (~1381 chars):   50% G12 attractor
    Routing stripped (~187 chars): 0% G12 attractor

Fix: _describe_skill() filters out routing + category fields
  (see _DESCRIBE_SKILL_STRIP_FIELDS in router_tools.py).

These tests pin the RouterLoop behavioral invariants:
  (i) The tool_response message appended for describe_skill has no routing field
  (ii) All stdlib skills produce concise describe responses (< 500 chars serialised)

Tests are Tier 2c: pure Python with _ScriptedLLM (no network, no LLM calls).
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.chat.router_tools import _DESCRIBE_SKILL_STRIP_FIELDS
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage

# ---------------------------------------------------------------------------
# Shared test infrastructure (inline to keep test self-contained)
# ---------------------------------------------------------------------------

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


class _FakeEventLog:
    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, type: str, **data) -> None:
        self.emitted.append({"type": type, **data})


class _FakeRouterHost:
    """Minimal in-memory RouterLoopHost for routing-strip tests."""

    chat_id: str = "test-chat-id"
    agent_name: str = "test-agent"
    agent_role: str = "assistant"
    output_language: str = "en"

    def __init__(self, skills: list[dict]) -> None:
        self._skills = skills
        self.outbox: list[dict] = []
        self.skill_calls: list[dict] = []
        self._events = _FakeEventLog()

    @property
    def events(self) -> _FakeEventLog:
        return self._events

    def list_available_skills(self) -> list[dict]:
        return self._skills

    def list_available_agents(self) -> list[dict]:
        return []

    def get_memory_index(self) -> dict:
        return {"status": "not_found", "content": ""}

    def get_file_permissions(self) -> None:
        return None

    def get_mcp_servers(self) -> list[dict]:
        return []

    def memory_path(self, layer: str, slug: str) -> str:
        return f"/memory/{layer}/{slug}.md"

    def memory_dir(self, layer: str) -> str:
        return f"/memory/{layer}"

    async def run_skill_awaitable(self, *, skill: str, input: dict, chain_id: str) -> dict:
        self.skill_calls.append({"skill": skill, "input": input, "chain_id": chain_id})
        return {"status": "ok", "skill": skill}

    async def send_to_agent(self, *, to: str, request: str, depth: int, chain_id: str) -> None:
        pass

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    async def file_read(self, path: str) -> str:
        raise FileNotFoundError(path)

    async def file_write(self, path: str, content: str) -> dict:
        return {"status": "ok"}

    async def file_delete(self, path: str) -> dict:
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


class _ScriptedLLM:
    """Real callable replacing call_llm_tools with a scripted sequence.

    Policy: Mock vs Fake — a real class with __call__, not AsyncMock.
    Also captures every call's kwargs so tests can inspect describe_skill
    tool_response content.
    """

    def __init__(self, script: list[LLMToolCallResult]) -> None:
        self._script = list(script)
        self.call_count: int = 0
        self.calls: list[dict] = []

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls.append(kwargs)
        result = self._script[self.call_count]
        self.call_count += 1
        return result


def _tool_result(calls: list[dict]) -> LLMToolCallResult:
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
        usage=_EMPTY_USAGE,
    )


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


# ---------------------------------------------------------------------------
# Skill fixture with verbose routing (would trigger G12 before fix)
# ---------------------------------------------------------------------------

_SKILL_WITH_ROUTING: dict = {
    "name": "test_skill",
    "description": "A test skill for G12 routing-strip verification.",
    "category": "general",
    "routing": {
        "intents": ["task"],
        "when_to_use": [
            "When the user explicitly requests test_skill",
            "When the user wants to run a test operation",
            "Typical form: 'test_skill を使って <task>'",
        ],
        "when_not_to_use": [
            "For general questions — use direct_llm",
            "For skill evaluation — use eval",
            "When the user wants to build eval specs — use eval_builder",
            "Conceptual explanations (stable_knowledge)",
        ],
        "examples": {
            "positive": ["test_skill を使って処理して", "run test_skill for X"],
            "negative": ["テストって何？", "eval してほしい"],
        },
    },
    "input_artifact": "user_message",
    "input_fields": ["task"],
}

# Total size of routing block when serialised
_ROUTING_SERIALISED_LEN = len(json.dumps(_SKILL_WITH_ROUTING.get("routing", {}), ensure_ascii=False))


# ---------------------------------------------------------------------------
# (i) describe_skill tool_response excludes routing field in RouterLoop context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_skill_routing_absent_in_llm_context():
    """Tier 2c: RouterLoop passes describe_skill response without routing to the LLM.

    G12 Pattern D fix (B11-R2): verbose routing block (~780+ chars) in the
    describe_skill tool_response triggers the empty-stop attractor.  The fix
    strips routing + category from the describe_skill result.

    This test verifies the RouterLoop end-to-end: after describe_skill is
    executed, the tool_response message appended to the LLM context does NOT
    contain the routing field from the skill catalogue entry.
    """
    host = _FakeRouterHost(skills=[_SKILL_WITH_ROUTING])
    loop = RouterLoop(host=host, chain_id="chain-g12")

    # Script: list_skills("") → list_skills("general") → describe_skill → invoke_skill → text reply
    # RouterLoop makes 5 calls: 4 tool-call rounds + 1 text reply after invoke_skill.
    scripted = _ScriptedLLM([
        _tool_result([{"name": "list_skills", "args": {"path": ""}}]),
        _tool_result([{"name": "list_skills", "args": {"path": "general"}}]),
        _tool_result([{"name": "describe_skill", "args": {"name": "test_skill"}}]),
        _tool_result([{"name": "invoke_skill", "args": {"name": "test_skill", "input": {"type": "user_message", "data": {"message": "run test"}}}}]),
        _text_result("Skill test_skill completed."),
    ])

    with patch("reyn.chat.router_loop.call_llm_tools", new=scripted):
        await loop.run("run test_skill", [])

    # Verify describe_skill was actually called (call 3 in 0-indexed = call_count 3)
    assert scripted.call_count >= 4, f"Expected ≥ 4 LLM calls, got {scripted.call_count}"

    # The 4th LLM call (index 3) receives messages that include the describe_skill tool_response.
    # Find the tool_response message with describe_skill content.
    fourth_call_messages = scripted.calls[3]["messages"]

    # Locate the describe_skill tool-response message.
    # dispatch_tool wraps results as {"status": "ok", "data": <value>}.
    # describe_skill returns a dict, list_skills returns a list — so filter
    # for tool messages where "data" is a dict (not a list).
    def _is_describe_skill_response(m: dict) -> bool:
        if m.get("role") != "tool":
            return False
        try:
            parsed = json.loads(m.get("content", "{}"))
        except json.JSONDecodeError:
            return False
        return isinstance(parsed.get("data"), dict) and '"input_artifact"' in m.get("content", "")

    describe_tool_responses = [m for m in fourth_call_messages if _is_describe_skill_response(m)]

    assert describe_tool_responses, (
        "Expected at least one describe_skill tool-response message "
        "(data=dict with input_artifact) in the 4th LLM call's messages"
    )

    for tool_resp_msg in describe_tool_responses:
        content_str = tool_resp_msg.get("content", "")
        # Must not contain routing field
        assert '"routing"' not in content_str, (
            f"describe_skill tool_response must not contain 'routing' field "
            f"(G12 Pattern D fix — _DESCRIBE_SKILL_STRIP_FIELDS).  "
            f"Content (first 200 chars): {content_str[:200]!r}"
        )
        # Must not contain when_to_use / when_not_to_use (routing subkeys)
        assert '"when_to_use"' not in content_str, (
            "describe_skill tool_response must not contain 'when_to_use' (routing subkey)"
        )
        # Must not contain category
        assert '"category"' not in content_str, (
            "describe_skill tool_response must not contain 'category' field "
            f"(_DESCRIBE_SKILL_STRIP_FIELDS: {_DESCRIBE_SKILL_STRIP_FIELDS})"
        )
        # Must preserve the essential invocation fields
        parsed = json.loads(content_str)
        data = parsed.get("data", {})
        assert data.get("name") == "test_skill", "name must be preserved"
        assert "description" in data, "description must be preserved"
        assert data.get("input_artifact") == "user_message", "input_artifact must be preserved"


# ---------------------------------------------------------------------------
# (ii) describe_skill response is concise enough to avoid P-b attractor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_skill_response_below_attractor_threshold():
    """Tier 2c: describe_skill tool_response is concise (<400 chars serialised).

    B11-R2 finding: G12 P-b attractor triggers when the last tool_response
    exceeds ~1000 chars.  With routing stripped, the describe_skill response
    should be well below the danger zone for any stdlib skill.

    400 chars = conservative threshold (all fields except routing should fit).
    The routing block alone is typically 780-1200 chars.
    """
    host = _FakeRouterHost(skills=[_SKILL_WITH_ROUTING])
    loop = RouterLoop(host=host, chain_id="chain-g12-size")

    scripted = _ScriptedLLM([
        _tool_result([{"name": "list_skills", "args": {"path": ""}}]),
        _tool_result([{"name": "list_skills", "args": {"path": "general"}}]),
        _tool_result([{"name": "describe_skill", "args": {"name": "test_skill"}}]),
        _text_result("Skill invoked."),
    ])

    with patch("reyn.chat.router_loop.call_llm_tools", new=scripted):
        await loop.run("run test_skill", [])

    # After describe_skill call, the 4th LLM call has the tool_response
    fourth_call_messages = scripted.calls[3]["messages"]

    def _is_describe_skill_response(m: dict) -> bool:
        if m.get("role") != "tool":
            return False
        try:
            parsed = json.loads(m.get("content", "{}"))
        except json.JSONDecodeError:
            return False
        return isinstance(parsed.get("data"), dict) and '"input_artifact"' in m.get("content", "")

    describe_tool_responses = [m for m in fourth_call_messages if _is_describe_skill_response(m)]

    assert describe_tool_responses, (
        "Expected describe_skill tool_response (data=dict with input_artifact) in 4th call"
    )

    for msg in describe_tool_responses:
        content = msg.get("content", "")
        # Routing block for this fixture is well over 400 chars; if routing is
        # NOT stripped, this assertion would fail.
        assert len(content) < 400, (
            f"describe_skill tool_response is {len(content)} chars — routing field "
            f"likely not stripped (G12 Pattern D fix).  "
            f"Routing block alone is {_ROUTING_SERIALISED_LEN} chars.  "
            f"Content: {content[:300]!r}..."
        )


# ---------------------------------------------------------------------------
# (iii) All stdlib skills produce concise describe responses
# ---------------------------------------------------------------------------


def test_all_stdlib_skills_describe_below_attractor_threshold():
    """Tier 2c: _describe_skill for every stdlib skill returns < 600 chars serialised.

    Ensures the routing-strip fix holds for all stdlib skills (not just the
    test fixture).  Skips if stdlib cannot be loaded (CI isolation).

    600 chars = measured max (skill_improver: 510 chars in B11-R2 measurement),
    well below the ~1000-char P-b danger zone (B7 cross-attractor analysis).
    The routing block alone is 780-1200 chars, so routing-stripped responses
    safely fit within this budget.
    """
    try:
        from reyn.chat.session import enumerate_available_skills
    except ImportError:
        pytest.skip("enumerate_available_skills not importable")

    try:
        stdlib_skills = enumerate_available_skills(exclude=set())
    except Exception as exc:
        pytest.skip(f"enumerate_available_skills raised {exc!r}")

    if not stdlib_skills:
        pytest.skip("No stdlib skills found")

    # Use _FakeRouterHost with all stdlib skills
    host = _FakeRouterHost(skills=stdlib_skills)
    loop = RouterLoop(host=host, chain_id="chain-stdlib")

    violations: list[str] = []
    for skill in stdlib_skills:
        name = skill.get("name", "?")
        result = loop._describe_skill(name)
        serialised = json.dumps({"status": "ok", "data": result}, ensure_ascii=False)
        if len(serialised) > 600:
            violations.append(
                f"{name!r}: {len(serialised)} chars (expected < 600; routing likely not stripped)"
            )

    assert not violations, (
        "These stdlib skills produce describe_skill responses exceeding 600 chars "
        "(G12 Pattern D attractor zone — threshold is 600 chars, P-b danger zone is ~1000 chars):\n"
        + "\n".join(violations)
    )
