"""Shared RouterLoop test helpers: FakeRouterHost, scripted LLM, result builders.

Uses FakeRouterHost and a scripted callable (ScriptedLLM) to return scripted
LLMToolCallResult sequences without hitting the network.

No unittest.mock.AsyncMock / MagicMock / patch(new_callable=AsyncMock) are
used. patch() is only called with real callables (policy: Mock vs Fake).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoop
from reyn.runtime.services import MemoryService

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
        threat_scan: Any = None,
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
        self.spawn_calls: list[dict] = []
        self.file_writes: list[tuple[str, str]] = []
        self.file_deletes: list[str] = []
        self.file_reads: list[str] = []
        self.index_regenerations: list[dict] = []

        # In-memory "file system"
        self._files: dict[str, str] = {}

        # Events (required by RouterLoopHost protocol for dispatch_tool)
        self._events = FakeEventLog()

        # The memory-store capability (#3607). A REAL MemoryService — the
        # class production wires — over this host's in-memory file callbacks,
        # so a memory test exercises the real domain rules (threat scan,
        # frontmatter, listing-index regen) instead of a hand-mirrored copy.
        # ``knowledge_sync`` is left None: the embedding index is a different
        # subsystem with its own tests.
        self._memory = MemoryService(
            agent_workspace_dir=Path(".reyn") / "agents" / self.agent_name,
            events=self._events,
            file_write=self._file_write,
            file_read=self._file_read,
            file_delete=self._file_delete,
            file_regenerate_index=self._file_regenerate_index,
            threat_scan=threat_scan,
        )

    @property
    def events(self) -> FakeEventLog:
        return self._events

    @property
    def memory(self) -> MemoryService:
        return self._memory

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

    async def reyn_repo_list(self, *, path: str) -> dict:
        return {"path": path, "entries": []}

    async def reyn_repo_read(self, *, path: str) -> dict:
        return {"path": path, "content": ""}

    async def web_search(self, *, query: str, max_results: int) -> dict:
        return {"kind": "web_search", "query": query, "results": []}

    async def web_fetch(self, *, url: str) -> dict:
        return {"kind": "web_fetch", "url": url, "status": "ok", "content": ""}

    # --- Action callbacks ---

    async def run_skill_awaitable(self, *, skill: str, input: dict,
                                   chain_id: str) -> dict:
        self.skill_calls.append({"skill": skill, "input": input, "chain_id": chain_id})
        return {"status": "ok", "skill": skill}

    async def send_to_agent(self, *, to: str, request: str, depth: int,
                            chain_id: str) -> None:
        self.agent_sends.append({"to": to, "request": request, "depth": depth,
                                  "chain_id": chain_id})

    async def spawn_session(self, *, request: str, mode: str,
                            narrowing: "dict | None", chain_id: str) -> dict:
        # #2103 S1bc / #2120: multi-session host hook (duck-typed; RouterLoop binds
        # spawn_session_fn only when this method exists). Records the spawn + returns
        # an ack — lets a dispatch test prove session_spawn reaches the handler.
        self.spawn_calls.append({"request": request, "mode": mode,
                                  "narrowing": narrowing, "chain_id": chain_id})
        return {"status": "ok", "kind": "session_spawned", "mode": mode}

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        self.outbox.append({"kind": kind, "text": text, "meta": meta})

    # --- File callbacks (the memory capability's, not the host's) ---
    #
    # #3607: these are no longer RouterLoopHost methods — the router host does
    # not expose file primitives. They are the callbacks this host hands to
    # its MemoryService, and their return shapes mirror Session._file_* (the
    # production wiring): always a dict, never a raise, with the
    # ``written`` / ``deleted`` / ``error`` keys the real wrappers return. A
    # shape that did not mirror them would let a memory test pass against a
    # contract production does not honour.

    async def _file_read(self, path: str) -> dict:
        self.file_reads.append(path)
        if path not in self._files:
            return {"error": f"file not found: {path}"}
        return {"path": path, "content": self._files[path]}

    async def _file_write(self, path: str, content: str) -> dict:
        self.file_writes.append((path, content))
        self._files[path] = content
        return {"path": path, "written": True}

    async def _file_delete(self, path: str) -> dict:
        self.file_deletes.append(path)
        existed = path in self._files
        self._files.pop(path, None)
        return {"path": path, "deleted": existed}

    async def file_list_directory(self, path: str) -> list[dict]:
        return [{"name": "file.txt", "type": "file"}]

    async def _file_regenerate_index(self, *, path: str, output_path: str,
                                     entry_template: str, header: str) -> dict:
        self.index_regenerations.append({
            "path": path,
            "output_path": output_path,
            "entry_template": entry_template,
            "header": header,
        })
        return {"path": path, "output_path": output_path, "entries": 0}

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


class RaisingLLM:
    """Real callable replacing call_llm_tools that always raises.

    Same policy standing as :class:`ScriptedLLM` (a real ``__call__``, so
    signature drift still raises ``TypeError``); this one lets a test pin
    the *failure* leg of a path that degrades when the LLM is unavailable
    — without depending on the ambient environment lacking credentials
    (#3382: four tests were green only because CI has no API key).
    """

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error or RuntimeError("scripted LLM failure")
        self.call_count: int = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.call_count += 1
        raise self.error
