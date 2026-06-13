"""Tier 2: #1538 — A2A router_cap-exhausted path delivers LLM wrap-up (not canned).

Before #1538, A2AHandler._emit_router_cap_exhausted_user was a standalone
canned-only implementation: it emitted a static _ROUTER_RETRY_EXHAUSTED_MSG
without attempting the LLM force-close wrap-up that ChatSession site C (#1496)
produces. The tui-coder trace confirmed that router_cap fires exclusively on
the a2a path (no-reset accumulation) — site C was dead code.

After #1538, A2AHandler receives `emit_router_cap_exhausted_fn` injected from
ChatSession at construction. Both SkillPlanGlue and A2AHandler paths call the
single ChatSession._emit_router_cap_exhausted_user — zero drift by construction.

Invariants pinned:

1. (wiring gate) A2AHandler._emit_router_cap_exhausted_user delegates to the
   injected callback with (exc, chain_id=...). The old canned path is removed.
2. (LLM wrap-up reachable) When a scripted LLM returns wrap-up content,
   ChatSession._emit_router_cap_exhausted_user emits an outbox message with
   meta["limit_stopped"] is True — proving the dead code is now reachable and
   the user receives a contextual summary instead of a canned string.

No mocks — real A2AHandler + real ChatSession + real scripted LLM callable.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.chat.services.a2a_handler import A2AHandler
from reyn.chat.services.chain_manager import ChainManager
from reyn.chat.session import ChatSession, RouterCapExceeded
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from tests.test_router_loop import FakeEventLog

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


# ── Fake helpers ──────────────────────────────────────────────────────────────


class _FakeJournal:
    """Minimal _JournalLike for ChainManager construction."""

    @property
    def snapshot(self) -> Any:
        from reyn.events.agent_snapshot import AgentSnapshot
        return AgentSnapshot(agent_name="test-agent")

    async def record_chain_register(self, *, chain_id: str, fields: dict) -> None:
        pass

    async def record_chain_update(self, *, chain_id: str, fields: dict) -> None:
        pass

    async def record_chain_resolve(self, *, chain_id: str) -> None:
        pass

    async def record_chain_timeout_fired(self, *, chain_id: str) -> None:
        pass


class _RecordingEmit:
    """Real async callable that records (exc, chain_id) pairs passed to it."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, str]] = []

    async def __call__(self, exc: Any, *, chain_id: str, **_kw: Any) -> None:
        self.calls.append((exc, chain_id))


class _ScriptedWrapupLLM:
    """Real scripted LLM callable that returns a fixed wrap-up text on call.

    Used to inject a successful LLM wrap-up response via the _llm_caller
    test seam on ChatSession._emit_router_cap_exhausted_user.
    """

    def __init__(self, wrapup_text: str) -> None:
        self._text = wrapup_text
        self.call_count: int = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.call_count += 1
        return LLMToolCallResult(
            content=self._text,
            tool_calls=[],
            finish_reason="stop",
            usage=_EMPTY_USAGE,
        )


def _make_a2a_handler(emit_fn: _RecordingEmit) -> A2AHandler:
    """Construct a minimal A2AHandler wired with the recording emit callback."""
    events = FakeEventLog()
    chain_mgr = ChainManager(
        journal=_FakeJournal(),
        events=events,
        chain_timeout_seconds=30.0,
        max_hop_depth=3,
    )

    async def _noop_outbox(msg: Any) -> None:
        pass

    async def _noop_router_loop(text: str, cid: str) -> None:
        pass

    async def _noop_limit_checkpoint(**kw: Any) -> Any:
        return None

    async def _noop_callback(*_a: Any, **_kw: Any) -> None:
        pass

    return A2AHandler(
        event_log=events,
        chain_manager=chain_mgr,
        agent_name="test-agent",
        max_hop_depth=3,
        safety_extensions={},
        output_language="en",
        append_history=lambda *_a, **_kw: None,
        put_outbox=_noop_outbox,
        handle_chat_limit_checkpoint=_noop_limit_checkpoint,
        run_router_loop=_noop_router_loop,
        reset_router_turn_counter=lambda: None,
        send_request_callback=_noop_callback,
        send_response_callback=_noop_callback,
        on_chain_timeout_fire=_noop_callback,
        emit_router_cap_exhausted_fn=emit_fn,
        get_router_loop_delegations=lambda: None,
        set_router_loop_delegations=lambda _v: None,
        get_router_loop_agent_replies=lambda: None,
        set_router_loop_agent_replies=lambda _v: None,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a2a_emit_router_cap_delegates_to_injected_fn() -> None:
    """Tier 2: A2AHandler._emit_router_cap_exhausted_user delegates to the
    injected emit_router_cap_exhausted_fn callback — the old standalone canned
    implementation is replaced by a thin forwarder (#1538 wiring gate).

    Pins: callback fires with the correct (exc, chain_id) when
    _emit_router_cap_exhausted_user is called on the a2a handler.
    """
    recording = _RecordingEmit()
    handler = _make_a2a_handler(recording)
    exc = RouterCapExceeded(count=3, cap=3, last_reason="test turn")

    await handler._emit_router_cap_exhausted_user(exc, chain_id="chain-a2a")

    assert recording.calls, "emit_router_cap_exhausted_fn was never called"
    called_exc, called_chain_id = recording.calls[0]
    assert called_exc is exc
    assert called_chain_id == "chain-a2a"


@pytest.mark.asyncio
async def test_a2a_router_cap_wrapup_produces_limit_stopped_meta() -> None:
    """Tier 2: ChatSession._emit_router_cap_exhausted_user (the fn A2AHandler
    delegates to) emits an outbox message with meta["limit_stopped"] is True
    when the scripted LLM returns wrap-up content.

    This is the positive dead-code-resolution proof: before #1538 the a2a path
    only emitted canned messages; after #1538 the same LLM force-close wrap-up
    that site C produces is reachable on the a2a path.

    Uses the _llm_caller test seam to inject a scripted LLM without touching
    the real LiteLLM stack.
    """
    session = ChatSession(agent_name="a2a-wrapup-test")
    scripted = _ScriptedWrapupLLM("Turn ended at limit — here is a summary.")
    exc = RouterCapExceeded(count=3, cap=3, last_reason="a2a chain")

    await session._emit_router_cap_exhausted_user(
        exc,
        chain_id="chain-a2a-t2",
        _llm_caller=scripted,
    )

    # Drain outbox and find the agent message produced by the LLM wrap-up.
    messages = []
    while not session.outbox.empty():
        messages.append(session.outbox.get_nowait())

    limit_stopped_msgs = [
        m for m in messages
        if hasattr(m, "meta") and m.meta.get("limit_stopped") is True
    ]
    assert limit_stopped_msgs, (
        f"No outbox message with meta.limit_stopped=True found; "
        f"got {[vars(m) for m in messages]}"
    )
    assert limit_stopped_msgs[0].kind == "agent"
    assert scripted.call_count >= 1
