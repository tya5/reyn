"""Tier 2: the #1593 PR-3 S1 three-arm ``Interpretation`` match in the OS loop.

``_run_scheme_tool_round`` dispatches on the scheme's ``Interpretation`` tagged
union (Execute / CodeBlock / RePresent). PR-3 single-owns this generalization so
PR-3 (CodeAct) and PR-4 (retrieval) ride one match, not two competing patches of
the same OS-loop seam. This test pins the routing invariant:

  - Execute   → ``_run_execute_round`` → ``(tool_calls, tool_results)`` (the
                byte-identical today-path; also covered end-to-end by the 22 exclude
                / universal-scheme tests).
  - CodeBlock → ``_run_codeblock_round`` (CodeAct body lands in PR-3 S2/S3).
  - RePresent → AssertionError (PR-4 owns the arm; unreached by any PR-3-era scheme).

No mocks: ``_FakeScheme`` is a real object implementing the ToolUseScheme surface
(a Fake, per testing.ja.md), emitting a chosen Interpretation.
"""
from __future__ import annotations

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.tools.scheme import (
    CodeBlock,
    ExecContext,
    Execute,
    ExecutionResult,
    RePresent,
)


class _FakeScheme:
    """A real (non-mock) ToolUseScheme that emits a fixed ``Interpretation`` from
    ``interpret`` — exercises the OS-loop match without driving a full LLM round.
    ``execute`` / ``format_feedback`` are the trivial identity used by the Execute
    arm with an empty action list."""

    def __init__(self, interp: object) -> None:
        self._interp = interp

    def build_presentation(self, available: object, layer_ctx: object, ops: object):
        raise AssertionError("build_presentation not exercised by this test")

    def interpret(self, llm_response: object, *, tool_catalog: dict, ops: object):
        return self._interp

    async def execute(self, interp: object, exec_ctx: ExecContext, ops: object):
        return ExecutionResult(tool_results=[])

    def format_feedback(self, exec_result: ExecutionResult, ops: object):
        return exec_result.tool_results


class _MinimalHost:
    """Construction-only host. ``RouterLoop.__init__`` stores ``host`` and resolves
    the scheme without calling any host method, so a bare object suffices for a
    match-routing test (verified against the __init__ body)."""


def _loop_with_scheme(interp: object) -> RouterLoop:
    loop = RouterLoop(host=_MinimalHost(), chain_id="t", max_iterations=1)
    loop._scheme = _FakeScheme(interp)  # the active scheme under test
    loop._catalog = {}
    return loop


@pytest.mark.asyncio
async def test_execute_arm_routes_and_returns() -> None:
    """Tier 2: Execute interp routes to the (byte-identical) execute round."""
    loop = _loop_with_scheme(Execute(actions=[]))
    tool_calls, tool_results = await loop._run_scheme_tool_round(object())
    assert tool_calls == []
    assert tool_results == []


@pytest.mark.asyncio
async def test_codeblock_arm_routes_to_codeact_body() -> None:
    """Tier 2: CodeBlock interp routes to the CodeAct arm (PR-3 S2/S3 body)."""
    loop = _loop_with_scheme(CodeBlock(code="x = 1"))
    with pytest.raises(NotImplementedError):
        await loop._run_scheme_tool_round(object())


@pytest.mark.asyncio
async def test_represent_arm_unreached_until_pr4() -> None:
    """Tier 2: RePresent interp is unreached until PR-4 owns the arm."""
    loop = _loop_with_scheme(RePresent(refinement=None))
    with pytest.raises(AssertionError, match="RePresent not reached"):
        await loop._run_scheme_tool_round(object())
