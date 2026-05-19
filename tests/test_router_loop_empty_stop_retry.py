"""Tier 2: RouterLoop empty-stop retry with continuation directive (B42-NF-W6-1).

Pinned invariants:

- When ``empty_stop_retry_directive`` is set AND ``REYN_EMPTY_STOP_RETRY=1``,
  an empty-stop response (= finish_reason=stop, no content, no tool_calls)
  triggers ONE retry. Before the retry, a synthetic ``role="user"`` message
  carrying the directive is appended to the messages list.
- When the env var is unset, the directive is plumbed in but ignored — the
  loop falls through to the existing "observe + surface" path (= no behaviour
  change for the default chat-router policy).
- When the directive is None, the env var is also ignored — no retry attempt
  even with the env var set.
- Retries are bounded at 1 per turn: a second empty-stop in the same turn
  falls through to the surface path (= no infinite retries when the
  attractor is unbreakable).
- A ``router_empty_response_retry_injected`` audit event is emitted on the
  retry path so the P6 audit trail records the intervention.

References:
- Anthropic ``handling-stop-reasons`` docs (= "continuation prompts as a
  last resort" — official recommendation).
- Hermes-agent #9400 — community implementation of the same pattern.
- B42-NF-W6-1 trace-patch-replay evidence (0/10 baseline → 10/10 patched
  on the W6-S1 plan-step empty-stop attractor).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage

# Reuse FakeRouterHost / _ScriptedLLM / text_result / tool_result from the
# canonical RouterLoop test module — real classes (= no Mock / AsyncMock).
from tests.test_router_loop import (  # noqa: E402
    FakeRouterHost,
    _ScriptedLLM,
    text_result,
    tool_result,
)


_DIRECTIVE = (
    "Now write your step report. Summarise the relevant content from "
    "the tool result above. Do not call another tool. Write the report now."
)


def _empty_stop_result() -> LLMToolCallResult:
    """An empty-stop result — what the attractor produces."""
    return LLMToolCallResult(
        content=None,
        tool_calls=[],
        finish_reason="stop",
        usage=TokenUsage(prompt_tokens=100, completion_tokens=0),
    )


def _make_loop(host: FakeRouterHost, directive: str | None) -> RouterLoop:
    return RouterLoop(
        host=host,
        chain_id="chain-empty-stop-test",
        max_iterations=5,
        empty_stop_retry_directive=directive,
    )


# ---------------------------------------------------------------------------
# Env var ON + directive set → retry with injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_path_injects_user_msg_when_env_var_set(monkeypatch):
    """Tier 2 (B42-NF-W6-1): when env var set AND directive set AND empty
    stop occurs, the loop appends a synthetic user message and retries.
    """
    monkeypatch.setenv("REYN_EMPTY_STOP_RETRY", "1")
    host = FakeRouterHost()
    loop = _make_loop(host, _DIRECTIVE)

    # Script: empty stop → (retry) → text reply
    scripted = _ScriptedLLM([_empty_stop_result(), text_result("recovered")])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("test", [])

    # Two LLM calls (= 1 initial + 1 retry)
    assert scripted.call_count == 2
    # Outbox carries the recovered text — NOT the empty-response fallback.
    assert len(host.outbox) == 1
    assert host.outbox[0]["text"] == "recovered"
    # Audit event was emitted for the retry.
    emitted = [e["type"] for e in host._events.emitted]
    assert "router_empty_response_detected" in emitted
    assert "router_empty_response_retry_injected" in emitted


# ---------------------------------------------------------------------------
# Env var unset → no retry, existing behaviour preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retry_when_env_var_unset(monkeypatch):
    """Tier 2 (regression guard): without ``REYN_EMPTY_STOP_RETRY=1``, the
    directive is ignored and the loop falls through to the existing
    "observe + surface" path (= empty-response fallback message in
    outbox, no second LLM call).
    """
    monkeypatch.delenv("REYN_EMPTY_STOP_RETRY", raising=False)
    host = FakeRouterHost()
    loop = _make_loop(host, _DIRECTIVE)

    scripted = _ScriptedLLM([_empty_stop_result()])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("test", [])

    # Only 1 LLM call (= no retry).
    assert scripted.call_count == 1
    # Outbox has the empty-response fallback message.
    assert len(host.outbox) == 1
    assert host.outbox[0]["meta"]["source"] == "router_empty_response"
    # No retry injection event.
    emitted = [e["type"] for e in host._events.emitted]
    assert "router_empty_response_retry_injected" not in emitted


# ---------------------------------------------------------------------------
# Env var set but directive None → no retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_retry_when_directive_is_none(monkeypatch):
    """Tier 2: env var set but directive None (= chat router default
    construction) → no retry, existing observe+surface path runs.
    """
    monkeypatch.setenv("REYN_EMPTY_STOP_RETRY", "1")
    host = FakeRouterHost()
    loop = _make_loop(host, None)  # no directive

    scripted = _ScriptedLLM([_empty_stop_result()])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("test", [])

    assert scripted.call_count == 1
    assert host.outbox[0]["meta"]["source"] == "router_empty_response"


# ---------------------------------------------------------------------------
# Bounded retries — second empty stop falls through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_bounded_to_one_per_turn(monkeypatch):
    """Tier 2: when the retry ALSO produces an empty stop, the second
    occurrence falls through to the surface path. Prevents infinite
    retries when the attractor is unbreakable.
    """
    monkeypatch.setenv("REYN_EMPTY_STOP_RETRY", "1")
    host = FakeRouterHost()
    loop = _make_loop(host, _DIRECTIVE)

    # Script: empty stop → empty stop on retry → loop must surface failure.
    scripted = _ScriptedLLM([_empty_stop_result(), _empty_stop_result()])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("test", [])

    # Exactly 2 LLM calls (= 1 initial + 1 retry, then surface).
    assert scripted.call_count == 2
    # Outbox has the empty-response fallback (= surfaced after retry failed).
    assert len(host.outbox) == 1
    assert host.outbox[0]["meta"]["source"] == "router_empty_response"
    # Exactly ONE retry injection event recorded (= bound enforced).
    emitted = [e["type"] for e in host._events.emitted]
    retry_count = emitted.count("router_empty_response_retry_injected")
    assert retry_count == 1


# ---------------------------------------------------------------------------
# Retry path injects the directive content verbatim
# ---------------------------------------------------------------------------


class _MessageCapturingScripted:
    """Real callable that records each call's messages snapshot."""

    def __init__(self, script):
        self._script = list(script)
        self.call_count = 0
        self.messages_per_call: list[list[dict]] = []

    async def __call__(self, **kwargs):
        msgs = kwargs.get("messages") or []
        self.messages_per_call.append([dict(m) for m in msgs])
        result = self._script[self.call_count]
        self.call_count += 1
        return result


@pytest.mark.asyncio
async def test_retry_injects_directive_verbatim(monkeypatch):
    """Tier 2: the directive string is appended verbatim as a
    ``role="user"`` message in the retry call's messages — no rewriting
    or wrapping by RouterLoop.
    """
    monkeypatch.setenv("REYN_EMPTY_STOP_RETRY", "1")
    host = FakeRouterHost()
    loop = _make_loop(host, _DIRECTIVE)

    spy = _MessageCapturingScripted([_empty_stop_result(), text_result("ok")])

    with patch("reyn.chat.router_loop.call_llm_tools", spy):
        await loop.run("user query", [])

    assert spy.call_count == 2
    retry_msgs = spy.messages_per_call[1]
    directive_msgs = [
        m for m in retry_msgs
        if m.get("role") == "user" and m.get("content") == _DIRECTIVE
    ]
    assert len(directive_msgs) == 1, (
        f"expected 1 directive user msg in retry, got {len(directive_msgs)}; "
        f"roles in retry: {[m.get('role') for m in retry_msgs]}"
    )


# ---------------------------------------------------------------------------
# Retry path preserves the existing audit chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_stop_event_still_emitted_on_retry_path(monkeypatch):
    """Tier 2: the existing ``router_empty_response_detected`` audit event
    is emitted BEFORE the retry path forks, so the P6 audit trail records
    that an empty stop occurred regardless of recovery success.
    """
    monkeypatch.setenv("REYN_EMPTY_STOP_RETRY", "1")
    host = FakeRouterHost()
    loop = _make_loop(host, _DIRECTIVE)

    scripted = _ScriptedLLM([_empty_stop_result(), text_result("recovered")])

    with patch("reyn.chat.router_loop.call_llm_tools", scripted):
        await loop.run("test", [])

    emitted = [e["type"] for e in host._events.emitted]
    assert "router_empty_response_detected" in emitted
    # Event order: detected first, then retry injected
    idx_detected = emitted.index("router_empty_response_detected")
    idx_injected = emitted.index("router_empty_response_retry_injected")
    assert idx_detected < idx_injected
