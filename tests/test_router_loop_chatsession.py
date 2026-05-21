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
    return ChatSession(
        agent_name="test_agent",
    )


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

    # Issue #383: role rename "agent" → "assistant" at construction time.
    agent_turns = [m for m in session.history if m.role == "assistant"]
    assert len(agent_turns) == 1
    assert agent_turns[0].text == "hello back"


# ---------------------------------------------------------------------------
# Test 2: invoke_skill e2e
# ---------------------------------------------------------------------------

def test_user_message_invoke_skill_e2e(tmp_path, monkeypatch):
    """Tier 1 framework boundary: ChatSession→RouterLoop invoke_skill — round 1 spawns skill (FP-0012 non-blocking) and the router exits on spawn-ack. AsyncMock required for the e2e path.

    Post-H3-ablation contract (= dogfood B32 §4.2 + H3 ablation diagnosis):
    invoke_skill / invoke_action returning the spawn-ack
    ``{status: "spawned", run_id, chain_id, note}`` causes the router
    loop to exit immediately rather than continuing for a second LLM
    round. The previous behaviour — accumulating the spawn-ack into
    messages and asking the LLM to compose an acknowledgment — was the
    race condition observed in B32 W3 S1 (= `(answered)` workaround in
    llm.py:821 caused the LLM to hallucinate a generic reply before the
    skill output was available).

    The skill_completed inbox path (= ``_handle_skill_completed``
    re-engaging the router with the real skill output) is the
    sanctioned reply path post-H3. That path is exercised by
    ``test_skill_completed_inbox_enqueued_on_finish`` in
    test_session_invariants.py.

    2026-05-17 N3 update: prior to this revision the spawn-ack turn
    produced NO agent message at all — the user saw silence between
    request and the eventual ``[task_completed]``. The OS now emits a
    deterministic synthetic acknowledgment via ``put_outbox(kind="agent")``
    before the early-exit. This restores the user-visible feedback
    without re-introducing the B32 race condition: the message is
    OS-composed (= not LLM-composed), so it cannot fabricate skill
    output that hasn't happened yet. See ``_SPAWN_ACK_MSG`` in
    ``router_loop.py`` for the i18n template.

    Script: round 1 invoke_action returning a spawn-ack; no round 2
    needed (= router exits on spawn-ack via the H3 check). Mock
    chat-mode dispatch via ``host.spawn_skill``.

    Assertions: spawn fired; ``invoke_skill_spawn_ack_exit`` event
    emitted; outbox carries exactly one deterministic OS-composed
    agent message hinting at ``/tasks``.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    spawn_called = {"called": False}

    rounds = [
        _tool_result([{"name": "invoke_action", "args": {
            "action_name": "skill__some_skill",
            "args": {"input": {"type": "test", "data": {}}},
        }}]),
        # No round 2: H3 patch exits the loop on spawn-ack.
    ]

    async def run():
        async def fake_adapter_spawn_skill(*, skill, input, chain_id):
            spawn_called["called"] = True
            return {
                "status": "spawned",
                "run_id": "20260510T000000Z_some_skill_aaaa",
                "chain_id": chain_id,
                "skill": skill,
                "note": "Running in the background. I will notify you when it completes. Use /tasks to check progress.",
            }

        with patch("reyn.chat.router_loop.call_llm_tools", new_callable=AsyncMock) as mock_llm, \
             patch.object(session._router_host, "spawn_skill",
                          side_effect=fake_adapter_spawn_skill), \
             patch.object(session._router_host, "list_available_skills",
                          return_value=[{"name": "some_skill", "category": "general"}]):
            mock_llm.side_effect = rounds
            await session._handle_user_message("run skill", chain_id="chain-003")

    _run(run())

    assert spawn_called["called"], "Skill should have been spawned"
    # Post-N3 contract: spawn-ack turn emits exactly ONE deterministic
    # OS-composed agent message hinting at /tasks. The full
    # narration of the skill output still comes later via the
    # skill_completed inbox → _handle_skill_completed path; this
    # message is just the user-visible "your request started" signal.
    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    assert len(agent_msgs) == 1, (
        f"Post-N3 contract: spawn-ack turn must emit exactly one "
        f"OS-composed agent message. Got: {[m.text[:80] for m in agent_msgs]}"
    )
    ack = agent_msgs[0]
    assert "/tasks" in ack.text, (
        f"Spawn-ack message must mention /tasks (the user's only "
        f"in-flight tracking surface). Got: {ack.text!r}"
    )
    assert ack.meta.get("source") == "spawn_ack", (
        f"Spawn-ack message must carry meta.source='spawn_ack' so "
        f"downstream consumers can distinguish it from LLM-composed "
        f"replies. Got meta={ack.meta!r}"
    )
    # H3 audit event: the spawn-ack exit must have fired exactly once.
    emitted = [
        e for e in session._chat_events.all()
        if e.type == "invoke_skill_spawn_ack_exit"
    ]
    assert len(emitted) == 1, (
        f"Expected exactly one invoke_skill_spawn_ack_exit event; "
        f"got {len(emitted)}: {emitted!r}"
    )


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

    session = ChatSession(
        agent_name="test_agent",
        registry=registry,
    )
    session.is_attached = True

    rounds = [
        _tool_result([{"name": "invoke_action", "args": {
            "action_name": "agent.peer__peer_agent",
            "args": {"request": "process the data please"},
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
