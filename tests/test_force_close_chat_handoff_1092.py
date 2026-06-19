"""Tier 2/3a: chat-axis force-close handoff (#1092 PR-F2b — the load-bearing piece).

When the chat overflow recovery (retry_loop) exhausts even at its floor, the
session FORCE-CLOSES instead of dead-ending: it consolidates the working context
into a capped summary (≤ output_reserve, F1) and installs it covers-all — firing
F2a's durable covers-respecting reset so the re-entry slices [consolidation] +
the new turn (not the raw head/tail that overflowed). By construction it
converges in ONE handoff; a cap=1 backstops the irreducible single-oversized-
message dead-end (no infinite loop).

Driven through the REAL retry_loop + handoff with a hand-written fake RouterLoop
(a collaborator double, NOT a MagicMock) that raises a context-overflow until the
consolidation appears in the sliced history — exercising the real terminal →
force-close → re-enter path. The chat analogue of D2's phase firing infra.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import _MAX_FORCE_CLOSE_HANDOFFS
from reyn.services.compaction.engine import ContextOverflowError
from tests.test_session_router_history_slicing import _make_session, _push

_CONSOL = "CONSOLIDATION-MARK-XYZ"


def _has_consolidation(history: list[dict]) -> bool:
    return any(_CONSOL in str(m.get("content", "")) for m in history)


def _capture_events(session) -> list[str]:
    seen: list[str] = []
    session._chat_events.add_subscriber(lambda e: seen.append(e.type))
    return seen


def _msg(role: str, text: str) -> ChatMessage:
    return ChatMessage(role=role, content=text, ts=datetime.now(timezone.utc).isoformat())


class _FakeRouterLoop:
    """Collaborator double for RouterLoop. ``run`` raises a context-overflow
    until the force-close consolidation appears in the sliced history (then
    converges); ``_force_close_call`` returns the capped consolidation. With
    ``always_overflow`` it never converges (the irreducible-message case)."""

    def __init__(self, *args: Any, always_overflow: bool = False, **kwargs: Any) -> None:
        self.router_model = "fake-model"
        self.force_close_calls = 0
        self.always_overflow = always_overflow

    async def run(self, *, user_text: str, history: list[dict]) -> TokenUsage:
        if self.always_overflow or not _has_consolidation(history):
            raise ContextOverflowError("simulated context_length too large")
        return TokenUsage(prompt_tokens=10, completion_tokens=5)

    async def _force_close_call(
        self, messages: list[dict], *, resolved_model: str
    ) -> LLMToolCallResult:
        self.force_close_calls += 1
        return LLMToolCallResult(
            content=_CONSOL, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
        )


def _install_fake_loop(monkeypatch, **kw) -> _FakeRouterLoop:
    fake = _FakeRouterLoop(**kw)
    monkeypatch.setattr("reyn.runtime.router_loop.RouterLoop", lambda *a, **k: fake)
    return fake


# ── firing: handoff fires, installs the consolidation, converges in one ───────


@pytest.mark.asyncio
async def test_handoff_fires_installs_consolidation_and_converges(
    tmp_path, monkeypatch
) -> None:
    """Tier 3a: a forced overflow terminal drives the force-close handoff — it
    fires the `router_force_close_handoff` event, installs the consolidation, and
    the re-entry converges in ONE handoff (the by-construction backstop (a))."""
    session = _make_session(tmp_path)  # viable T_max → force-close engine wired
    for t in ("U1-old", "A1-old", "U2-old"):
        _push(session, "user" if t.startswith("U") else "assistant", t)
    _install_fake_loop(monkeypatch)
    events = _capture_events(session)

    await session._run_router_loop("new question", "chain-1")

    # The P6 event log (audit truth) shows EXACTLY ONE handoff → converged
    # in one (the by-construction backstop (a)); the run returned (no raise).
    assert events.count("router_force_close_handoff") == 1
    slice_msgs = session._history_buffer.build_history()
    assert _has_consolidation(slice_msgs)              # reaches-LLM


@pytest.mark.asyncio
async def test_handoff_cap_raises_no_infinite_loop(tmp_path, monkeypatch) -> None:
    """Tier 3a: (cap=1 backstop) an irreducible turn (overflows even after
    consolidation) does NOT infinite-loop — after one handoff the cap raises the
    genuine dead-end. force_close fired exactly once."""
    session = _make_session(tmp_path)  # viable T_max → force-close engine wired
    _push(session, "user", "U1-old")
    _install_fake_loop(monkeypatch, always_overflow=True)
    events = _capture_events(session)

    with pytest.raises(ContextOverflowError):
        await session._run_router_loop("irreducibly huge", "chain-1")

    # bounded: EXACTLY one handoff fired (== _MAX_FORCE_CLOSE_HANDOFFS), then the
    # cap raised the genuine dead-end — no infinite loop.
    assert events.count("router_force_close_handoff") == _MAX_FORCE_CLOSE_HANDOFFS
    assert "router_context_overflow_unrecovered" in events        # genuine dead-end


# ── wrap-up-fits: bounded fallback (Fork 1) ──────────────────────────────────


class _FailFirstLoop:
    """_force_close_call overflows on the first ``fail_first`` attempts, then
    succeeds — exercises the bounded wrap-up fallback shrinking through its
    decreasing input candidates until one fits."""

    def __init__(self, fail_first: int = 2) -> None:
        self.attempts = 0
        self._fail_first = fail_first

    async def _force_close_call(
        self, messages: list[dict], *, resolved_model: str
    ) -> LLMToolCallResult:
        self.attempts += 1
        if self.attempts <= self._fail_first:
            raise ContextOverflowError("wrap-up input too large")
        return LLMToolCallResult(
            content=_CONSOL, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
        )


@pytest.mark.asyncio
async def test_wrap_up_bounded_fallback_fits(tmp_path) -> None:
    """Tier 2: when the richer wrap-up inputs overflow, the bounded fallback
    shrinks (through its decreasing candidates) until one fits and the
    consolidation is produced (wrap-up-fits — Fork 1's chat _force_close_call_
    with_retry analogue)."""
    session = _make_session(tmp_path, t_max=2000)
    for t in ("U1", "A1", "U2", "A2"):
        _push(session, "user" if t.startswith("U") else "assistant", t)
    loop = _FailFirstLoop(fail_first=2)
    out = await session._force_close_wrap_up(loop, resolved_model="m")
    assert out == _CONSOL
    assert loop.attempts == 3  # 2 over-large candidates fell back, 3rd fit


class _AlwaysOverflowWrapUp:
    async def _force_close_call(self, messages, *, resolved_model):
        raise ContextOverflowError("overflow even at summary-only")


@pytest.mark.asyncio
async def test_wrap_up_sub_viable_raises(tmp_path) -> None:
    """Tier 2: if even summary-only overflows, the model is RUNTIME sub-viable →
    _force_close_wrap_up raises (the handoff loop surfaces it as a dead-end)."""
    session = _make_session(tmp_path, t_max=2000)
    _push(session, "user", "U1")
    with pytest.raises(ContextOverflowError):
        await session._force_close_wrap_up(_AlwaysOverflowWrapUp(), resolved_model="m")


# ── install: covering consolidation + covered turns dropped (reaches-LLM) ─────


@pytest.mark.asyncio
async def test_force_close_handoff_installs_covering_consolidation(tmp_path) -> None:
    """Tier 2: _force_close_handoff (running the REAL _force_close_wrap_up against
    a fake loop) installs a covers-all force-close summary (firing F2a's reset) —
    the consolidation reaches the slice and the covered raw turns are dropped; the
    P6 event fires."""
    session = _make_session(tmp_path, t_max=2000)
    session._append_history(_msg("user", "U1-covered"))
    session._append_history(_msg("assistant", "A1-covered"))
    events = _capture_events(session)

    await session._force_close_handoff(loop=_FakeRouterLoop(), user_text="x")

    slice_blob = "\n".join(
        str(m.get("content", "")) for m in session._history_buffer.build_history()
    )
    assert _CONSOL in slice_blob                # consolidation reaches the slice
    assert "U1-covered" not in slice_blob        # covered raw turns dropped
    assert "router_force_close_handoff" in events
