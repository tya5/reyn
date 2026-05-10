"""End-to-end tests: ChatSession + RouterLoop integration (PR35 wave F2).

These tests exercise the full ChatSession → RouterLoopHost → RouterLoop
path. call_llm_tools is patched to return scripted results without hitting
the network.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from reyn.chat.session import ChatSession, _PendingChain
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage

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


def _make_session(tmp_path: Path) -> ChatSession:
    return ChatSession(agent_name="test_agent")


def _drain_outbox(session: ChatSession) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test 1: chitchat e2e — LLM replies with text, outbox gets "agent" message
# ---------------------------------------------------------------------------

def test_user_message_chitchat_e2e(tmp_path, monkeypatch):
    """Tier 1 framework boundary: ChatSession→RouterLoop integration — user message produces kind=agent outbox entry. AsyncMock isolates from network for e2e path verification.

    Minimal session: mock call_llm_tools to return text 'hi'.
    User message → router → assert outbox has kind='agent', text='hi'.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True  # enable status messages

    async def run():
        with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = _text_result("hi")
            await session._handle_user_message("hello", chain_id="chain-001")

    _run(run())

    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    assert len(agent_msgs) == 1, f"Expected 1 agent outbox msg; got {[m.kind for m in msgs]}"
    assert agent_msgs[0].text == "hi"


def test_user_message_chitchat_appended_to_history(tmp_path, monkeypatch):
    """Tier 1 framework boundary: agent reply from RouterLoop is appended to session history with role=agent. AsyncMock isolates from network for e2e path verification."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    async def run():
        with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = _text_result("hello back")
            await session._handle_user_message("hello", chain_id="chain-002")

    _run(run())

    agent_turns = [m for m in session.history if m.role == "agent"]
    assert len(agent_turns) == 1
    assert agent_turns[0].text == "hello back"


# ---------------------------------------------------------------------------
# Test 2: invoke_skill e2e
# ---------------------------------------------------------------------------

def test_user_message_invoke_skill_e2e(tmp_path, monkeypatch):
    """Tier 1 framework boundary: ChatSession→RouterLoop skill invocation — round 1 invokes skill, round 2 produces text reply in outbox. AsyncMock required for multi-round e2e path.

    Script: round 1 invoke_skill, round 2 text reply.
    Mock skill execution via _run_skill_awaitable.
    Assert skill ran and outbox has final text reply.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    skill_ran = {"called": False}

    async def fake_run_skill_awaitable(spec, *, chain_id):
        skill_ran["called"] = True
        return {"status": "finished", "data": {"result": "done"}}

    rounds = [
        _tool_result([{"name": "invoke_skill", "args": {
            "name": "some_skill",
            "input": {"type": "test", "data": {}},
        }}]),
        _text_result("Skill complete."),
    ]

    async def run():
        # patch on the adapter — RouterLoop calls host.run_skill_awaitable directly.
        async def fake_adapter_run_skill(*, skill, input, chain_id):
            skill_ran["called"] = True
            return {"status": "finished", "data": {"result": "done"}}

        with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm, \
             patch.object(session._router_host, "run_skill_awaitable",
                          side_effect=fake_adapter_run_skill), \
             patch.object(session._router_host, "list_available_skills",
                          return_value=[{"name": "some_skill", "category": "general"}]):
            mock_llm.side_effect = rounds
            await session._handle_user_message("run skill", chain_id="chain-003")

    _run(run())

    assert skill_ran["called"], "Skill should have been invoked"
    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    assert len(agent_msgs) == 1
    assert agent_msgs[0].text == "Skill complete."


# ---------------------------------------------------------------------------
# Test 3: delegate_to_agent registers pending chain
# ---------------------------------------------------------------------------

def test_delegate_registers_pending_chain(tmp_path, monkeypatch):
    """Tier 2: OS invariant — delegate_to_agent in _handle_agent_request registers a PendingChain with correct origin_agent and waiting_on fields.

    Script: delegate_to_agent tool_call in _handle_agent_request context.
    Assert _PendingChain registered with correct chain_id.
    """
    monkeypatch.chdir(tmp_path)

    # We need a registry with a fake peer
    registry = MagicMock()
    registry.iter_reachable_agents.return_value = [
        {"name": "peer_agent", "role": "data analyst"},
    ]
    registry.exists.return_value = True
    registry.permit.return_value = True

    target_session = MagicMock()
    target_session.submit_agent_request = AsyncMock()
    registry.get_or_load.return_value = target_session
    registry.ensure_running = AsyncMock()

    session = ChatSession(agent_name="test_agent", registry=registry)
    session.is_attached = True

    rounds = [
        _tool_result([{"name": "delegate_to_agent", "args": {
            "to": "peer_agent",
            "request": "process the data please",
        }}]),
        _text_result("Delegated."),
    ]

    async def run():
        with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = rounds
            await session._handle_agent_request({
                "from_agent": "origin_agent",
                "request": "can you delegate this?",
                "depth": 1,
                "chain_id": "chain-del-001",
            })

    _run(run())

    # PR-refactor-session-1 wave 2: pending chains live in ChainManager.
    # Observe via public ChainManager.get() — returns None if not registered.
    pc = session._chains.get("chain-del-001")
    assert pc is not None, (
        f"_PendingChain not registered; registered chains: {session._chains.all_chain_ids()}"
    )
    assert isinstance(pc, _PendingChain)
    assert pc.origin_agent == "origin_agent"
    assert "peer_agent" in pc.waiting_on


# ---------------------------------------------------------------------------
# Test 4: RouterLoopHost protocol satisfied by ChatSession
# ---------------------------------------------------------------------------

def test_chatsession_satisfies_host_protocol(tmp_path, monkeypatch):
    """Tier 1: public contract — RouterHostAdapter (session._router_host) exposes all RouterLoopHost required methods and property types. Protocol compliance test; fails when required API is removed or renamed from the adapter."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    host = session._router_host

    required = [
        "chat_id", "agent_name", "agent_role",
        "list_available_skills", "list_available_agents",
        "get_memory_index", "get_file_permissions", "get_mcp_servers",
        "memory_path", "memory_dir",
        "run_skill_awaitable", "send_to_agent", "put_outbox",
        "file_read", "file_write", "file_delete", "file_list_directory",
        "file_regenerate_index",
        "mcp_list_servers", "mcp_list_tools", "mcp_call_tool",
        "resolve_model",
    ]
    missing = [m for m in required if not hasattr(host, m)]
    assert missing == [], f"Missing protocol members on RouterHostAdapter: {missing}"

    # Verify property types
    assert isinstance(host.chat_id, str)
    assert host.chat_id == "test_agent"
    assert isinstance(host.agent_name, str)
    assert isinstance(host.agent_role, str)


# ---------------------------------------------------------------------------
# Test 5: resolve_model delegates to _resolver
# ---------------------------------------------------------------------------

def test_resolve_model_uses_resolver(tmp_path, monkeypatch):
    """Tier 1 framework boundary: RouterHostAdapter.resolve_model delegates to ModelResolver; named models resolve to configured values and unknown names pass through unchanged."""
    monkeypatch.chdir(tmp_path)
    from reyn.llm.model_resolver import ModelResolver
    resolver = ModelResolver({"router": "openai/gpt-4o-mini"})
    session = ChatSession(agent_name="test_agent", resolver=resolver)

    assert session._router_host.resolve_model("router") == "openai/gpt-4o-mini"
    assert session._router_host.resolve_model("unknown") == "unknown"  # pass-through


# ---------------------------------------------------------------------------
# Test 6: list_available_skills excludes router/compactor
# ---------------------------------------------------------------------------

def test_list_available_skills_excludes_stdlib_router(tmp_path, monkeypatch):
    """Tier 2: OS invariant — RouterHostAdapter.list_available_skills() must never expose skill_router or chat_compactor to the LLM tool catalog. (FP-0011: skill_narrator was removed; the router LLM narrates inline.)"""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    skills = session._router_host.list_available_skills()
    names = {s.get("name") for s in skills}
    assert "skill_router" not in names
    assert "chat_compactor" not in names
    # skill_narrator no longer exists post-FP-0011; the assertion is now that
    # the name simply does not appear in any enumeration result.
    assert "skill_narrator" not in names


# ---------------------------------------------------------------------------
# Test 7: _build_history_for_router slices correctly
# ---------------------------------------------------------------------------

def test_build_history_for_router_shape(tmp_path, monkeypatch):
    """Tier 1 framework boundary: _build_history_for_router returns OpenAI-style dicts with correct role mapping and ordering from session history."""
    monkeypatch.chdir(tmp_path)
    from reyn.chat.session import ChatMessage
    session = _make_session(tmp_path)

    # Inject some history
    session.history = [
        ChatMessage(role="user", text="hello", ts="t1"),
        ChatMessage(role="agent", text="hi", ts="t2"),
        ChatMessage(role="user", text="tell me more", ts="t3"),
        ChatMessage(role="agent", text="sure!", ts="t4"),
    ]

    history = session._build_history_for_router()

    assert isinstance(history, list)
    for msg in history:
        assert "role" in msg and "content" in msg
        assert msg["role"] in ("user", "assistant")

    # Order must be preserved
    roles = [m["role"] for m in history]
    assert roles[0] == "user"
    assert roles[1] == "assistant"
