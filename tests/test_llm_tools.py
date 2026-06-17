"""Unit tests for call_llm_tools and call_llm — the tool_use and JSON-mode LLM wrappers.

Migration note (PR-test-policy-1):
  All LLM-touching tests now use @pytest.mark.replay (LLMReplay Fake) instead of
  unittest.mock patches on litellm.acompletion.

  Remaining unittest.mock usages are Tier 1 (framework contract) or Tier 1 (error path)
  — see individual docstrings.
"""
from __future__ import annotations

import pytest

from reyn.llm.llm import call_llm, call_llm_tools
from reyn.schemas.models import ContextFrame

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

MODEL = "gemini-2.5-flash-lite"  # bare name — consistent across record (proxy strips prefix)
                                   # and replay (no proxy, key matches)

MINIMAL_MESSAGES = [{"role": "user", "content": "hi"}]
MINIMAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": "run a skill",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def _make_budget_tracker(*, per_agent_tokens_hard: int | None = None):
    """Build a real BudgetTracker with optional per-agent token cap."""
    from reyn.budget.budget import BudgetTracker, CostConfig, CostLimitConfig
    cfg = CostConfig()
    if per_agent_tokens_hard is not None:
        cfg.per_agent_tokens = CostLimitConfig(hard_limit=per_agent_tokens_hard)
    return BudgetTracker(cfg)


def _minimal_context_frame() -> ContextFrame:
    """Minimal ContextFrame for call_llm replay tests."""
    from reyn.dev.testing.replay import REPLAY_DATETIME
    return ContextFrame(
        current_phase="test",
        instructions="Reply with a minimal valid JSON decide turn.",
        input_artifact={},
        candidate_outputs=[],
        output_language="en",
        current_datetime=REPLAY_DATETIME,
    )


# ---------------------------------------------------------------------------
# Tier 1 — Framework contract: kwargs forwarding to litellm
# These tests verify that call_llm_tools passes the correct kwargs to
# litellm.acompletion. LLMReplay cannot inspect kwargs at this level (it only
# returns a stored response), so a minimal monkeypatch capturing the call is
# the only way to assert what litellm actually received. This is intentional:
# the tested invariants are internal contract rules (stream=False, no
# response_format) not LLM behavior.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_false_is_forced(monkeypatch):
    """Tier 1: framework contract: stream=False must be passed to litellm regardless of caller input.

    Uses a capturing async function (not AsyncMock) to inspect kwargs.
    Framework boundary — intentional monkeypatch.
    """
    import litellm

    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        # Return a minimal litellm-compatible ModelResponse
        # Build a minimal response dict; litellm.ModelResponse accepts this shape.
        msg = type("_Msg", (), {"content": "ok", "tool_calls": None})()
        choice = type("_Choice", (), {"message": msg, "finish_reason": "stop"})()
        usage = type("_Usage", (), {"prompt_tokens": 1, "completion_tokens": 1})()
        resp = type("_Resp", (), {"choices": [choice], "usage": usage})()
        return resp

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    await call_llm_tools(
        model=MODEL,
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
    )

    assert captured.get("stream") is False


@pytest.mark.asyncio
async def test_response_format_not_passed(monkeypatch):
    """Tier 1: framework contract: response_format must NOT be passed (incompatible with tools= on most providers).

    Framework boundary — intentional monkeypatch.
    """
    import litellm

    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        msg = type("_Msg", (), {"content": "ok", "tool_calls": None})()
        choice = type("_Choice", (), {"message": msg, "finish_reason": "stop"})()
        usage = type("_Usage", (), {"prompt_tokens": 1, "completion_tokens": 1})()
        return type("_Resp", (), {"choices": [choice], "usage": usage})()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    await call_llm_tools(
        model=MODEL,
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
    )

    assert "response_format" not in captured


@pytest.mark.asyncio
async def test_tools_and_tool_choice_passed_through(monkeypatch):
    """Tier 1: framework contract: tools and tool_choice arrive at litellm verbatim.

    Framework boundary — intentional monkeypatch.
    """
    import litellm

    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        msg = type("_Msg", (), {"content": "ok", "tool_calls": None})()
        choice = type("_Choice", (), {"message": msg, "finish_reason": "stop"})()
        usage = type("_Usage", (), {"prompt_tokens": 1, "completion_tokens": 1})()
        return type("_Resp", (), {"choices": [choice], "usage": usage})()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    custom_tools = [{"type": "function", "function": {"name": "custom"}}]
    await call_llm_tools(
        model=MODEL,
        messages=MINIMAL_MESSAGES,
        tools=custom_tools,
        tool_choice="required",
    )

    assert captured["tools"] == custom_tools
    assert captured["tool_choice"] == "required"


# ---------------------------------------------------------------------------
# Tier 1 — Error path: BudgetExceeded before LLM call
# LLMReplay cannot help here: the function raises before calling litellm at all.
# The test verifies that litellm is never called, which requires asserting the
# absence of an LLM call — not its presence or result.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_llm_tools_pre_check_blocks_when_over_quota(monkeypatch):
    """Tier 1: error path: when per-agent token cap is exhausted, call raises BudgetExceeded
    before calling litellm.

    LLMReplay cannot cover this path (litellm is never reached). Framework boundary.
    """
    import litellm

    called = []

    async def fake_acompletion(**kwargs):
        called.append(True)
        return None  # should never get here

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    from reyn.budget.budget import BudgetExceeded
    from reyn.llm.pricing import TokenUsage

    # Set hard limit of 10 tokens, then fill it up
    tracker = _make_budget_tracker(per_agent_tokens_hard=10)
    tracker.record_llm(
        model="openai/gpt-4o",
        agent="test-agent",
        usage=TokenUsage(prompt_tokens=8, completion_tokens=5),  # total=13 > 10
    )

    with pytest.raises(BudgetExceeded):
        await call_llm_tools(
            model=MODEL,
            messages=MINIMAL_MESSAGES,
            tools=MINIMAL_TOOLS,
            budget=tracker,
            budget_agent="test-agent",
        )

    # litellm must NOT have been called
    assert called == []


@pytest.mark.asyncio
async def test_call_llm_pre_check_blocks_when_over_quota(monkeypatch):
    """Tier 1: error path: call_llm raises BudgetExceeded before calling litellm when cap exceeded.

    LLMReplay cannot cover this path (litellm is never reached). Framework boundary.
    """
    import litellm

    called = []

    async def fake_acompletion(**kwargs):
        called.append(True)
        return None

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    from reyn.budget.budget import BudgetExceeded
    from reyn.llm.pricing import TokenUsage

    tracker = _make_budget_tracker(per_agent_tokens_hard=5)
    tracker.record_llm(
        model="openai/gpt-4o",
        agent="skill-agent",
        usage=TokenUsage(prompt_tokens=4, completion_tokens=3),  # total=7 > 5
    )

    frame = _minimal_context_frame()
    with pytest.raises(BudgetExceeded):
        await call_llm(
            MODEL,
            frame,
            budget=tracker,
            budget_agent="skill-agent",
        )

    assert called == []


# ---------------------------------------------------------------------------
# Tier 3a — Replay tests: LLM output → call_llm_tools / call_llm behavior
# ---------------------------------------------------------------------------

@pytest.mark.replay("fixtures/llm/llm_tools/text_only.jsonl")
@pytest.mark.asyncio
async def test_call_llm_tools_returns_text_when_no_tool_calls():
    """Tier 3a: when the LLM returns plain text with no tool_calls, result reflects that."""
    result = await call_llm_tools(
        model=MODEL,
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
    )

    assert isinstance(result.content, str)
    assert len(result.content) > 0
    assert result.tool_calls == []


@pytest.mark.replay("fixtures/llm/llm_tools/tool_call.jsonl")
@pytest.mark.asyncio
async def test_call_llm_tools_returns_normalized_tool_calls():
    """Tier 3a: tool_calls are normalized to plain dicts (no litellm internals)."""
    messages = [{"role": "user", "content": "call the run_skill tool with skill=hello"}]
    result = await call_llm_tools(
        model=MODEL,
        messages=messages,
        tools=MINIMAL_TOOLS,
        tool_choice="required",
    )

    assert len(result.tool_calls) >= 1
    tc_out = result.tool_calls[0]
    # Must be a plain dict, not a litellm internal object
    assert isinstance(tc_out, dict)
    assert tc_out["type"] == "function"
    assert isinstance(tc_out["function"], dict)
    assert "name" in tc_out["function"]
    assert "arguments" in tc_out["function"]
    # arguments must be a JSON string (not a dict)
    assert isinstance(tc_out["function"]["arguments"], str)


@pytest.mark.replay("fixtures/llm/llm_tools/text_only.jsonl")
@pytest.mark.asyncio
async def test_call_llm_tools_records_tokens_to_budget():
    """Tier 3a: budget tracker accumulates prompt+completion tokens after a call.

    Re-uses text_only.jsonl (same MINIMAL_MESSAGES+MINIMAL_TOOLS key) so no
    separate fixture file is needed — the SHA-256 key is identical.
    """
    tracker = _make_budget_tracker()
    await call_llm_tools(
        model=MODEL,
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
        budget=tracker,
        budget_agent="test-agent",
    )

    snap = tracker.snapshot()
    recorded = snap["agent_tokens"].get("test-agent", 0)
    assert recorded > 0, f"Expected non-zero tokens recorded; got snapshot={snap}"
    assert snap["daily_tokens"] == recorded
    assert snap["monthly_tokens"] == recorded


@pytest.mark.replay("fixtures/llm/llm_tools/text_only.jsonl")
@pytest.mark.asyncio
async def test_call_llm_tools_no_budget_kwarg_skips_tracking():
    """Tier 3a: backward compat — budget=None (default) skips tracking, no side effects.

    Re-uses text_only.jsonl (same MINIMAL_MESSAGES+MINIMAL_TOOLS key).
    """
    result = await call_llm_tools(
        model=MODEL,
        messages=MINIMAL_MESSAGES,
        tools=MINIMAL_TOOLS,
        # No budget kwarg
    )
    # Result is valid regardless of budget: content or tool_calls present
    assert isinstance(result.content, str) or len(result.tool_calls) > 0


@pytest.mark.replay("fixtures/llm/llm_tools/call_llm_budget.jsonl")
@pytest.mark.asyncio
async def test_call_llm_records_tokens_to_budget():
    """Tier 3a: call_llm records tokens when budget kwarg is provided."""
    tracker = _make_budget_tracker()
    frame = _minimal_context_frame()
    await call_llm(
        MODEL,
        frame,
        prompt_cache_enabled=False,
        budget=tracker,
        budget_agent="skill-agent",
    )

    snap = tracker.snapshot()
    recorded = snap["agent_tokens"].get("skill-agent", 0)
    assert recorded > 0, f"Expected non-zero tokens for skill-agent; snapshot={snap}"


# ---------------------------------------------------------------------------
# Tier 3a — Drift detection
# ---------------------------------------------------------------------------

@pytest.mark.replay("fixtures/llm/llm_tools/text_only.jsonl")
@pytest.mark.asyncio
async def test_llm_tools_drift_detection():
    """Tier 3a: drift detection: changes in messages/tools must invalidate the fixture key
    and raise MissingFixture. Ensures that replay test keys are tight enough to catch
    prompt drift."""
    from reyn.dev.testing.replay import MissingFixture

    # Use a different message from the fixture — the SHA-256 key will not match
    different_messages = [{"role": "user", "content": "intentionally different from fixture"}]

    with pytest.raises(MissingFixture):
        await call_llm_tools(
            model=MODEL,
            messages=different_messages,
            tools=MINIMAL_TOOLS,
        )
