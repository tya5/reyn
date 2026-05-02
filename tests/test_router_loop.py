"""Unit tests for RouterLoop (PR35 wave-2 task D).

Uses FakeRouterHost and monkeypatches call_llm_tools to return scripted
LLMToolCallResult sequences without hitting the network.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chitchat_no_tools():
    """call_llm_tools returns text; assert put_outbox called once, no tools."""
    host = FakeRouterHost()
    loop = make_loop(host)

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = text_result("hello")
        await loop.run("hi", [])

    assert len(host.outbox) == 1
    assert host.outbox[0]["kind"] == "agent"
    assert host.outbox[0]["text"] == "hello"
    assert len(host.skill_calls) == 0
    mock_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_skill_round():
    """Round 1: tool_call invoke_skill; round 2: text reply."""
    host = FakeRouterHost(skills=[{"name": "my_skill", "category": "general"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "my_skill",
            "input": {"type": "Foo", "data": {}},
        }}]),
        text_result("Done!"),
    ]

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = rounds
        await loop.run("run my skill", [])

    assert len(host.skill_calls) == 1
    assert host.skill_calls[0]["skill"] == "my_skill"
    assert host.skill_calls[0]["chain_id"] == "chain-test"

    assert len(host.outbox) == 1
    assert host.outbox[0]["text"] == "Done!"
    assert mock_llm.await_count == 2


@pytest.mark.asyncio
async def test_two_round_sequential():
    """Round 1: read_file; round 2: invoke_skill (uses read content); round 3: text."""
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
    """Round 1 returns 2 tool_calls; assert both executed concurrently."""
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

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = rounds
        await loop.run("run both", [])

    assert len(host.skill_calls) == 2
    called_skills = {c["skill"] for c in host.skill_calls}
    assert called_skills == {"skill_a", "skill_b"}
    assert host.outbox[0]["text"] == "Both done."


@pytest.mark.asyncio
async def test_max_iterations_exhausted():
    """call_llm_tools always returns tool_calls; error outbox after max_iterations."""
    host = FakeRouterHost()
    loop = make_loop(host, max_iterations=3)

    # Always return a tool call (unknown tool to avoid side effects)
    always_tool = tool_result([{"name": "bogus_tool", "args": {}}])

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = always_tool
        await loop.run("do stuff", [])

    assert mock_llm.await_count == 3
    assert len(host.outbox) == 1
    assert host.outbox[0]["kind"] == "error"
    assert "max iterations" in host.outbox[0]["text"]
    assert "3" in host.outbox[0]["text"]


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_in_result():
    """tool_call with unknown name; assert tool result contains 'unknown tool', loop continues."""
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
    """invoke remember_shared; assert host.file_write + file_regenerate_index called."""
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

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = rounds
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
    """_list_skills('') groups by category and returns category+count entries."""
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
    """_list_skills('write') returns the 2 write skills."""
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
    """_list_memory('') returns [{path: 'shared', count}, {path: 'agent', count}]."""
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
    """invoke delegate_to_agent; assert host.send_to_agent called correctly."""
    host = FakeRouterHost(agents=[{"name": "peer_agent", "role": "data agent"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{
            "name": "delegate_to_agent",
            "args": {"to": "peer_agent", "request": "please process the data"},
        }]),
        text_result("Delegated."),
    ]

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = rounds
        await loop.run("send to peer", [])

    assert len(host.agent_sends) == 1
    assert host.agent_sends[0]["to"] == "peer_agent"
    assert host.agent_sends[0]["request"] == "please process the data"
    assert host.agent_sends[0]["chain_id"] == "chain-test"
    assert host.outbox[0]["text"] == "Delegated."


@pytest.mark.asyncio
async def test_forget_memory_deletes_file_and_regenerates_index():
    """invoke forget_memory; assert file_delete + file_regenerate_index called."""
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

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = rounds
        await loop.run("forget my role", [])

    assert "/memory/shared/user_role.md" in host.file_deletes
    assert len(host.index_regenerations) == 1
    assert host.outbox[0]["text"] == "Forgotten."


@pytest.mark.asyncio
async def test_history_appended_to_messages():
    """Prior history turns appear in the messages before the user utterance."""
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
    """LLM emits tool_call with name='read_file' (not in catalog for no-file host).

    Assert: returned tool_result has status='error' and kind='unknown_tool',
    and host.file_read is never called.
    """
    # Host with no file_permissions → read_file not in catalog
    host = FakeRouterHost(
        skills=[{"name": "list_skills", "category": "general"}],
        file_permissions=None,  # no file tools
        mcp_servers=[],
    )
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "read_file", "args": {"path": "/some/file.txt"}}]),
        text_result("Sorry, let me try differently."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=mock_llm):
        await loop.run("read README.md", [])

    # host.file_read must NOT have been called
    assert len(host.file_reads) == 0, "file_read must not be called for unknown tool"

    # The tool result fed back to the LLM should carry status=error, kind=unknown_tool
    round2_messages = messages_captured[1]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    result_data = json.loads(tool_msgs[0]["content"])
    assert result_data.get("status") == "error"
    error = result_data.get("error", {})
    assert error.get("kind") == "unknown_tool"
    assert "read_file" in error.get("message", "")

    # Loop recovered and produced a reply
    assert host.outbox[0]["text"] == "Sorry, let me try differently."


@pytest.mark.asyncio
async def test_tool_names_populated_per_run():
    """_tool_names reflects the host's configuration on each run() call.

    First run: no file permissions, no MCP → file/mcp tools absent.
    Second run: with file permissions → file tools present.
    """
    host_no_file = FakeRouterHost(file_permissions=None, mcp_servers=[])
    loop = RouterLoop(host=host_no_file, chain_id="chain-test")

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = text_result("ok")
        await loop.run("hello", [])

    names_no_file = frozenset(loop._tool_names)
    assert "read_file" not in names_no_file
    assert "list_skills" in names_no_file  # always present

    # Second run with a host that has file permissions
    host_with_file = FakeRouterHost(
        file_permissions={"read": ["/docs"], "write": []},
        mcp_servers=[],
    )
    loop2 = RouterLoop(host=host_with_file, chain_id="chain-test-2")

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = text_result("ok")
        await loop2.run("hello", [])

    names_with_file = frozenset(loop2._tool_names)
    assert "read_file" in names_with_file


@pytest.mark.asyncio
async def test_known_tool_still_dispatches():
    """Sanity check: a valid catalog tool (list_skills) dispatches normally."""
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
    """dispatch_tool emits tool_called and tool_returned events on success."""
    host = FakeRouterHost(skills=[{"name": "my_skill", "category": "general"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "my_skill",
            "input": {"type": "Foo", "data": {}},
        }}]),
        text_result("Done!"),
    ]

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = rounds
        await loop.run("run skill", [])

    event_types = [e["type"] for e in host.events.emitted]
    assert "tool_called" in event_types
    assert "tool_returned" in event_types


@pytest.mark.asyncio
async def test_dispatch_tool_emits_tool_failed_on_unknown_tool():
    """dispatch_tool emits tool_failed on unknown_tool error."""
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
    """invoke_skill with a hallucinated skill name is rejected.

    Layer A (enum) catches it via jsonschema validation → invalid_args.
    If somehow enum is bypassed, Layer B raises ValueError → exception kind.
    Either way, no skill spawn occurs.
    """
    host = FakeRouterHost(skills=[{"name": "real_skill", "category": "general"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "ai_article_writer.write_article",  # hallucinated name
            "input": {"type": "T", "data": {}},
        }}]),
        text_result("Ok, trying differently."),
    ]

    messages_captured: list[list[dict]] = []

    async def mock_llm(*, messages, **kwargs):
        messages_captured.append(list(messages))
        return rounds[len(messages_captured) - 1]

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=mock_llm):
        await loop.run("run bogus skill", [])

    # No skill should have been spawned
    assert len(host.skill_calls) == 0, "No skill must be spawned for unknown name"

    # Tool result must be an error (either invalid_args from enum or exception from Layer B)
    round2_messages = messages_captured[1]
    tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    result_data = json.loads(tool_msgs[0]["content"])
    assert result_data.get("status") == "error"
    error_kind = result_data.get("error", {}).get("kind", "")
    assert error_kind in ("invalid_args", "exception"), (
        f"Expected invalid_args or exception, got {error_kind!r}"
    )

    assert host.outbox[0]["text"] == "Ok, trying differently."


@pytest.mark.asyncio
async def test_invoke_skill_with_known_name_dispatches():
    """invoke_skill with a valid skill name runs successfully (happy path)."""
    host = FakeRouterHost(skills=[{"name": "real_skill", "category": "general"}])
    loop = make_loop(host)

    rounds = [
        tool_result([{"name": "invoke_skill", "args": {
            "name": "real_skill",
            "input": {"type": "T", "data": {"key": "value"}},
        }}]),
        text_result("Skill ran."),
    ]

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = rounds
        await loop.run("run real skill", [])

    assert len(host.skill_calls) == 1
    assert host.skill_calls[0]["skill"] == "real_skill"
    assert host.outbox[0]["text"] == "Skill ran."


@pytest.mark.asyncio
async def test_invoke_skill_layer_b_catches_bypass():
    """Layer B defense: even if enum validation is skipped (no skills in enum),
    an unknown skill name raises ValueError → exception error kind.

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


@pytest.mark.asyncio
async def test_no_events_attribute_needed_for_unknown_tool_path():
    """Regression: unknown tool error still uses events from host.events via dispatch_tool."""
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
