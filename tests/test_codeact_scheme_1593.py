"""Tier 2: #1593 PR-3 S3a — CodeActScheme (interpret + execute glue + feedback).

CodeAct is own-logic (not delegating). This pins:
  - interpret extracts the snippet (fenced or bare) as a CodeBlock.
  - execute threads the OS per-call gate (exec_ctx.extra['dispatch']) + sandbox into
    the CodeActRunner and wraps the result — and REQUIRES the gate (no silent
    ungated run).
  - format_feedback passes the runner envelope(s) through.

The per-call gate RE-ENTRY invariant (N calls → N gate invocations, exclude
per-call) is pinned at the runner level in test_codeact_runner_1593.py (the gate is
the dispatch callback execute forwards). Here we use a real Fake runner (records
what execute forwarded) — no mocks; build_presentation is S3b (async, e2e adapter).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from reyn.tools.scheme import CodeBlock, ExecContext, ExecutionResult
from reyn.tools.schemes.codeact import CodeActScheme


class _FakeRunner:
    """Records the args execute forwards; returns a canned envelope. A real Fake
    (implements ``run``), not a mock."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"ok": True, "status": "ok", "result": "ran"}


def test_interpret_extracts_fenced_code() -> None:
    """Tier 2: interpret pulls the fenced ```python block as the CodeBlock."""
    resp = SimpleNamespace(content="here:\n```python\nresult = tool('m')\n```\nthanks")
    interp = CodeActScheme().interpret(resp, tool_catalog={}, ops=None)
    assert isinstance(interp, CodeBlock)
    assert interp.code.strip() == "result = tool('m')"


def test_interpret_bare_content_when_no_fence() -> None:
    """Tier 2: interpret falls back to the whole content when there is no fence."""
    resp = SimpleNamespace(content="result = 1 + 1")
    interp = CodeActScheme().interpret(resp, tool_catalog={}, ops=None)
    assert interp.code == "result = 1 + 1"


@pytest.mark.asyncio
async def test_execute_threads_gate_and_sandbox_into_runner() -> None:
    """Tier 2: execute forwards the OS gate (exec_ctx.extra['dispatch']) + the
    sandbox + code into the runner, and wraps the envelope in ExecutionResult."""
    runner = _FakeRunner()
    scheme = CodeActScheme(runner=runner)

    async def gate(name: str, args: dict) -> dict:
        return {"status": "ok", "data": None}

    sentinel_sandbox = object()
    exec_ctx = ExecContext(
        sandbox=sentinel_sandbox,
        extra={"dispatch": gate, "sandbox_policy": {"network": False}, "timeout": 12},
    )
    res = await scheme.execute(CodeBlock(code="result = tool('m')"), exec_ctx, ops=None)

    assert isinstance(res, ExecutionResult)
    assert res.tool_results == [{"ok": True, "status": "ok", "result": "ran"}]
    forwarded = runner.calls[0]
    assert forwarded["dispatch"] is gate           # the OS gate is threaded through
    assert forwarded["sandbox_backend"] is sentinel_sandbox
    assert forwarded["code"] == "result = tool('m')"
    assert forwarded["sandbox_policy"] == {"network": False}
    assert forwarded["timeout"] == 12


@pytest.mark.asyncio
async def test_execute_requires_the_os_gate() -> None:
    """Tier 2: execute refuses to run without the OS gate (no silent ungated run)."""
    scheme = CodeActScheme(runner=_FakeRunner())
    exec_ctx = ExecContext(sandbox=object(), extra={})  # no 'dispatch'
    with pytest.raises(ValueError, match="dispatch"):
        await scheme.execute(CodeBlock(code="x = 1"), exec_ctx, ops=None)


def test_format_feedback_passthrough() -> None:
    """Tier 2: format_feedback returns the runner envelopes unchanged."""
    scheme = CodeActScheme(runner=_FakeRunner())
    results = [{"ok": True, "result": 1}]
    assert scheme.format_feedback(ExecutionResult(tool_results=results), ops=None) == results
