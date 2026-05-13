"""RouterLoop replay / unit tests (PR35 Wave 3 Task G).

Replaces the old skill_router phase-based tests with tests that exercise the
RouterLoop + native tool_use path.

Strategy
--------
- Semantic / E2E tests (chitchat, skill invocation, memory, delegation) →
  ``@pytest.mark.replay`` with fixtures in ``tests/fixtures/llm/router/``.
  LLMReplay intercepts ``litellm.acompletion`` and either replays a recorded
  response or records a new one on first run (REYN_LLM_RECORD=1 or missing fixture).

- Pathology / structural tests (max_iterations, parallel dispatch, tools catalog
  inspection) → direct monkeypatch on ``reyn.chat.router_loop.call_llm_tools``.
  These tests verify RouterLoop structural invariants without needing a real LLM
  call, so they don't need fixtures.

Fixture directory: ``tests/fixtures/llm/router/`` (created on first record run).
Old ``tests/fixtures/llm/skill_router/`` is preserved until Wave H.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.chat.router_system_prompt import build_system_prompt
from reyn.chat.router_tools import build_tools
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage

# ---------------------------------------------------------------------------
# Minimal events stub
# ---------------------------------------------------------------------------

class _FakeEventLog:
    """Minimal events stub: records emitted events, no subscribers."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, type: str, **data) -> None:
        self.emitted.append({"type": type, **data})


# ---------------------------------------------------------------------------
# FakeRouterHost (shared with test_router_loop.py — kept in sync)
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

        # Events stub for dispatch_tool
        self._events = _FakeEventLog()

    @property
    def events(self) -> "_FakeEventLog":
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
        # Match production ChatSession._memory_path: appends .md.
        return f"/memory/{layer}/{slug}.md"

    def memory_dir(self, layer: str) -> str:
        return f"/memory/{layer}"

    # --- Action callbacks ---

    async def run_skill_awaitable(self, *, skill: str, input: dict,
                                   chain_id: str) -> dict:
        self.skill_calls.append({"skill": skill, "input": input, "chain_id": chain_id})
        return {"status": "finished", "data": {"result": f"{skill} ran"}}

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
        # Return the bare model name (no provider prefix).
        # LLMReplay computes the fixture key from the model string passed to
        # litellm.acompletion.  call_llm_tools only strips the provider prefix
        # when LITELLM_API_BASE is set (proxy_kwargs non-empty).  By returning
        # the bare name here the key is identical in both record mode (proxy
        # active, no stripping needed) and replay mode (no proxy).
        return "gemini-2.5-flash-lite"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


def _tool_result(calls: list[dict]) -> LLMToolCallResult:
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
        usage=_EMPTY_USAGE,
    )


def _make_loop(host: FakeRouterHost, max_iterations: int = 5) -> RouterLoop:
    return RouterLoop(host=host, chain_id="chain-test", max_iterations=max_iterations)


# ---------------------------------------------------------------------------
# ── Semantic / E2E tests (LLMReplay) ────────────────────────────────────────
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason=(
        "FP-0022 changed router system prompt (web_fetch always in catalog). "
        "Fixture args_hash needs re-recording with LITELLM proxy access. "
        "Test passes locally when .reyn/index/ matches recording cwd; fails on "
        "CI's clean cwd. Re-record with REYN_LLM_RECORD=1 from a clean cwd "
        "(no .reyn/index/sources.yaml) to make deterministic. Tracked under "
        "FP-0022 follow-up."
    ),
    strict=False,
)
@pytest.mark.replay("fixtures/llm/router/chitchat.jsonl")
@pytest.mark.asyncio
async def test_chitchat_text_reply():
    """LLM returns text directly with no tool_calls; outbox has kind='agent'."""
    host = FakeRouterHost(
        skills=[
            {"name": "text_summariser", "description": "Summarises text.", "category": "general"},
        ],
    )
    loop = _make_loop(host)

    await loop.run("Hi! How are you?", [])

    assert len(host.outbox) == 1, f"Expected 1 outbox entry, got: {host.outbox}"
    msg = host.outbox[0]
    assert msg["kind"] == "agent", f"Expected kind='agent', got: {msg['kind']}"
    assert isinstance(msg["text"], str) and len(msg["text"]) > 0, (
        "Expected non-empty text reply for chitchat"
    )
    assert len(host.skill_calls) == 0, (
        f"Chitchat should not invoke any skill; got: {host.skill_calls}"
    )


@pytest.mark.xfail(
    reason="FP-0022 fixture re-record needed (see test_chitchat_text_reply).",
    strict=False,
)
@pytest.mark.replay("fixtures/llm/router/invoke_skill_single_round.jsonl")
@pytest.mark.asyncio
async def test_invoke_skill_single_round():
    """LLM calls invoke_skill then returns text; host.run_skill_awaitable called once."""
    host = FakeRouterHost(
        skills=[
            {"name": "text_summariser", "description": "Summarises text.", "category": "general"},
        ],
    )
    loop = _make_loop(host)

    await loop.run(
        "Use the text_summariser skill to summarise: The quick brown fox jumps over the lazy dog.",
        [],
    )

    # The LLM must have called invoke_skill at least once before producing text.
    # We assert that a skill was invoked (not which name) because the LLM picks
    # the skill name from the catalogue; exact name matching would be brittle.
    assert len(host.skill_calls) >= 1, (
        f"Expected at least 1 skill call; got: {host.skill_calls}"
    )
    assert host.skill_calls[0]["chain_id"] == "chain-test"

    # Final outbox entry is text
    assert len(host.outbox) == 1
    assert host.outbox[0]["kind"] == "agent"
    assert len(host.outbox[0]["text"]) > 0


@pytest.mark.asyncio
async def test_delegate_to_agent():
    """RouterLoop dispatches LLM-emitted delegate_to_agent to host.send_to_agent.

    Uses direct monkeypatch (not LLMReplay) because PR37 wave 2D removed the
    enum attractor on `delegate_to_agent.to`, so the LLM no longer reliably
    picks the agent name from a recorded prompt — we'd be testing LLM behavior
    rather than dispatch correctness. Direct mock pins the dispatch path.
    """
    host = FakeRouterHost(
        agents=[{"name": "researcher", "role": "research agent", "cluster": "default"}],
    )
    loop = _make_loop(host)

    rounds = [
        _tool_result([{
            "name": "delegate_to_agent",
            "args": {"to": "researcher",
                     "request": "find info on climate change"},
        }]),
        _text_result("Delegated to researcher."),
    ]
    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = rounds
        await loop.run(
            "Delegate to the researcher: find info on climate change.",
            [],
        )

    assert len(host.agent_sends) >= 1, (
        f"Expected at least 1 delegate call; got: {host.agent_sends}"
    )
    assert host.agent_sends[0]["to"] == "researcher"
    assert host.agent_sends[0]["chain_id"] == "chain-test"
    assert isinstance(host.agent_sends[0]["request"], str)

    # After delegate dispatch, RouterLoop exits with an "awaiting peer
    # reply" status note; the peer's actual response comes back later via
    # pending_chain (PR14) which re-invokes the router.
    assert len(host.outbox) == 1
    assert host.outbox[0]["kind"] == "status"
    assert "awaiting peer reply" in host.outbox[0]["text"]


@pytest.mark.xfail(
    reason="FP-0022 fixture re-record needed (see test_chitchat_text_reply).",
    strict=False,
)
@pytest.mark.replay("fixtures/llm/router/memory_recall.jsonl")
@pytest.mark.asyncio
async def test_memory_recall_via_list_then_read():
    """LLM lists memory then reads a body; sequence produces final text reply."""
    memory_content = (
        "# Memory Index (shared)\n\n"
        "- [User Role](user_role.md) — The user is a senior developer.\n"
    )
    host = FakeRouterHost(
        memory_index={"status": "ok", "content": memory_content},
    )
    # Seed the in-memory file system so file_read works
    host._files["/memory/shared/user_role.md"] = (
        "---\nname: User Role\ndescription: The user is a senior developer.\n"
        "type: user\n---\n\nThe user is a senior developer working on agent OS.\n"
    )

    loop = _make_loop(host)

    await loop.run("What do you know about my role?", [])

    # At least one list_memory or read_memory_body call should have happened
    # (we track these via file_reads since read_memory_body calls host.file_read)
    # The final outbox must have a text reply
    assert len(host.outbox) == 1
    assert host.outbox[0]["kind"] == "agent"
    assert len(host.outbox[0]["text"]) > 0


# ---------------------------------------------------------------------------
# ── Pathology / structural tests (direct monkeypatch) ───────────────────────
# These don't need recorded fixtures because LLM behavior is scripted.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_iterations_aborts_with_error():
    """Every LLM round returns tool_calls; error outbox emitted after max_iterations."""
    host = FakeRouterHost()
    loop = _make_loop(host, max_iterations=3)

    always_tool = _tool_result([{"name": "bogus_tool", "args": {}}])

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = always_tool
        await loop.run("do stuff forever", [])

    assert mock_llm.await_count == 3, (
        f"Expected exactly 3 LLM calls (max_iterations), got {mock_llm.await_count}"
    )
    assert len(host.outbox) == 1
    assert host.outbox[0]["kind"] == "error"
    assert "max iterations" in host.outbox[0]["text"].lower() or "3" in host.outbox[0]["text"]


@pytest.mark.asyncio
async def test_parallel_tool_calls_in_one_round():
    """One LLM round with 2 tool_calls; both executed before next round."""
    host = FakeRouterHost(
        skills=[
            {"name": "skill_a", "category": "general"},
            {"name": "skill_b", "category": "general"},
        ]
    )
    loop = _make_loop(host)

    rounds = [
        _tool_result([
            {"id": "tc_0", "name": "invoke_skill",
             "args": {"name": "skill_a", "input": {"type": "X", "data": {}}}},
            {"id": "tc_1", "name": "invoke_skill",
             "args": {"name": "skill_b", "input": {"type": "Y", "data": {}}}},
        ]),
        _text_result("Both skills ran successfully."),
    ]

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = rounds
        await loop.run("run both skills", [])

    assert len(host.skill_calls) == 2, (
        f"Both skills must be called; got: {[c['skill'] for c in host.skill_calls]}"
    )
    called_skills = {c["skill"] for c in host.skill_calls}
    assert called_skills == {"skill_a", "skill_b"}
    assert host.outbox[0]["text"] == "Both skills ran successfully."


@pytest.mark.asyncio
async def test_tools_param_includes_only_allowed_skills():
    """The tools= sent to call_llm_tools are built from host.list_available_skills() only.

    Sets up a host with 2 skills. Captures the tools= argument on the first LLM
    call and verifies the tool catalog reflects the host's skill list.
    Specifically: build_tools is called with host's skills, so any additional
    skill NOT in the host is absent from the resulting tool spec.
    """
    allowed_skills = [
        {"name": "read_local_files", "description": "Read local files.", "category": "file"},
        {"name": "text_summariser", "description": "Summarises text.", "category": "general"},
    ]
    host = FakeRouterHost(skills=allowed_skills)
    loop = _make_loop(host)

    captured_tools: list[dict] = []
    captured_system: list[str] = []

    async def capturing_llm(*, model, messages, tools, tool_choice, **kwargs):
        captured_tools.extend(tools)
        # System prompt is always the first message
        if messages and messages[0]["role"] == "system":
            captured_system.append(messages[0]["content"])
        return _text_result("Done.")

    with patch("reyn.chat.router_loop.call_llm_tools", side_effect=capturing_llm):
        await loop.run("hello", [])

    # The tool catalog must have been passed (non-empty)
    assert len(captured_tools) > 0, "tools= must be non-empty"

    # Build the expected tool catalog directly and compare names
    expected_tools = build_tools(
        allowed_skills,
        [],  # no agents
        file_permissions=None,
        mcp_servers=None,
    )
    expected_tool_names = {t["function"]["name"] for t in expected_tools}
    actual_tool_names = {t["function"]["name"] for t in captured_tools}
    assert actual_tool_names == expected_tool_names, (
        f"Tool names mismatch.\nExpected: {sorted(expected_tool_names)}\n"
        f"Actual:   {sorted(actual_tool_names)}"
    )

    # A skill NOT in the allowlist must not appear in the system prompt skill summary
    # The system prompt lists skill categories; "text_summariser" / "read_local_files"
    # categories should appear and no phantom skill names should be present.
    system_text = captured_system[0] if captured_system else ""
    # "disallowed_secret_skill" is not in the host — verify it's absent
    assert "disallowed_secret_skill" not in system_text


@pytest.mark.asyncio
async def test_validator_anchor_unchanged():
    """System prompt lists only the host's allowed skill categories; absent skills are absent.

    Build a host with skills=[A, B]; render system prompt; assert both categories
    appear and a phantom category 'phantom' is absent.
    """
    allowed_skills = [
        {"name": "skill_alpha", "description": "Alpha skill.", "category": "analytics"},
        {"name": "skill_beta", "description": "Beta skill.", "category": "analytics"},
    ]
    host = FakeRouterHost(skills=allowed_skills)

    prompt = build_system_prompt(
        agent_name=host.agent_name,
        agent_role=host.agent_role,
        available_skills=host.list_available_skills(),
        available_agents=host.list_available_agents(),
        memory_index=host.get_memory_index(),
        file_permissions=host.get_file_permissions(),
        mcp_servers=host.get_mcp_servers(),
    )

    # The analytics category with 2 skills must appear
    assert "analytics (2)" in prompt, (
        f"Expected 'analytics (2)' in system prompt.\nPrompt: {prompt[:500]}"
    )
    # A phantom category must not appear
    assert "phantom" not in prompt, (
        "Phantom skill category must not appear in system prompt"
    )
    # When no skills exist for a category, it's absent from the prompt
    host_no_skills = FakeRouterHost(skills=[])
    prompt_empty = build_system_prompt(
        agent_name=host_no_skills.agent_name,
        agent_role=host_no_skills.agent_role,
        available_skills=[],
        available_agents=[],
        memory_index=host_no_skills.get_memory_index(),
        file_permissions=None,
        mcp_servers=None,
    )
    assert "(none)" in prompt_empty, (
        "Empty skill list must render '(none)' in system prompt"
    )


@pytest.mark.asyncio
async def test_invoke_skill_then_remember():
    """Round 1: invoke_skill; round 2: remember_shared; round 3: text reply.

    Verifies the multi-step sequence: skill run + memory write + final outbox.
    Uses direct monkeypatch (no fixture needed — fully scripted behavior).
    """
    host = FakeRouterHost(
        skills=[{"name": "text_summariser", "category": "general"}],
        file_permissions={"read": ["/memory"], "write": ["/memory"]},
    )
    loop = _make_loop(host)

    rounds = [
        _tool_result([{"name": "invoke_skill", "args": {
            "name": "text_summariser",
            "input": {"type": "user_message", "data": {"text": "summarise this"}},
        }}]),
        _tool_result([{
            "name": "remember_shared",
            "args": {
                "slug": "project_goal",
                "name": "Project Goal",
                "description": "Build a reliable agent OS",
                "type": "project",
                "body": "The project goal is to build a reliable agent OS.",
            },
        }]),
        _text_result("I ran the summariser and saved the project goal."),
    ]

    with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
        mock_llm.side_effect = rounds
        await loop.run(
            "Summarise this and remember the project goal.", []
        )

    # Skill ran
    assert len(host.skill_calls) == 1
    assert host.skill_calls[0]["skill"] == "text_summariser"

    # Memory written
    written_paths = [p for p, _ in host.file_writes]
    assert "/memory/shared/project_goal.md" in written_paths

    # Index regenerated
    assert len(host.index_regenerations) == 1

    # Final text outbox
    assert len(host.outbox) == 1
    assert host.outbox[0]["kind"] == "agent"
    assert "summariser" in host.outbox[0]["text"].lower() or len(host.outbox[0]["text"]) > 0


# ---------------------------------------------------------------------------
# ── B11-R3 fix: named skill → direct invoke_skill (no list_skills first) ────
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    reason="FP-0022 fixture re-record needed (see test_chitchat_text_reply).",
    strict=False,
)
@pytest.mark.replay("fixtures/llm/router/named_skill_direct_invoke.jsonl")
@pytest.mark.asyncio
async def test_named_skill_direct_invoke_without_list_skills():
    """Tier 3: B11-R3 fix — when user names a skill that appears in the
    Available skills list, router calls invoke_skill directly (no list_skills hop).

    Root cause of B9-NEW-3 / B10-NEW-2 text-reply non-determinism:
    - Old prompt required 'call list_skills first, then invoke_skill'.
    - Weak LLM (gemini-2.5-flash-lite) sometimes fell through to Reply intent
      after the mandatory list_skills hop, producing a clarification text reply
      instead of invoking the skill.
    - The multi-verb Japanese input 'review して改善案を出して' combined with a
      second entity name ('direct_llm') triggered the 'need clarification' path.

    Fix (B11-R3): system prompt now says 'If the user names a skill in the
    Available skills list, call invoke_skill directly (skip list_skills)'.

    This test verifies the contract: the router must call invoke_skill (not
    produce a text reply or clarification) when skill name is explicit in the
    user message and in the Available skills list.
    """
    host = FakeRouterHost(
        skills=[
            {
                "name": "skill_improver",
                "description": "Iteratively improve an existing skill by reviewing and applying changes.",
                "category": "general",
                "input_artifact": "user_message | improvement_session",
                "input_fields": ["target_skill"],
            },
            {
                "name": "direct_llm",
                "description": "Direct LLM call for single-shot tasks (summarize, classify, generate).",
                "category": "general",
                "input_artifact": "user_message",
                "input_fields": [],
            },
        ],
    )
    loop = _make_loop(host)

    # Exact user input from B9-S1 / B10-S1 dogfood sessions
    await loop.run(
        "skill_improver で direct_llm を 1 回 review して改善案を出して",
        [],
    )

    # The router MUST have called invoke_skill (not produced a text-only reply).
    # If the bug recurs, skill_calls will be empty and outbox will have a
    # clarification text — the assertion catches this.
    assert len(host.skill_calls) >= 1, (
        "B11-R3 regression: router produced text reply instead of invoke_skill. "
        f"Skill calls: {host.skill_calls}. Outbox: {host.outbox}"
    )
    # The invoked skill must be skill_improver (exact name from the user message)
    assert host.skill_calls[0]["skill"] == "skill_improver", (
        f"Expected skill_improver; got: {host.skill_calls[0]['skill']}"
    )


# ---------------------------------------------------------------------------
# ── Monkeypatch lifecycle invariant ─────────────────────────────────────────
# ---------------------------------------------------------------------------

def test_no_monkeypatch_leak():
    """LLMReplay monkeypatch is confined to @replay-marked tests.

    Protects the conftest install/restore contract. If LLMReplay leaks into
    non-replay tests, calls to litellm.acompletion would use the fake,
    masking real integration failures.
    """
    import litellm

    mod = getattr(litellm.acompletion, "__module__", "") or ""
    assert "reyn" not in mod, (
        f"litellm.acompletion appears to still be monkeypatched! module={mod!r}"
    )
