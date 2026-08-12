"""End-to-end tests: Session + RouterLoop integration (PR35 wave F2).

These tests exercise the full Session → RouterLoopHost → RouterLoop
path. call_llm_tools is patched to return scripted results without hitting
the network.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.router_loop import RouterLoopHost
from reyn.runtime.session import Session
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

    async def fake_llm(*args, **kwargs):
        return _text_result("hi")

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", fake_llm)

    async def run():
        await session._handle_inbox_text("hello", chain_id="chain-001")

    _run(run())

    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    (only,) = agent_msgs
    assert only.text == "hi"


def test_user_message_chitchat_appended_to_history(tmp_path, monkeypatch):
    """Tier 1: agent reply from RouterLoop is appended to session history with role=agent. AsyncMock isolates from network for e2e path verification."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    async def fake_llm(*args, **kwargs):
        return _text_result("hello back")

    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", fake_llm)

    async def run():
        await session._handle_inbox_text("hello", chain_id="chain-002")

    _run(run())

    # Issue #383: role rename "agent" → "assistant" at construction time.
    agent_turns = [m for m in session.history if m.role == "assistant"]
    (only,) = agent_turns
    assert only.text == "hello back"


# ---------------------------------------------------------------------------
# Test 3: (formerly) delegate_to_agent registers pending chain — deleted in
# proposal 0067 P6 (#3978). This test dispatched `invoke_action(action_name=
# "delegate_to_agent", ...)` inside `_handle_agent_request` and asserted a
# nested PendingChain got registered via `inter_agent_messaging.py`'s
# `dispatched = self._get_router_loop_delegations()` branch — a list
# populated ONLY by RouterLoop's `send_to_agent` dispatch, which was
# delegate_to_agent's own handler. With delegate_to_agent retired and no
# replacement producer for this exact nested-delegation path (architect
# ruling, #3978 P6), `_get_router_loop_delegations()` is now permanently
# empty — the code this test exercised has no live producer. Six-question
# test review: "who would miss this test" → nobody; the mechanism it probed
# is dead, not merely relocated. run_pipeline(collect="async")'s own
# ChainManager.register() call site (session_api.py) is covered separately
# by tests/runtime/test_session_invariants.py, which drives it directly.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 4: RouterLoopHost protocol satisfied by Session
# ---------------------------------------------------------------------------

def _router_loop_host_member_names() -> list[str]:
    """Walk RouterLoopHost's MRO and collect every declared public member
    name (methods + property/annotation-only attributes), across it and its
    RouterLoopCore base — the same set `hasattr` needs to probe for a real
    Protocol-conformance check.

    #4153 post-mortem: a hand-written mirror of this list (a hardcoded
    ``required = [...]``) went stale the moment ``send_to_agent`` was
    removed from the Protocol — the mirror kept the retired name and the
    test asserted for it forever, independent of what the Protocol actually
    declares. Deriving the set here means a future member addition/removal
    on the Protocol is reflected automatically, with nothing to keep in
    sync by hand."""
    names: set[str] = set()
    for klass in RouterLoopHost.__mro__:
        if klass is object or klass is Protocol or klass.__module__ == "typing":
            continue
        names.update(k for k in vars(klass) if not k.startswith("_"))
        names.update(
            k for k in getattr(klass, "__annotations__", {}) if not k.startswith("_")
        )
    return sorted(names)


def test_chatsession_satisfies_host_protocol(tmp_path, monkeypatch):
    """Tier 1: public contract — RouterHostAdapter (session.router_host) exposes every RouterLoopHost Protocol member. Protocol compliance test; fails when required API is removed or renamed from the adapter."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    host = session.router_host

    required = _router_loop_host_member_names()
    # lead-coder review: a derivation that silently returns [] (e.g. the
    # vars()/__annotations__ walk stops finding anything after an unrelated
    # typing-internals change) makes `missing` vacuously [] too — green
    # without checking anything. Guard the derivation itself, not just its
    # result. Falsify-verified: with the derivation forced to return [],
    # this assert fires (not the missing== one).
    assert required, "Protocol member derivation returned nothing — the gate would pass vacuously"
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
    """Tier 1: RouterHostAdapter.resolve_model delegates to ModelResolver; a
    configured class resolves to its target, and a raw '/'-containing
    string still passes through unchanged (name position). #4349: an
    unresolved BARE (no '/') name is a class position that failed to
    resolve and now raises, rather than passing through unchanged."""
    monkeypatch.chdir(tmp_path)
    from reyn.llm.model_resolver import ModelResolver
    resolver = ModelResolver({"router": "openai/gpt-4o-mini"})
    session = make_session(agent_name="test_agent", resolver=resolver)

    assert session.router_host.resolve_model("router") == "openai/gpt-4o-mini"
    assert session.router_host.resolve_model("openai/literal-model") == "openai/literal-model"
    import pytest
    with pytest.raises(ValueError, match="unknown"):
        session.router_host.resolve_model("unknown")


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
