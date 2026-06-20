"""Shared RouterLoop test helpers: FakeRouterHost, scripted LLM, result builders.

Uses FakeRouterHost and a scripted callable (ScriptedLLM) to return scripted
LLMToolCallResult sequences without hitting the network.

No unittest.mock.AsyncMock / MagicMock / patch(new_callable=AsyncMock) are
used. patch() is only called with real callables (policy: Mock vs Fake).
"""
from __future__ import annotations

import json
from typing import Any

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop

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

    async def reyn_src_list(self, *, path: str) -> dict:
        return {"path": path, "entries": []}

    async def reyn_src_read(self, *, path: str) -> dict:
        return {"path": path, "content": ""}

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return {"kind": "web_search", "query": query, "results": []}

    async def web_fetch(self, *, url: str, max_length: int) -> dict:
        return {"kind": "web_fetch", "url": url, "status": "ok", "content": ""}

    # --- Memory paths ---

    def memory_path(self, layer: str, slug: str) -> str:
        # Match production Session._memory_path contract: appends .md.
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

EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=EMPTY_USAGE,
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
        usage=EMPTY_USAGE,
    )


def make_loop(host: FakeRouterHost, max_iterations: int = 5) -> RouterLoop:
    return RouterLoop(host=host, chain_id="chain-test", max_iterations=max_iterations)


class ScriptedLLM:
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
