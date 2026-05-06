"""Tier 2: OS invariant — ``world`` purity ops skip memo lookup on resume.

Background: dispatch_tool memoizes against ResumePlan.committed_steps to
prevent duplicate side effects on resume. However, ``world`` purity ops
(web_fetch, web_search, etc.) read external state that may be transient
or flaky — a recorded "0 results" from a flaky API call would lock in
forever if memoized. Re-executing on resume gives a fresh world view.

The contract under test:
  - ``world`` purity + ``resume_plan`` set + matching CommittedStep →
    memo is BYPASSED, invoker is called fresh.
  - ``world`` purity + ``resume_plan`` set + matching CommittedStep →
    no ``step_memoized`` audit event (= it really fell through).
  - ``side_effect`` and ``external`` purity → memo HIT (regression
    check; existing memoization behavior preserved).
  - ``world`` + ``resume_plan=None`` → normal execution (sanity).

Reference: PR-memo-purity-fix M2 in the active plan.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.dispatch import DispatchContext, dispatch_tool
from reyn.skill.skill_resume_analyzer import (
    CommittedStep,
    ResumePlan,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **data: Any) -> None:
        self.events.append((event_type, data))


_CATALOG = {
    "web_fetch":  {"function": {"name": "web_fetch"}},
    "web_search": {"function": {"name": "web_search"}},
    "file":       {"function": {"name": "file"}},
    "mcp":        {"function": {"name": "mcp"}},
}


def _make_ctx(
    *,
    resume_plan: ResumePlan | None = None,
    phase: str = "search",
) -> tuple[DispatchContext, _FakeEvents]:
    ev = _FakeEvents()
    return (
        DispatchContext(
            caller_kind="skill_phase",
            caller_id="search_skill.search",
            chain_id="c1",
            tool_catalog=_CATALOG,
            events=ev,
            skill_run_id="run_world",
            phase=phase,
            resume_plan=resume_plan,
        ),
        ev,
    )


def _plan_with(steps: list[CommittedStep]) -> ResumePlan:
    return ResumePlan(
        run_id="run_world",
        skill_name="search_skill",
        skill_input={},
        current_phase="search",
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=steps,
    )


# ---------------------------------------------------------------------------
# world purity bypasses memo
# ---------------------------------------------------------------------------


def test_world_op_bypasses_memo_and_invokes_fresh():
    """Tier 2: ``world`` op + matching CommittedStep → invoker IS called.

    The recorded result (e.g. transient "0 results" from a flaky API)
    must not silently lock the skill into a wrong path on resume.
    """
    invoker_called = []

    async def invoker(args):
        invoker_called.append(True)
        return {"results": ["fresh", "data"]}

    async def go():
        from reyn.dispatch.dispatcher import _compute_args_hash
        args = {"url": "https://example.com/search?q=foo"}
        args_hash = _compute_args_hash(args)
        plan = _plan_with([
            CommittedStep(
                op_invocation_id="search.0",
                op_kind="web_fetch",
                phase="search",
                args_hash=args_hash,
                seq=10,
                result={"results": []},  # transient/flaky empty result
            ),
        ])
        ctx, ev = _make_ctx(resume_plan=plan)
        result = await dispatch_tool(
            name="web_fetch", args=args, ctx=ctx, invoker=invoker,
            op_invocation_id="search.0",
        )
        return result, ev

    result, ev = asyncio.run(go())
    assert invoker_called == [True], (
        "world op must re-execute, not replay memo"
    )
    assert result == {"status": "ok", "data": {"results": ["fresh", "data"]}}
    types = [t for t, _ in ev.events]
    assert "step_memoized" not in types, (
        f"world op skip must NOT emit step_memoized; got {types}"
    )


def test_world_op_web_search_also_bypasses_memo():
    """Tier 2: same skip applies to ``web_search`` (any world purity)."""
    invoker_called = []

    async def invoker(args):
        invoker_called.append(True)
        return {"hits": ["fresh"]}

    async def go():
        from reyn.dispatch.dispatcher import _compute_args_hash
        args = {"q": "reyn skill resume"}
        args_hash = _compute_args_hash(args)
        plan = _plan_with([
            CommittedStep(
                op_invocation_id="search.1",
                op_kind="web_search",
                phase="search",
                args_hash=args_hash,
                seq=11,
                result={"hits": []},
            ),
        ])
        ctx, _ = _make_ctx(resume_plan=plan)
        return await dispatch_tool(
            name="web_search", args=args, ctx=ctx, invoker=invoker,
            op_invocation_id="search.1",
        )

    result = asyncio.run(go())
    assert invoker_called == [True]
    assert result["data"] == {"hits": ["fresh"]}


# ---------------------------------------------------------------------------
# Non-world purity preserves memo (regression check)
# ---------------------------------------------------------------------------


def test_side_effect_op_still_uses_memo():
    """Tier 2: regression — ``side_effect`` (file) still memoizes on resume.

    Critical: the memo skip must be NARROW to ``world`` only. Letting
    ``file/write`` re-execute on resume would duplicate workspace
    writes — exactly what memoization prevents.
    """
    invoker_called = []

    async def invoker(args):
        invoker_called.append(True)
        return {"freshly_written": True}

    async def go():
        from reyn.dispatch.dispatcher import _compute_args_hash
        args = {"op": "write", "path": "x.txt", "content": "y"}
        args_hash = _compute_args_hash(args)
        plan = _plan_with([
            CommittedStep(
                op_invocation_id="search.0",
                op_kind="file",
                phase="search",
                args_hash=args_hash,
                seq=12,
                result={"saved": "x.txt"},
            ),
        ])
        ctx, _ = _make_ctx(resume_plan=plan)
        return await dispatch_tool(
            name="file", args=args, ctx=ctx, invoker=invoker,
            op_invocation_id="search.0",
        )

    result = asyncio.run(go())
    assert invoker_called == [], (
        "side_effect must still memoize (no double-write on resume)"
    )
    assert result == {"status": "ok", "data": {"saved": "x.txt"}}


def test_external_op_mcp_still_uses_memo():
    """Tier 2: regression — ``external`` (mcp) still memoizes on resume.

    mcp/call_tool may invoke write APIs (Notion create_page, Slack
    send) — replaying via memo prevents duplicate side effects.
    Read-only mcp APIs are technically world-pure but cannot be
    distinguished at the registry level; conservative memoization is
    safer than risking double writes.
    """
    invoker_called = []

    async def invoker(args):
        invoker_called.append(True)
        return {"page_id": "fresh_id"}

    async def go():
        from reyn.dispatch.dispatcher import _compute_args_hash
        args = {"server": "notion", "name": "create_page",
                "arguments": {"title": "hi"}}
        args_hash = _compute_args_hash(args)
        plan = _plan_with([
            CommittedStep(
                op_invocation_id="search.0",
                op_kind="mcp",
                phase="search",
                args_hash=args_hash,
                seq=13,
                result={"page_id": "original_id"},
            ),
        ])
        ctx, _ = _make_ctx(resume_plan=plan)
        return await dispatch_tool(
            name="mcp", args=args, ctx=ctx, invoker=invoker,
            op_invocation_id="search.0",
        )

    result = asyncio.run(go())
    assert invoker_called == [], (
        "external must still memoize (no duplicate mcp call on resume)"
    )
    assert result["data"] == {"page_id": "original_id"}


# ---------------------------------------------------------------------------
# Backward compat — world op without resume_plan
# ---------------------------------------------------------------------------


def test_world_op_with_no_resume_plan_executes_normally():
    """Tier 2: backward compat — world op + resume_plan=None → fresh execution."""
    invoker_called = []

    async def invoker(args):
        invoker_called.append(True)
        return {"results": ["x"]}

    async def go():
        ctx, _ = _make_ctx(resume_plan=None)
        return await dispatch_tool(
            name="web_fetch", args={"url": "https://e.com"},
            ctx=ctx, invoker=invoker, op_invocation_id="search.0",
        )

    result = asyncio.run(go())
    assert invoker_called == [True]
    assert result["data"] == {"results": ["x"]}
