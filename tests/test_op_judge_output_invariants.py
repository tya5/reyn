"""Tier 2: OS invariant tests for judge_output op handler (FP-0007 Component D).

Design notes (testing policy alignment):
- No MagicMock / AsyncMock / patch. litellm.acompletion is replaced with
  a real callable stub (allowed by testing policy).
- All assertions use the public execute_op → result dict surface.
- P6 audit: event emission is verified via EventLog.events (public read).
- P3 / P7: the test stubs return deterministic JSON; the OS is never
  asked to interpret the rubric content.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime import execute_op
from reyn.core.op_runtime.context import OpContext
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import JudgeOutputIROp
from reyn.security.permissions.permissions import PermissionDecl

# ---------------------------------------------------------------------------
# Fake litellm response (real callable — not MagicMock)
# ---------------------------------------------------------------------------


def _make_fake_response(content: str) -> object:
    """Build a minimal litellm-shaped response object."""
    msg = type("_Msg", (), {"content": content, "tool_calls": None})()
    choice = type("_Choice", (), {"message": msg, "finish_reason": "stop"})()
    usage = type("_Usage", (), {"prompt_tokens": 5, "completion_tokens": 5})()
    return type("_Resp", (), {"choices": [choice], "usage": usage})()


class _FakeLLM:
    """Real callable stub that returns a scripted JSON response."""

    def __init__(self, score: float, reason: str = "test reason") -> None:
        self._content = json.dumps({"score": score, "reason": reason})
        self.call_count = 0
        self.last_messages: list[dict] = []

    async def __call__(self, **kwargs: Any) -> object:
        self.call_count += 1
        self.last_messages = kwargs.get("messages", [])
        return _make_fake_response(self._content)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(tmp_path: Path) -> OpContext:
    """Construct a minimal OpContext using real instances (no mocks)."""
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
    )


def _make_op(
    target: str = "artifact.data.summary",
    rubric: str = "Score 0-1: is the summary non-empty?",
    threshold: float = 0.8,
    on_fail: str = "transition",
    model: str | None = None,
) -> JudgeOutputIROp:
    return JudgeOutputIROp(
        kind="judge_output",
        target=target,
        rubric=rubric,
        threshold=threshold,
        on_fail=on_fail,
        model=model,
    )


def _seed_artifact(ctx: OpContext, artifact: dict[str, Any]) -> None:
    """Append a synthetic artifact entry to workspace.artifacts."""
    ctx.workspace.artifacts.append({"phase": "test", "artifact": artifact, "path": ""})


# ---------------------------------------------------------------------------
# Tier 2 invariant tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_output_emits_tool_executed_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: judge_output op execution emits a tool_executed P6 audit event."""
    import litellm

    fake_llm = _FakeLLM(score=0.9, reason="good summary")
    monkeypatch.setattr(litellm, "acompletion", fake_llm)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    _seed_artifact(ctx, {"type": "t", "data": {"summary": "Hello world"}})
    op = _make_op(target="artifact.data.summary", threshold=0.8)

    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", f"unexpected error: {result}"
    # P6: at least one tool_executed event must have been emitted
    tool_events = [e for e in ctx.events.all() if e.type == "tool_executed"]
    assert tool_events, "judge_output must emit a tool_executed event (P6)"
    evt = tool_events[-1]
    assert evt.data.get("op") == "judge_output"
    assert evt.data.get("target") == "artifact.data.summary"
    assert "score" in evt.data
    assert "passed" in evt.data
    assert "threshold" in evt.data


@pytest.mark.asyncio
async def test_judge_output_passing_score_returns_passed_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: when LLM returns score >= threshold, passed=True is returned."""
    import litellm

    fake_llm = _FakeLLM(score=0.9, reason="well structured")
    monkeypatch.setattr(litellm, "acompletion", fake_llm)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    _seed_artifact(ctx, {"type": "t", "data": {"summary": "A clear and concise summary."}})
    op = _make_op(target="artifact.data.summary", threshold=0.8)

    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", f"unexpected error: {result}"
    assert result["kind"] == "judge_output"
    assert result["passed"] is True
    assert abs(result["score"] - 0.9) < 1e-6
    assert result["threshold"] == 0.8
    assert fake_llm.call_count == 1


@pytest.mark.asyncio
async def test_judge_output_failing_score_returns_passed_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: when LLM returns score < threshold, passed=False is returned."""
    import litellm

    fake_llm = _FakeLLM(score=0.5, reason="too vague")
    monkeypatch.setattr(litellm, "acompletion", fake_llm)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    _seed_artifact(ctx, {"type": "t", "data": {"summary": "Vague text."}})
    op = _make_op(target="artifact.data.summary", threshold=0.8)

    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", f"unexpected error: {result}"
    assert result["kind"] == "judge_output"
    assert result["passed"] is False
    assert abs(result["score"] - 0.5) < 1e-6
    assert result["reason"] == "too vague"


@pytest.mark.asyncio
async def test_judge_output_resolves_target_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: target path 'artifact.data.summary' resolves to the nested value.

    The value extracted from artifact.data.summary must appear in the LLM
    messages, proving the OS performed the path traversal correctly.
    """
    import litellm

    fake_llm = _FakeLLM(score=0.85, reason="target resolved correctly")
    monkeypatch.setattr(litellm, "acompletion", fake_llm)
    monkeypatch.chdir(tmp_path)

    ctx = _make_ctx(tmp_path)
    # artifact = {"data": {"summary": "X"}} stored under "artifact" key in resolution ctx
    _seed_artifact(ctx, {"data": {"summary": "X"}})
    op = _make_op(target="artifact.data.summary", threshold=0.5)

    result = await execute_op(op, ctx, caller="control_ir")

    assert result.get("status") != "error", f"unexpected error: {result}"
    assert fake_llm.call_count == 1, "LLM must have been called once"

    # The user message must contain the resolved value "X"
    user_msgs = [m for m in fake_llm.last_messages if m.get("role") == "user"]
    assert user_msgs, "LLM must receive a user message"
    user_content = user_msgs[-1].get("content", "")
    assert '"X"' in user_content or "X" in user_content, (
        f"Resolved value 'X' must appear in LLM user message; got: {user_content[:200]}"
    )
