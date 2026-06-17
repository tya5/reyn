"""Tier 2: #1496 router_cap axis — limit-deny → force-close wrap-up (site C).

Site C fires in ``_emit_router_cap_exhausted_user`` BEFORE ``run_loop`` starts
(router cap is checked per-turn entry). History comes from the session's
``_history_buffer.build_history()``, not run_loop's local messages.

A temporary ``RouterLoop`` is constructed with the session's ``_router_host``
and ``chain_id``; ``_llm_caller`` is a Tier-2 test seam for injecting a
scripted fake without mocking.

Tests verify:
- ``limit_denied`` event emitted with ``kind="router_cap"``
- Force-close wrap-up text → OutboxMessage with ``limit_stopped=True``
  and ``limit_kind="router_cap"``
- No force-close text → fallback canned error + agent reply (no limit_stopped)

No mocks. _ScriptedLLM is a real fake (not AsyncMock/MagicMock).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.outbox import OutboxMessage
from reyn.chat.session import ChatSession, RouterCapExceeded
from reyn.config import LoopConfig, OnLimitConfig, SafetyConfig
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from tests.test_router_loop import _ScriptedLLM, text_result

_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _make_session(tmp_path: Path, cap: int = 3) -> ChatSession:
    safety = SafetyConfig(loop=LoopConfig(max_router_calls_per_turn=cap))
    return ChatSession(
        agent_name="test_cap_agent",
        budget_tracker=BudgetTracker(CostConfig()),
        safety=safety,
    )


def _exc(count: int = 3, cap: int = 3) -> RouterCapExceeded:
    return RouterCapExceeded(count=count, cap=cap, last_reason="loop_reason")


def _run(coro):
    return asyncio.run(coro)


def _drain(session: ChatSession) -> list[OutboxMessage]:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


# ── 1. limit_denied event emitted (with wrap-up text) ─────────────────────────


@pytest.mark.asyncio
async def test_router_cap_limit_denied_event_emitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: #1496 site C — limit_denied event with kind=router_cap is
    emitted when the per-turn router cap is exhausted."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    emitted: list[dict] = []
    orig_emit = session._chat_events.emit

    def capture(name, **kw):
        emitted.append({"type": name, **kw})
        return orig_emit(name, **kw)

    session._chat_events.emit = capture  # type: ignore[assignment]

    llm = _ScriptedLLM([text_result("wrap-up summary here")])
    await session._emit_router_cap_exhausted_user(
        _exc(3, 3), chain_id="chain-cap", _llm_caller=llm,
    )

    limit_evs = [e for e in emitted if e["type"] == "limit_denied"]
    (ev,) = limit_evs
    assert ev["kind"] == "router_cap"
    assert ev["count"] == 3
    assert ev["cap"] == 3


# ── 2. wrap-up text → OutboxMessage with limit_stopped=True ───────────────────


@pytest.mark.asyncio
async def test_router_cap_force_close_wrap_up_emits_limit_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: #1496 site C — when force-close wrap-up produces text, an
    agent OutboxMessage with limit_stopped=True and limit_kind=router_cap
    is emitted (no fallback canned error)."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    llm = _ScriptedLLM([text_result("cap reached; completed steps 1-3")])
    await session._emit_router_cap_exhausted_user(
        _exc(3, 3), chain_id="chain-cap-wrapup", _llm_caller=llm,
    )

    msgs = _drain(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    (agent_msg,) = agent_msgs
    assert agent_msg.text == "cap reached; completed steps 1-3"
    meta = agent_msg.meta or {}
    assert meta.get("limit_stopped") is True
    assert meta.get("limit_kind") == "router_cap"

    # No error message — canned fallback must NOT fire
    assert not any(m.kind == "error" for m in msgs)


# ── 3. no wrap-up text → fallback canned reply ────────────────────────────────


@pytest.mark.asyncio
async def test_router_cap_fallback_when_wrap_up_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: #1496 site C — when wrap-up produces no text (empty content),
    the original canned error + agent fallback reply is emitted instead."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    llm = _ScriptedLLM([LLMToolCallResult(
        content=None, tool_calls=[], finish_reason="stop", usage=_USAGE,
    )])
    await session._emit_router_cap_exhausted_user(
        _exc(3, 3), chain_id="chain-cap-empty", _llm_caller=llm,
    )

    msgs = _drain(session)
    kinds = [m.kind for m in msgs]
    # canned fallback fires → error + agent
    assert "error" in kinds, f"expected error msg; got {kinds}"
    assert "agent" in kinds, f"expected agent fallback; got {kinds}"

    # none of the agent messages should carry limit_stopped
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    for m in agent_msgs:
        assert not (m.meta or {}).get("limit_stopped"), (
            f"limit_stopped must not be set on canned fallback; got {m.meta}"
        )
