"""Tier 2: ChatSession wires the empty-stop retry directive into the
top-level chat router's RouterLoop (B43-NF-W6-1 fix).

Pinned invariants:

- ``_CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE`` exists in ``reyn.chat.session``
  as a non-empty string referencing "user" and "tool result" (= the chat
  router's narration-after-tool contract).
- It is **distinct** from the plan-step directive in
  ``reyn.chat.planner._PLAN_STEP_EMPTY_STOP_RETRY_DIRECTIVE`` (= plan-step
  says "step report"; chat router says "reply to the user"). The wording
  divergence matches the per-call-site voice and prevents accidental
  reuse of the plan-step text in the user-facing path.
- ``ChatSession._handle_user_message`` constructs ``RouterLoop`` with
  ``empty_stop_retry_directive=`` set to the chat-router constant — pinned
  via a real ``CapturingRouterLoop`` subclass injected through
  ``pytest.monkeypatch`` (= module-attribute setup, not fake-collaborator
  mocking per testing.ja.md).

References:
- PR #265 (merged): plan-step empty-stop retry — same opt-in mechanic on
  ``REYN_EMPTY_STOP_RETRY=1``, wired only on planner.py:868 + planner.py:955.
- B43-NF-W6-1: trace-patch-replay on the W6-S2 top-level router empty stop
  confirmed structural (= baseline 6/10 empty → patched 0/10 empty).
- Lead-coder review heuristic "new mechanism must cover every entry path":
  3 RouterLoop construction sites total. This test pins the third
  (session.py) is wired identically to the planner sites already
  pinned by ``test_router_loop_post_tool_user_directive`` /
  ``test_router_loop_empty_stop_retry``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.chat import router_loop as rl
from reyn.chat.session import (
    _CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE,
    ChatSession,
)
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage

_EMPTY_USAGE = TokenUsage(prompt_tokens=10, completion_tokens=5)


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


class _ScriptedLLM:
    """Real-fake callable matching the ``call_llm_tools`` signature."""

    def __init__(self, script: list[LLMToolCallResult]):
        self._script = list(script)
        self.call_count = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        result = self._script[self.call_count]
        self.call_count += 1
        return result


def _make_session(tmp_path: Path) -> ChatSession:
    return ChatSession(agent_name="test_agent_b44")


# ---------------------------------------------------------------------------
# Constant shape
# ---------------------------------------------------------------------------


def test_chat_router_directive_is_non_empty_string():
    """Tier 2: the directive must be a non-empty string — empty would defeat
    the retry path's purpose (= injecting nothing is equivalent to no
    retry)."""
    assert isinstance(_CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE, str)
    assert _CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE.strip()


def test_chat_router_directive_references_user_and_tool_result():
    """Tier 2: directive content references the chat-router contract —
    'user' (= the actual user, not the planner) and 'tool result' (= the
    prior tool response the LLM must narrate)."""
    directive = _CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE.lower()
    assert "user" in directive
    assert "tool result" in directive


def test_chat_router_directive_distinct_from_plan_step_directive():
    """Tier 2: must not accidentally reuse the plan-step wording. The
    plan-step directive says "step report"; the chat router says "reply"."""
    from reyn.chat.planner import _PLAN_STEP_EMPTY_STOP_RETRY_DIRECTIVE
    assert (
        _CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE
        != _PLAN_STEP_EMPTY_STOP_RETRY_DIRECTIVE
    )
    # Concrete voice markers
    assert "step report" not in _CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE.lower()
    assert "reply" in _CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE.lower()


# ---------------------------------------------------------------------------
# Wiring pin — ChatSession constructs RouterLoop with the directive set
# ---------------------------------------------------------------------------


class _CapturingRouterLoop(rl.RouterLoop):
    """Real subclass that records the kwargs every RouterLoop construction
    receives. Used in place of the production RouterLoop via
    ``monkeypatch.setattr`` so the test observes ChatSession's
    construction call without mocking the type contract."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any):
        # Snapshot all kwargs the caller passed before forwarding.
        _CapturingRouterLoop.last_kwargs = dict(kwargs)
        super().__init__(**kwargs)


@pytest.mark.asyncio
async def test_chat_session_passes_directive_to_router_loop(monkeypatch, tmp_path):
    """Tier 2c: ``ChatSession._handle_user_message`` wires directive to ``RouterLoop`` (B43-NF-W6-1).

    ``ChatSession._handle_user_message`` constructs its ``RouterLoop``
    with ``empty_stop_retry_directive=_CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE``.

    Without this wiring, the env-var-gated retry path remains dormant at
    the chat-router layer (= PR #265 only covered the planner sub_loop
    sites). N=10 trace-patch-replay on the W6-S2 top-level empty stop
    showed baseline 6/10 → patched 0/10, confirming the directive
    propagation is the load-bearing change. Pin both the kwarg's presence
    AND its identity against the module-level constant.
    """
    monkeypatch.chdir(tmp_path)
    # Replace the RouterLoop import inside session.py with the capturing
    # subclass. session._handle_user_message does ``from
    # reyn.chat.router_loop import RouterLoop`` inside the function, so
    # the module-level patch is what gets observed.
    monkeypatch.setattr(rl, "RouterLoop", _CapturingRouterLoop)

    # Replace the LLM caller with a scripted text reply so the run
    # completes without network. We use a single text_result to exit the
    # loop after one iteration — sufficient to capture the construction.
    scripted = _ScriptedLLM([_text_result("ok")])
    monkeypatch.setattr(rl, "call_llm_tools", scripted)

    session = _make_session(tmp_path)
    session.is_attached = True

    await session._handle_user_message("hello", chain_id="chain-test-b44")

    # The capturing subclass recorded the kwargs from the construction.
    captured = _CapturingRouterLoop.last_kwargs
    assert "empty_stop_retry_directive" in captured, (
        "ChatSession must pass empty_stop_retry_directive to RouterLoop "
        "(= B43-NF-W6-1 wiring). Got kwargs: " + str(list(captured.keys()))
    )
    assert (
        captured["empty_stop_retry_directive"]
        == _CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE
    ), (
        "ChatSession must pass the canonical chat-router directive, not "
        "an inlined or accidentally-renamed string. Pinning identity "
        "against _CHAT_ROUTER_EMPTY_STOP_RETRY_DIRECTIVE catches both "
        "accidental copy-edits and re-use of the plan-step directive."
    )


@pytest.mark.asyncio
async def test_chat_session_directive_is_distinct_from_plan_step_at_wire(
    monkeypatch, tmp_path,
):
    """Tier 2: regression guard — the directive ChatSession passes is
    NOT the plan-step constant. Catches accidental copy-paste of the
    planner wiring (= which would say "step report" to actual users)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rl, "RouterLoop", _CapturingRouterLoop)
    scripted = _ScriptedLLM([_text_result("ok")])
    monkeypatch.setattr(rl, "call_llm_tools", scripted)

    session = _make_session(tmp_path)
    session.is_attached = True
    await session._handle_user_message("hello", chain_id="chain-b44-distinct")

    captured = _CapturingRouterLoop.last_kwargs
    from reyn.chat.planner import _PLAN_STEP_EMPTY_STOP_RETRY_DIRECTIVE
    assert (
        captured["empty_stop_retry_directive"]
        != _PLAN_STEP_EMPTY_STOP_RETRY_DIRECTIVE
    )
