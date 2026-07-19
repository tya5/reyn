"""End-to-end tests: Session + RouterLoop integration (PR35 wave F2).

These tests exercise the full Session → RouterLoopHost → RouterLoop
path. call_llm_tools is patched to return scripted results without hitting
the network.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.session import Session, _PendingChain
from tests._support.agent_session import make_session

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


def _make_session(tmp_path: Path) -> Session:
    return make_session(
        agent_name="test_agent",
        chat_tool_use_scheme="universal-category",  # #1657: suite uses universal-category stub shape
    )


def _drain_outbox(session: Session) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


def _run(coro):
    return asyncio.run(coro)


class _StubSession:
    """Minimal peer Session stub for delegate_to_agent tests.

    Records submit_agent_request calls without invoking real session lifecycle.
    Real Session instantiation pulls in the full agent/event/loop stack;
    a stub is sufficient because the test only verifies the chain registration.
    """

    def __init__(self) -> None:
        self.submitted_requests: list[tuple] = []

    async def submit_agent_request(self, *args, **kwargs) -> None:
        self.submitted_requests.append((args, kwargs))


class _StubAgentRegistry:
    """Minimal AgentRegistry stub for delegate_to_agent tests.

    Pre-loads a single reachable peer agent and returns the supplied
    target session from get_or_load. Real AgentRegistry boots the on-disk
    registry catalog + permission graph; a stub is sufficient because the
    test only verifies the chain registration side-effect.
    """

    def __init__(self, target_session: _StubSession) -> None:
        self._target = target_session

    def iter_reachable_agents(self, self_name: str) -> list[dict]:
        return [{"name": "peer_agent", "role": "data analyst"}]

    def exists(self, name: str) -> bool:
        return True

    def permit(self, from_agent: str, to_agent: str) -> bool:
        return True

    def get_or_load(self, name: str, *, is_delegate: bool = False) -> _StubSession:
        return self._target

    async def ensure_running(self, name: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Test 1: chitchat e2e — LLM replies with text, outbox gets "agent" message
# ---------------------------------------------------------------------------

def test_user_message_chitchat_e2e(tmp_path, monkeypatch):
    """Tier 1: Session→RouterLoop integration — user message produces kind=agent outbox entry. AsyncMock isolates from network for e2e path verification.

    Minimal session: mock call_llm_tools to return text 'hi'.
    User message → router → assert outbox has kind='agent', text='hi'.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True  # enable status messages

    async def fake_llm(*args, **kwargs):
        return _text_result("hi")

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", fake_llm)

    async def run():
        await session._handle_user_message("hello", chain_id="chain-001")

    _run(run())

    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    (only,) = agent_msgs
    assert only.text == "hi"


def test_user_message_chitchat_appended_to_history(tmp_path, monkeypatch):
    """Tier 1: agent reply from RouterLoop is appended to session history with role=agent. AsyncMock isolates from network for e2e path verification."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    async def fake_llm(*args, **kwargs):
        return _text_result("hello back")

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", fake_llm)

    async def run():
        await session._handle_user_message("hello", chain_id="chain-002")

    _run(run())

    # Issue #383: role rename "agent" → "assistant" at construction time.
    agent_turns = [m for m in session.history if m.role == "assistant"]
    (only,) = agent_turns
    assert only.text == "hello back"


# ---------------------------------------------------------------------------
# Test 3: delegate_to_agent registers pending chain
# ---------------------------------------------------------------------------

def test_delegate_registers_pending_chain(tmp_path, monkeypatch):
    """Tier 2: OS invariant — delegate_to_agent in _handle_agent_request registers a PendingChain with correct origin_agent and waiting_on fields.

    Script: delegate_to_agent tool_call in _handle_agent_request context.
    Assert _PendingChain registered with correct chain_id.
    """
    monkeypatch.chdir(tmp_path)

    # Stub a registry with a single reachable peer.
    target_session = _StubSession()
    registry = _StubAgentRegistry(target_session)

    session = make_session(
        agent_name="test_agent",
        registry=registry,
        chat_tool_use_scheme="universal-category",  # #1657
    )
    session.is_attached = True

    rounds = [
        _tool_result([{"name": "invoke_action", "args": {
            "action_name": "multi_agent__delegate",
            "args": {"to": "peer_agent", "request": "process the data please"},
        }}]),
        _text_result("Delegated."),
    ]

    call_count = {"n": 0}

    async def fake_llm(*args, **kwargs):
        result = rounds[call_count["n"]]
        call_count["n"] += 1
        return result

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", fake_llm)

    async def run():
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
# Test 4: RouterLoopHost protocol satisfied by Session
# ---------------------------------------------------------------------------

def test_chatsession_satisfies_host_protocol(tmp_path, monkeypatch):
    """Tier 1: public contract — RouterHostAdapter (session.router_host) exposes all RouterLoopHost required methods and property types. Protocol compliance test; fails when required API is removed or renamed from the adapter."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    host = session.router_host

    required = [
        "chat_id", "agent_name", "agent_role",
        "list_available_agents",
        "get_memory_index", "get_file_permissions", "get_mcp_servers",
        "memory_path", "memory_dir",
        "send_to_agent", "put_outbox",
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
    """Tier 1: RouterHostAdapter.resolve_model delegates to ModelResolver; named models resolve to configured values and unknown names pass through unchanged."""
    monkeypatch.chdir(tmp_path)
    from reyn.llm.model_resolver import ModelResolver
    resolver = ModelResolver({"router": "openai/gpt-4o-mini"})
    session = make_session(agent_name="test_agent", resolver=resolver)

    assert session.router_host.resolve_model("router") == "openai/gpt-4o-mini"
    assert session.router_host.resolve_model("unknown") == "unknown"  # pass-through


# ---------------------------------------------------------------------------
# Test 7: _build_history_for_router slices correctly
# ---------------------------------------------------------------------------

def test_build_history_for_router_shape(tmp_path, monkeypatch):
    """Tier 1: _build_history_for_router returns OpenAI-style dicts with correct role mapping and ordering from session history."""
    monkeypatch.chdir(tmp_path)
    from reyn.runtime.chat_message import ChatMessage
    session = _make_session(tmp_path)

    # Inject some history (Issue #383: new content kwarg + assistant role)
    session.history = [
        ChatMessage(role="user", content="hello", ts="t1"),
        ChatMessage(role="assistant", content="hi", ts="t2"),
        ChatMessage(role="user", content="tell me more", ts="t3"),
        ChatMessage(role="assistant", content="sure!", ts="t4"),
    ]

    history = session._history_buffer.build_history()

    assert isinstance(history, list)
    for msg in history:
        assert "role" in msg and "content" in msg
        assert msg["role"] in ("user", "assistant")

    # Order must be preserved
    roles = [m["role"] for m in history]
    assert roles[0] == "user"
    assert roles[1] == "assistant"
