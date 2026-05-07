"""Unit tests for RouterLoop (PR35 wave-2 task D).

Uses FakeRouterHost and a scripted callable (_ScriptedLLM) to return scripted
LLMToolCallResult sequences without hitting the network.

No unittest.mock.AsyncMock / MagicMock / patch(new_callable=AsyncMock) are
used. patch() is only called with real callables (policy: Mock vs Fake).
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage

# ---------------------------------------------------------------------------
# Minimal EventLog stub for tests
# ---------------------------------------------------------------------------

class FakeEventLog:
    """Minimal events stub: records emitted events, no subscribers."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, type: str, **data) -> None:
        self.emitted.append({"type": type, **data})


# ---------------------------------------------------------------------------
# FakeRouterHost
# ---------------------------------------------------------------------------

class FakeRouterHost:
    """In-memory RouterLoopHost implementation for tests."""

    chat_id: str = "test-chat-id"
    agent_name: str = "test-agent"
    agent_role: str = "test role"
    output_language: str = "en"

    def __init__(
        self,
        skills: list[dict] | None = None,
        agents: list[dict] | None = None,
        memory_index: dict | None = None,
        file_permissions: dict | None = None,
        mcp_servers: list[dict] | None = None,
    ):
        self._skills = skills or []
        self._agents = agents or []
        self._memory_index = memory_index or {"status": "not_found", "content": ""}
        self._file_permissions = file_permissions
        self._mcp_servers = mcp_servers or []

        # Track calls
        self.outbox: list[dict] = []
        self.skill_calls: list[dict] = []
        self.agent_sends: list[dict] = []
        self.file_writes: list[tuple[str, str]] = []
        self.file_deletes: list[str] = []
        self.file_reads: list[str] = []
        self.index_regenerations: list[dict] = []

        # In-memory "file system"
        self._files: dict[str, str] = {}

        # Events (required by RouterLoopHost protocol for dispatch_tool)
        self._events = FakeEventLog()

    @property
    def events(self) -> FakeEventLog:
        return self._events

    # --- Catalogue ---

    def list_available_skills(self) -> list[dict]:
        return self._skills

    def list_available_agents(self) -> list[dict]:
        return self._agents

    def get_memory_index(self) -> dict:
        return self._memory_index

    def get_file_permissions(self) -> dict | None:
        return self._file_permissions

    def get_mcp_servers(self) -> list[dict]:
        return self._mcp_servers

    def get_web_fetch_allowed(self) -> bool:
        return False

    def get_project_context(self) -> str:
        return ""

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return {"kind": "web_search", "query": query, "results": []}

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        return {"kind": "web_fetch", "url": url, "status": "ok", "content": ""}

    # --- Memory paths ---

    def memory_path(self, layer: str, slug: str) -> str:
        # Match production ChatSession._memory_path contract: appends .md.
        return f"/memory/{layer}/{slug}.md"

    def memory_dir(self, layer: str) -> str:
        return f"/memory/{layer}"

    # --- Action callbacks ---

    async def run_skill_awaitable(self, *, skill: str, input: dict,
                                   chain_id: str) -> dict:
        self.skill_calls.append({"skill": skill, "input": input, "chain_id": chain_id})
        return {"status": "ok", "skill": skill}

    async def send_to_agent(self, *, to: str, request: str, depth: int,
                            chain_id: str) -> None:
        self.agent_sends.append({"to": to, "request": request, "depth": depth,
                                  "chain_id": chain_id})

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    # --- File ops ---

    async def file_read(self, path: str) -> str:
        self.file_reads.append(path)
        if path not in self._files:
            raise FileNotFoundError(f"not found: {path}")
        return self._files[path]

    async def file_write(self, path: str, content: str) -> dict:
        self.file_writes.append((path, content))
        self._files[path] = content
        return {"status": "ok", "path": path}

    async def file_delete(self, path: str) -> dict:
        self.file_deletes.append(path)
        self._files.pop(path, None)
        return {"status": "ok", "path": path}

    async def file_list_directory(self, path: str) -> list[dict]:
        return [{"name": "file.txt", "type": "file"}]

    async def file_regenerate_index(self, path: str, output_path: str,
                                     entry_template: str, header: str) -> dict:
        self.index_regenerations.append({
            "path": path,
            "output_path": output_path,
            "entry_template": entry_template,
            "header": header,
        })
        return {"status": "ok"}

    # --- MCP ops ---

    async def mcp_list_servers(self) -> list[dict]:
        return self._mcp_servers

    async def mcp_list_tools(self, server: str) -> list[dict]:
        return [{"name": "tool1", "description": "A tool"}]

    async def mcp_call_tool(self, server: str, tool: str, args: dict) -> dict:
        return {"status": "ok", "server": server, "tool": tool}

    # --- Model resolution ---

    def resolve_model(self, name: str) -> str:
        return f"fake-model-{name}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


def tool_result(calls: list[dict]) -> LLMToolCallResult:
    """calls: list of {id, name, arguments_dict}"""
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


def make_loop(host: FakeRouterHost, max_iterations: int = 5) -> RouterLoop:
    return RouterLoop(host=host, chain_id="chain-test", max_iterations=max_iterations)


class _ScriptedLLM:
    """Real callable replacing call_llm_tools with a scripted sequence.

    Allowed by policy (Mock vs Fake section): a real class with __call__
    that raises TypeError on signature drift, unlike AsyncMock.
    """

    def __init__(self, script: list[LLMToolCallResult]) -> None:
        self._script = list(script)
        self.call_count: int = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        result = self._script[self.call_count]
        self.call_count += 1
        return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chitchat_no_tools():
    """Tier 1: RouterLoop text-reply path puts one agent message in outbox."""
    host = FakeRouterHost()
    loop = make_loop(host)

    scripted = _ScriptedLLM([text_result("hello")])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("hi", [])

    assert len(host.outbox) == 1
    assert host.outbox[0]["kind"] == "agent"
    assert host.outbox[0]["text"] == "hello"
    assert len(host.skill_calls) == 0
    assert scripted.call_count == 1


@pytest.mark.asyncio
async def test_single_skill_round():
    """Tier 1: RouterLoop dispatches invoke_skill on round 1 and produces text reply on round 2."""
    host = FakeRouterHost(skills=[{"name": "my_skill", "category": "general"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "my_skill",
            "input": {"type": "Foo", "data": {}},
        }}]),
        text_result("Done!"),
    ]
    scripted = _ScriptedLLM(rounds)

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("run my skill", [])

    assert len(host.skill_calls) == 1
    assert host.skill_calls[0]["skill"] == "my_skill"
    assert host.skill_calls[0]["chain_id"] == "chain-test"

    assert len(host.outbox) == 1
    assert host.outbox[0]["text"] == "Done!"
    assert scripted.call_count == 2


@pytest.mark.asyncio
async def test_two_round_sequential():
    """Tier 1: multi-round message accumulation — tool results from round 1 and 2 appear in round 3 messages."""
    host = FakeRouterHost(
        file_permissions={"read": ["/docs"], "write": []},
        skills=[{"name": "skill_a", "category": "general"}],
    )
    host._files["/docs/note.txt"] = "content from file"
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "read_file", "args": {"path": "/docs/note.txt"}}]),
        tool_result([{"name": "invoke_skill", "args": {
            "name": "skill_a",
            "input": {"type": "T", "data": {"note": "content from file"}},
        }}]),
        text_result("All done."),
    ]

    messages_seen: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_seen.append(list(messages))
        return rounds[len(messages_seen) - 1]

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=mock_llm):
        await loop.run("process the file", [])

    # Round 3 messages should include tool results from both prior rounds
    final_messages = messages_seen[2]
    roles = [m["role"] for m in final_messages]
    assert roles.count("tool") == 2, "Two tool result messages expected"
    assert len(host.skill_calls) == 1
    assert host.outbox[0]["text"] == "All done."


@pytest.mark.asyncio
async def test_parallel_tool_calls_executed():
    """Tier 1: RouterLoop executes all tool_calls from a single round concurrently."""
    host = FakeRouterHost(
        skills=[
            {"name": "skill_a", "category": "general"},
            {"name": "skill_b", "category": "general"},
        ]
    )
    loop = make_loop(host)

    rounds = [
        tool_result([
            {"id": "tc_0", "name": "invoke_skill", "args": {
                "name": "skill_a", "input": {"type": "X", "data": {}}}},
            {"id": "tc_1", "name": "invoke_skill", "args": {
                "name": "skill_b", "input": {"type": "Y", "data": {}}}},
        ]),
        text_result("Both done."),
    ]
    scripted = _ScriptedLLM(rounds)

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("run both", [])

    assert len(host.skill_calls) == 2
    called_skills = {c["skill"] for c in host.skill_calls}
    assert called_skills == {"skill_a", "skill_b"}
    assert host.outbox[0]["text"] == "Both done."


@pytest.mark.asyncio
async def test_max_iterations_exhausted():
    """Tier 2: OS invariant — RouterLoop emits error outbox message after exceeding max_iterations cap. Loop never runs more iterations than configured."""
    host = FakeRouterHost()
    loop = make_loop(host, max_iterations=3)

    # Always return a tool call (unknown tool to avoid side effects)
    always_tool = tool_result([{"name": "bogus_tool", "args": {}}])
    scripted = _ScriptedLLM([always_tool] * 3)

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("do stuff", [])

    assert scripted.call_count == 3
    assert len(host.outbox) == 1
    assert host.outbox[0]["kind"] == "error"
    assert "max iterations" in host.outbox[0]["text"]
    assert "3" in host.outbox[0]["text"]


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_in_result():
    """Tier 1: unknown tool name produces error tool result with kind=unknown_tool; loop continues to next round."""
    host = FakeRouterHost()
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "bogus", "args": {}}]),
        text_result("Recovered."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=mock_llm):
        await loop.run("try bogus", [])

    # Find the tool result message from round 1
    round2_messages = messages_captured[1]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    result_data = json.loads(tool_msgs[0]["content"])
    # PR36: unknown tools now return {status: "error", error: {kind: "unknown_tool", ...}}
    assert result_data.get("status") == "error"
    error = result_data.get("error", {})
    assert error.get("kind") == "unknown_tool"
    assert "bogus" in error.get("message", "")
    assert host.outbox[0]["text"] == "Recovered."


@pytest.mark.asyncio
async def test_remember_shared_writes_file_and_regenerates_index():
    """Tier 1: remember_shared tool writes memory file with correct frontmatter and triggers index regeneration."""
    host = FakeRouterHost(file_permissions={"read": ["/memory"], "write": ["/memory"]})
    loop = make_loop(host)

    rounds = [
        tool_result([{
            "name": "remember_shared",
            "args": {
                "slug": "user_role",
                "name": "User Role",
                "description": "User is a developer",
                "type": "user",
                "body": "The user is a senior developer.",
            },
        }]),
        text_result("Saved."),
    ]
    scripted = _ScriptedLLM(rounds)

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("remember: I'm a developer", [])

    # file_write should have been called with the right path
    written_paths = [path for path, _ in host.file_writes]
    assert "/memory/shared/user_role.md" in written_paths

    # Check frontmatter in written content
    written_content = dict(host.file_writes)["/memory/shared/user_role.md"]
    assert "name: User Role" in written_content
    assert "type: user" in written_content
    assert "The user is a senior developer." in written_content

    # file_regenerate_index should have been called
    assert len(host.index_regenerations) == 1
    regen = host.index_regenerations[0]
    assert regen["output_path"] == "/memory/shared/MEMORY.md"

    assert host.outbox[0]["text"] == "Saved."


@pytest.mark.asyncio
async def test_list_skills_empty_path_returns_categories():
    """Tier 1: list_skills('') returns category+count entries grouped by category. Tests tool API output shape without LLM involvement."""
    skills = [
        {"name": "write_blog", "category": "write"},
        {"name": "write_email", "category": "write"},
        {"name": "general_task", "category": "general"},
    ]
    host = FakeRouterHost(skills=skills)
    loop = make_loop(host)

    result = loop._list_skills("")

    # Sort by category for comparison
    result_by_cat = {r["category"]: r["count"] for r in result}
    assert result_by_cat == {"general": 1, "write": 2}


@pytest.mark.asyncio
async def test_list_skills_with_category_returns_items():
    """Tier 1: list_skills('write') returns only skills in the write category. Tests tool API output shape without LLM involvement."""
    skills = [
        {"name": "write_blog", "description": "Writes blog posts", "category": "write"},
        {"name": "write_email", "description": "Writes emails", "category": "write"},
        {"name": "general_task", "description": "General task", "category": "general"},
    ]
    host = FakeRouterHost(skills=skills)
    loop = make_loop(host)

    result = loop._list_skills("write")

    assert len(result) == 2
    names = {r["name"] for r in result}
    assert names == {"write_blog", "write_email"}


@pytest.mark.asyncio
async def test_list_memory_top_level():
    """Tier 1: list_memory('') returns layer+count entries from memory index. Tests tool API output shape without LLM involvement."""
    memory_content = (
        "# Memory Index (shared)\n\n"
        "- [User Role](user_role.md) — Developer\n"
        "- [Project Goal](project_goal.md) — Build OS\n"
        "\n"
        "# Memory Index (agent: chat_20240101)\n\n"
        "- [Feedback tone](feedback_tone.md) — Prefer formal\n"
    )
    host = FakeRouterHost(
        memory_index={"status": "ok", "content": memory_content}
    )
    loop = make_loop(host)

    result = loop._list_memory("")

    result_by_path = {r["path"]: r["count"] for r in result}
    assert result_by_path["shared"] == 2
    assert result_by_path["agent"] == 1


@pytest.mark.asyncio
async def test_delegate_to_agent():
    """Tier 2: OS invariant — RouterLoop exits after first delegate_to_agent dispatch and emits awaiting-peer-reply status; does not iterate further in the same turn.

    Earlier (pre-PR-tui-4-followup) the loop continued and the LLM
    re-delegated each iteration until the cap was exhausted. Fix:
    after a successful delegate dispatch, RouterLoop emits a status
    note and returns; PR14 pending_chain re-invokes router with the
    peer reply later.
    """
    host = FakeRouterHost(agents=[{"name": "peer_agent", "role": "data agent"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{
            "name": "delegate_to_agent",
            "args": {"to": "peer_agent", "request": "please process the data"},
        }]),
        # Subsequent rounds intentionally not consumed — loop must exit
        # after the delegate dispatch.
        text_result("Should not reach this round."),
    ]
    scripted = _ScriptedLLM(rounds)

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("send to peer", [])

    assert len(host.agent_sends) == 1
    assert host.agent_sends[0]["to"] == "peer_agent"
    assert host.agent_sends[0]["request"] == "please process the data"
    assert host.agent_sends[0]["chain_id"] == "chain-test"
    # Only the first LLM call ran; the second round was never consumed.
    assert scripted.call_count == 1
    # Outbox shows the "awaiting peer reply" status, not a text reply.
    assert any(
        m["kind"] == "status" and "awaiting peer reply" in m["text"]
        for m in host.outbox
    ), f"Expected awaiting-peer-reply status; got: {host.outbox}"


@pytest.mark.asyncio
async def test_delegate_does_not_re_delegate_in_same_turn():
    """Tier 2: OS invariant — RouterLoop.run() exits after first delegate dispatch even if LLM keeps emitting delegate calls; exactly one dispatch occurs regardless of max_iterations.

    Real LLM behavior (dogfood verify): with the old code the LLM saw
    `{status: dispatched}`, didn't realize the peer reply would arrive
    asynchronously, and re-delegated each iteration until the cap fired.
    Now we exit after the first delegate so pending_chain can take over.
    """
    host = FakeRouterHost(agents=[{"name": "peer", "role": "x"}])
    loop = make_loop(host, max_iterations=5)

    # If the loop kept iterating it would call delegate 5 times.
    delegate_round = tool_result([{
        "name": "delegate_to_agent",
        "args": {"to": "peer", "request": "do work"},
    }])
    scripted = _ScriptedLLM([delegate_round] * 5)

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("delegate", [])

    # Exactly one delegate dispatch; loop exited after the first iteration.
    assert len(host.agent_sends) == 1
    assert scripted.call_count == 1


@pytest.mark.asyncio
async def test_dedupe_duplicate_async_tool_calls_in_same_round():
    """Tier 2: OS invariant — duplicate async tool_calls (same name, same
    args) in a single LLM round are deduped before dispatch (F5 fix).

    Weak models (e.g. gemini-2.5-flash-lite) sometimes emit
    `delegate_to_agent` twice with identical arguments in one tool_calls
    list. Without dedupe, the peer's inbox would receive the same
    request twice, doubling cost and confusing the chain. After dedupe,
    exactly one send_to_agent runs and a `tool_call_deduped` audit event
    is emitted for the suppressed call.
    """
    host = FakeRouterHost(agents=[{"name": "peer", "role": "x"}])
    loop = make_loop(host)

    # Two identical delegate_to_agent calls in the same round.
    duplicate_round = tool_result([
        {"id": "tc_a", "name": "delegate_to_agent",
         "args": {"to": "peer", "request": "do work"}},
        {"id": "tc_b", "name": "delegate_to_agent",
         "args": {"to": "peer", "request": "do work"}},
    ])
    scripted = _ScriptedLLM([duplicate_round])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("send", [])

    # Only one send_to_agent — duplicate suppressed.
    assert len(host.agent_sends) == 1
    assert host.agent_sends[0]["to"] == "peer"
    assert host.agent_sends[0]["request"] == "do work"
    # Audit event records the suppressed call.
    deduped_events = [
        e for e in host.events.emitted  # type: ignore[attr-defined]
        if e["type"] == "tool_call_deduped"
    ]
    assert len(deduped_events) == 1, (
        f"expected 1 tool_call_deduped event; got: {host.events.emitted}"
    )
    assert deduped_events[0]["name"] == "delegate_to_agent"
    assert deduped_events[0]["reason"] == "duplicate_async_in_round"


@pytest.mark.asyncio
async def test_dedupe_does_not_collapse_distinct_async_args():
    """Tier 2: OS invariant — async tool_calls with different args are
    NOT deduped (F5 false-positive guard).

    Two `delegate_to_agent` calls to the same peer with different
    `request` payloads must both dispatch — they're legitimately distinct
    work items.
    """
    host = FakeRouterHost(agents=[{"name": "peer", "role": "x"}])
    loop = make_loop(host)

    distinct_round = tool_result([
        {"id": "tc_a", "name": "delegate_to_agent",
         "args": {"to": "peer", "request": "task A"}},
        {"id": "tc_b", "name": "delegate_to_agent",
         "args": {"to": "peer", "request": "task B"}},
    ])
    scripted = _ScriptedLLM([distinct_round])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("send two tasks", [])

    # Both dispatch — different args.
    assert len(host.agent_sends) == 2
    requests = sorted(s["request"] for s in host.agent_sends)
    assert requests == ["task A", "task B"]
    # No dedupe events.
    deduped_events = [
        e for e in host.events.emitted  # type: ignore[attr-defined]
        if e["type"] == "tool_call_deduped"
    ]
    assert len(deduped_events) == 0


@pytest.mark.asyncio
async def test_dedupe_does_not_apply_to_non_invoke_sync_tool_calls():
    """Tier 2: OS invariant — duplicate SYNC tool_calls (other than
    invoke_skill) in same round are NOT deduped.

    Sync tool dupes are wasteful but correctness-preserving (same args →
    same result), and deduping them risks tool_call_id mismatches in the
    follow-up assistant message. Only async tools (delegate_to_agent) and
    invoke_skill (G3) get the dedupe treatment; describe_skill and other
    pure-sync tools do not.
    """
    host = FakeRouterHost(skills=[{"name": "my_skill", "category": "general"}])
    loop = make_loop(host)

    rounds = [
        tool_result([
            {"id": "tc_a", "name": "describe_skill",
             "args": {"name": "my_skill"}},
            {"id": "tc_b", "name": "describe_skill",
             "args": {"name": "my_skill"}},
        ]),
        text_result("done"),
    ]
    scripted = _ScriptedLLM(rounds)

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("describe", [])

    # No dedupe events for non-invoke_skill sync tools.
    deduped_events = [
        e for e in host.events.emitted  # type: ignore[attr-defined]
        if e["type"] == "tool_call_deduped"
    ]
    assert len(deduped_events) == 0


# ---------------------------------------------------------------------------
# G3 fix (dogfood batch 5 B5-M1): dedupe duplicate invoke_skill in same round
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dedupe_duplicate_invoke_skill_in_same_round():
    """Tier 2: OS invariant — duplicate invoke_skill calls with identical
    name+input in one LLM round are deduplicated before dispatch (G3 fix).

    Weak models (observed in B5-M1) emit `invoke_skill` three times in
    the same tool_calls list with identical args, causing 333k tokens /
    51 LLM calls for a single review request. After dedupe, exactly one
    skill invocation runs and a `tool_call_deduped` audit event is emitted
    for each suppressed call.
    """
    host = FakeRouterHost(skills=[
        {"name": "skill_improver", "category": "general"},
    ])
    loop = make_loop(host)

    # Three identical invoke_skill calls in the same round (B5-M1 pattern).
    duplicate_round = tool_result([
        {"id": "tc_a", "name": "invoke_skill",
         "args": {"name": "skill_improver", "input": {"type": "T", "data": {}}}},
        {"id": "tc_b", "name": "invoke_skill",
         "args": {"name": "skill_improver", "input": {"type": "T", "data": {}}}},
        {"id": "tc_c", "name": "invoke_skill",
         "args": {"name": "skill_improver", "input": {"type": "T", "data": {}}}},
    ])
    scripted = _ScriptedLLM([duplicate_round, text_result("done")])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("improve the skill", [])

    # Only one skill invocation — two duplicates suppressed.
    assert len(host.skill_calls) == 1
    assert host.skill_calls[0]["skill"] == "skill_improver"

    # Two audit events for the two suppressed calls.
    deduped_events = [
        e for e in host.events.emitted
        if e["type"] == "tool_call_deduped"
    ]
    assert len(deduped_events) == 2
    for evt in deduped_events:
        assert evt["name"] == "invoke_skill"
        assert evt["reason"] == "duplicate_invoke_skill_in_round"


@pytest.mark.asyncio
async def test_dedupe_does_not_collapse_distinct_invoke_skill_args():
    """Tier 2: OS invariant — invoke_skill calls with different args in
    the same round are NOT deduped (G3 false-positive guard).

    Two invoke_skill calls for different skills (or same skill with
    different inputs) must both execute — they are legitimately distinct
    work items.
    """
    host = FakeRouterHost(skills=[
        {"name": "skill_a", "category": "general"},
        {"name": "skill_b", "category": "general"},
    ])
    loop = make_loop(host)

    distinct_round = tool_result([
        {"id": "tc_a", "name": "invoke_skill",
         "args": {"name": "skill_a", "input": {"type": "T", "data": {"x": 1}}}},
        {"id": "tc_b", "name": "invoke_skill",
         "args": {"name": "skill_b", "input": {"type": "T", "data": {"x": 2}}}},
    ])
    scripted = _ScriptedLLM([distinct_round, text_result("done")])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("run two skills", [])

    # Both skills executed — different args, no collapse.
    assert len(host.skill_calls) == 2
    called = {c["skill"] for c in host.skill_calls}
    assert called == {"skill_a", "skill_b"}

    # No dedupe events.
    deduped_events = [
        e for e in host.events.emitted
        if e["type"] == "tool_call_deduped"
    ]
    assert len(deduped_events) == 0


@pytest.mark.asyncio
async def test_tool_call_deduped_event_emitted_for_invoke_skill():
    """Tier 2: P6 invariant — deduped invoke_skill calls emit
    `tool_call_deduped` events with correct name and reason fields,
    making the dedupe visible in the audit log (P6).
    """
    host = FakeRouterHost(skills=[
        {"name": "my_skill", "category": "general"},
    ])
    loop = make_loop(host)

    duplicate_round = tool_result([
        {"id": "tc_a", "name": "invoke_skill",
         "args": {"name": "my_skill", "input": {"type": "T", "data": {}}}},
        {"id": "tc_b", "name": "invoke_skill",
         "args": {"name": "my_skill", "input": {"type": "T", "data": {}}}},
    ])
    scripted = _ScriptedLLM([duplicate_round, text_result("done")])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("run skill", [])

    deduped_events = [
        e for e in host.events.emitted
        if e["type"] == "tool_call_deduped"
    ]
    assert len(deduped_events) == 1
    evt = deduped_events[0]
    assert evt["name"] == "invoke_skill"
    assert evt["reason"] == "duplicate_invoke_skill_in_round"
    assert evt["chain_id"] == "chain-test"


@pytest.mark.asyncio
async def test_forget_memory_deletes_file_and_regenerates_index():
    """Tier 1: forget_memory tool deletes the memory file and triggers index regeneration."""
    host = FakeRouterHost(file_permissions={"read": ["/memory"], "write": ["/memory"]})
    host._files["/memory/shared/user_role.md"] = "# old memory"
    loop = make_loop(host)

    rounds = [
        tool_result([{
            "name": "forget_memory",
            "args": {"layer": "shared", "slug": "user_role"},
        }]),
        text_result("Forgotten."),
    ]
    scripted = _ScriptedLLM(rounds)

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("forget my role", [])

    assert "/memory/shared/user_role.md" in host.file_deletes
    assert len(host.index_regenerations) == 1
    assert host.outbox[0]["text"] == "Forgotten."


@pytest.mark.asyncio
async def test_history_appended_to_messages():
    """Tier 1: prior history turns appear in LLM messages before the current user utterance, in correct role order."""
    host = FakeRouterHost()
    loop = make_loop(host)

    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]

    messages_seen: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_seen.append(list(messages))
        return text_result("reply")

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=mock_llm):
        await loop.run("new message", history)

    first_call_messages = messages_seen[0]
    roles = [m["role"] for m in first_call_messages]
    # system, history[0], history[1], user
    assert roles == ["system", "user", "assistant", "user"]
    assert first_call_messages[-1]["content"] == "new message"


# ---------------------------------------------------------------------------
# PR36 Layer 1: tool name validation tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_tool_name_returns_error_not_dispatched():
    """Tier 2: OS invariant — tool_call for a name absent from the current catalog returns status=error/kind=unknown_tool; underlying host method is never called.

    LLM emits tool_call with name='delete_file' (not in catalog when
    file.write isn't declared; read_file IS unconditionally exposed
    so we use a write-class tool to exercise the unknown-name path).
    """
    # Host with no file_permissions → write_file / delete_file not in
    # catalog (read_file / list_directory ARE, unconditionally).
    host = FakeRouterHost(
        skills=[{"name": "list_skills", "category": "general"}],
        file_permissions=None,
        mcp_servers=[],
    )
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "delete_file", "args": {"path": "/some/file.txt"}}]),
        text_result("Sorry, let me try differently."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=mock_llm):
        await loop.run("delete the temp file", [])

    # The tool result fed back to the LLM should carry status=error, kind=unknown_tool
    round2_messages = messages_captured[1]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    result_data = json.loads(tool_msgs[0]["content"])
    assert result_data.get("status") == "error"
    error = result_data.get("error", {})
    assert error.get("kind") == "unknown_tool"
    assert "delete_file" in error.get("message", "")

    # Loop recovered and produced a reply
    assert host.outbox[0]["text"] == "Sorry, let me try differently."


@pytest.mark.asyncio
async def test_tool_names_populated_per_run():
    """Tier 1: tool catalog reflects host configuration — write-class file
    tools absent without file.write declaration, present with it.

    Read-class file tools (read_file, list_directory) are unconditional
    by design (= aligned with the OS-level default-grant on paths within
    the project root). Write-class tools (write_file, delete_file) are
    gated on `file_permissions.write` being non-empty.
    """
    host_no_file = FakeRouterHost(file_permissions=None, mcp_servers=[])
    loop = RouterLoop(host=host_no_file, chain_id="chain-test")

    scripted1 = _ScriptedLLM([text_result("ok")])
    with patch("reyn.chat.router_loop.call_llm_tools", scripted1):
        await loop.run("hello", [])

    names_no_file = frozenset(loop._tool_names)
    # read tools always present
    assert "read_file" in names_no_file
    assert "list_directory" in names_no_file
    # write tools gated
    assert "write_file" not in names_no_file
    assert "delete_file" not in names_no_file
    assert "list_skills" in names_no_file  # always present

    # Second run with a host that has file.write permissions
    host_with_write = FakeRouterHost(
        file_permissions={"read": ["/docs"], "write": ["/tmp"]},
        mcp_servers=[],
    )
    loop2 = RouterLoop(host=host_with_write, chain_id="chain-test-2")

    scripted2 = _ScriptedLLM([text_result("ok")])
    with patch("reyn.chat.router_loop.call_llm_tools", scripted2):
        await loop2.run("hello", [])

    names_with_write = frozenset(loop2._tool_names)
    assert "read_file" in names_with_write
    assert "write_file" in names_with_write
    assert "delete_file" in names_with_write


@pytest.mark.asyncio
async def test_known_tool_still_dispatches():
    """Tier 1: valid catalog tool (list_skills) dispatches and returns status=ok with list data. Sanity check that tool name validation does not block legitimate tools."""
    host = FakeRouterHost(
        skills=[{"name": "my_skill", "category": "general"}],
    )
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "list_skills", "args": {"path": ""}}]),
        text_result("Here are the skills."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=mock_llm):
        await loop.run("what skills do you have?", [])

    # The tool result should not be an error
    round2_messages = messages_captured[1]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    result_data = json.loads(tool_msgs[0]["content"])
    # dispatch_tool wraps success as {"status": "ok", "data": <result>}
    assert result_data.get("status") == "ok"
    # list_skills returns a list (categories) inside "data"
    assert isinstance(result_data.get("data"), list)
    assert host.outbox[0]["text"] == "Here are the skills."


# ---------------------------------------------------------------------------
# PR37 Wave 2D: dispatch_tool integration + S13b skill-name validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_tool_emits_tool_called_and_tool_returned_events():
    """Tier 2: P6 invariant — dispatch_tool emits tool_called and tool_returned events on successful skill invocation."""
    host = FakeRouterHost(skills=[{"name": "my_skill", "category": "general"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "my_skill",
            "input": {"type": "Foo", "data": {}},
        }}]),
        text_result("Done!"),
    ]
    scripted = _ScriptedLLM(rounds)

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("run skill", [])

    event_types = [e["type"] for e in host.events.emitted]
    assert "tool_called" in event_types
    assert "tool_returned" in event_types


@pytest.mark.asyncio
async def test_dispatch_tool_emits_tool_failed_on_unknown_tool():
    """Tier 2: P6 invariant — dispatch_tool emits tool_failed event with error_kind=unknown_tool when tool name is not in catalog."""
    host = FakeRouterHost()
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "bogus_unknown_tool", "args": {}}]),
        text_result("Recovered."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=mock_llm):
        await loop.run("try bogus", [])

    event_types = [e["type"] for e in host.events.emitted]
    assert "tool_failed" in event_types
    failed = next(e for e in host.events.emitted if e["type"] == "tool_failed")
    assert failed["error_kind"] == "unknown_tool"


@pytest.mark.asyncio
async def test_invoke_skill_with_unknown_skill_name_rejected():
    """Tier 2: OS invariant — invoke_skill with a hallucinated skill name is rejected
    and emits a deterministic i18n error message; no skill spawned (G10 / B2-M2 fix).

    Layer A (enum) catches it via jsonschema validation → invalid_args.
    If somehow enum is bypassed, Layer B raises ValueError → exception kind.
    Either way, no skill spawn occurs. G10 fix: the router short-circuits and emits
    a deterministic i18n message instead of passing the error back to the LLM.
    """
    host = FakeRouterHost(skills=[{"name": "real_skill", "category": "general"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "ai_article_writer.write_article",  # hallucinated name
            "input": {"type": "T", "data": {}},
        }}]),
        text_result("Ok, trying differently."),  # must NOT be reached after G10 fix
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=mock_llm):
        await loop.run("run bogus skill", [])

    # No skill should have been spawned
    assert len(host.skill_calls) == 0, "No skill must be spawned for unknown name"

    # G10: router exits after the failed tool call — only 1 LLM call (no second round).
    assert len(messages_captured) == 1, (
        "G10 fix: LLM must NOT be called a second time after invoke_skill error; "
        f"got {len(messages_captured)} call(s)"
    )

    # Outbox must contain a deterministic error message (not the LLM-generated fallback).
    assert host.outbox, "Expected at least one outbox message"
    agent_msgs = [m for m in host.outbox if m.get("kind") == "agent"]
    assert agent_msgs, f"Expected agent-kind outbox message; got: {host.outbox}"
    text = agent_msgs[0]["text"]
    assert "Tool call failed" in text, (
        f"Expected deterministic English error message; got: {text!r}"
    )


@pytest.mark.asyncio
async def test_invoke_skill_with_known_name_dispatches():
    """Tier 1: invoke_skill with a valid skill name dispatches and produces text reply. Happy-path sanity check for skill name validation."""
    host = FakeRouterHost(skills=[{"name": "real_skill", "category": "general"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "real_skill",
            "input": {"type": "T", "data": {"key": "value"}},
        }}]),
        text_result("Skill ran."),
    ]
    scripted = _ScriptedLLM(rounds)

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("run real skill", [])

    assert len(host.skill_calls) == 1
    assert host.skill_calls[0]["skill"] == "real_skill"
    assert host.outbox[0]["text"] == "Skill ran."


@pytest.mark.asyncio
async def test_invoke_skill_layer_b_catches_bypass():
    """Tier 2: OS invariant — Layer B defense raises ValueError for unknown skill name even when enum validation is bypassed; skill name validation is enforced at dispatch time.

    Simulate by calling _invoke_router_tool directly with a name not in skills.
    """
    host = FakeRouterHost(skills=[{"name": "real_skill", "category": "general"}])
    loop = RouterLoop(host=host, chain_id="chain-test")

    # Prime the catalog (normally done in run())
    from reyn.chat.router_tools import build_tools
    tools = build_tools(host.list_available_skills(), host.list_available_agents())
    loop._catalog = {t["function"]["name"]: t for t in tools}
    loop._tool_names = frozenset(loop._catalog.keys())

    with pytest.raises(ValueError, match="not found"):
        await loop._invoke_router_tool(
            "invoke_skill",
            {"name": "hallucinated_skill", "input": {"type": "T", "data": {}}},
        )


# ---------------------------------------------------------------------------
# B3-M2 fix: _list_skills name-lookup fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_skills_name_lookup_fallback():
    """Tier 2: OS invariant — _list_skills returns a 1-item list when path matches a skill name but no category.

    Covers the B3-M2 bug: LLM calls list_skills(path="read_local_files")
    but all skills have no category set (defaulting to "general"), so
    the category filter returns 0 results. The name-lookup fallback must
    return the matching skill entry instead of an empty list.
    """
    skills = [
        {"name": "read_local_files", "description": "Read files from local FS"},
        {"name": "write_blog", "description": "Write a blog post", "category": "write"},
    ]
    host = FakeRouterHost(skills=skills)
    loop = make_loop(host)

    result = loop._list_skills("read_local_files")

    assert len(result) == 1
    assert result[0]["name"] == "read_local_files"
    assert result[0]["description"] == "Read files from local FS"


@pytest.mark.asyncio
async def test_list_skills_unknown_path_returns_empty():
    """Tier 2: OS invariant — _list_skills returns empty list when path matches neither a category nor a skill name.

    Regression guard: unknown path must still return [] (existing contract).
    """
    skills = [
        {"name": "read_local_files", "description": "Read files"},
        {"name": "write_blog", "description": "Write blog", "category": "write"},
    ]
    host = FakeRouterHost(skills=skills)
    loop = make_loop(host)

    result = loop._list_skills("nonexistent_category_or_skill")

    assert result == []


@pytest.mark.asyncio
async def test_list_skills_empty_path_returns_all_categories():
    """Tier 2: OS invariant — _list_skills('') groups all skills by category and returns [{category, count}] entries.

    Regression guard for existing empty-path behaviour: skills without an
    explicit category fall into "general".
    """
    skills = [
        {"name": "read_local_files"},           # no category → "general"
        {"name": "write_blog", "category": "write"},
        {"name": "write_email", "category": "write"},
    ]
    host = FakeRouterHost(skills=skills)
    loop = make_loop(host)

    result = loop._list_skills("")

    by_cat = {r["category"]: r["count"] for r in result}
    assert by_cat == {"general": 1, "write": 2}


@pytest.mark.asyncio
async def test_no_events_attribute_needed_for_unknown_tool_path():
    """Tier 2: P6 invariant — unknown tool error emits tool_failed via host.events through dispatch_tool; event routing is not bypassed on the error path."""
    host = FakeRouterHost()
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "nonexistent_tool", "args": {}}]),
        text_result("Recovered."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=mock_llm):
        await loop.run("try nonexistent", [])

    round2_messages = messages_captured[1]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    result_data = json.loads(tool_msgs[0]["content"])
    assert result_data.get("status") == "error"
    assert result_data["error"]["kind"] == "unknown_tool"
    # events were emitted
    assert any(e["type"] == "tool_failed" for e in host.events.emitted)
