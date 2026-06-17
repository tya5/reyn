"""Tier 2: OS invariant — ``world`` purity op event-emission consistency.

The memo-skip for world purity (M2) is a NARROW exception: only memo
lookup is bypassed. Everything else about world op behavior must
remain consistent with the broader dispatch contract:

  - Each invocation produces a fresh ``step_completed`` WAL entry so
    a future resume sees the latest world view.
  - Audit events (``tool_returned``) are emitted with the fresh result.
  - ``tool_called`` is NOT emitted (world has no side-effect ambiguity
    to flag — same as the existing dispatcher policy).
  - ``step_started`` is NOT emitted (no ambiguity to disambiguate).
  - Repeated calls to the same world op within a single run all
    execute fresh (no in-flight cache).

Reference: PR-memo-purity-fix M3 in the active plan.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from reyn.core.dispatch import DispatchContext, dispatch_tool
from reyn.core.events.state_log import StateLog
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
}


def _make_ctx(
    *,
    state_log: StateLog | None = None,
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
            state_log=state_log,
            skill_run_id="run_consistency",
            phase=phase,
            resume_plan=resume_plan,
        ),
        ev,
    )


# ---------------------------------------------------------------------------
# Fresh execution emits step_completed (so future resume can see it)
# ---------------------------------------------------------------------------


def test_world_op_fresh_run_emits_step_completed(tmp_path: Path):
    """Tier 2: world op writes a step_completed WAL entry on fresh execution.

    Even though world op skips memo on resume, the current call's
    result must still be persisted — otherwise a *subsequent* crash +
    resume would have no record of this call.
    """
    log = StateLog(tmp_path / "wal.jsonl")

    async def invoker(args):
        return {"results": ["one", "two"]}

    async def go():
        ctx, _ = _make_ctx(state_log=log, resume_plan=None)
        await dispatch_tool(
            name="web_fetch",
            args={"url": "https://example.com"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="search.0",
        )

    asyncio.run(go())
    kinds = [e["kind"] for e in log.iter_from(0)]
    assert "step_completed" in kinds, (
        f"world op must emit step_completed for future-resume visibility; got {kinds}"
    )
    assert "step_started" not in kinds, (
        "world op must NOT emit step_started (no ambiguity to disambiguate)"
    )


def test_world_op_resume_skip_still_emits_step_completed(tmp_path: Path):
    """Tier 2: world op + memo skip on resume → still emits a fresh step_completed.

    The memo-skipped invocation produces a NEW result; that new result
    must replace (= append after) the original in the WAL so the next
    resume uses the freshest view.
    """
    log = StateLog(tmp_path / "wal.jsonl")

    async def invoker(args):
        return {"results": ["fresh_after_resume"]}

    async def go():
        from reyn.core.dispatch.dispatcher import _compute_args_hash
        args = {"url": "https://e.com"}
        plan = ResumePlan(
            run_id="run_consistency",
            skill_name="search_skill",
            skill_input={},
            current_phase="search",
            last_phase_artifact_path=None,
            awaiting_intervention_id=None,
            committed_steps=[
                CommittedStep(
                    op_invocation_id="search.0",
                    op_kind="web_fetch",
                    phase="search",
                    args_hash=_compute_args_hash(args),
                    seq=10,
                    result={"results": []},
                ),
            ],
        )
        ctx, _ = _make_ctx(state_log=log, resume_plan=plan)
        await dispatch_tool(
            name="web_fetch", args=args, ctx=ctx, invoker=invoker,
            op_invocation_id="search.0",
        )

    asyncio.run(go())
    completed = [
        e for e in log.iter_from(0) if e["kind"] == "step_completed"
    ]
    assert completed
    # The fresh result replaces the recorded stale value
    assert completed[0]["result"] == {"results": ["fresh_after_resume"]}


# ---------------------------------------------------------------------------
# Audit event semantics for world purity
# ---------------------------------------------------------------------------


def test_world_op_emits_tool_returned_but_not_tool_called():
    """Tier 2: world purity audit-event policy — only post-event.

    Pre-event ``tool_called`` is gated to side_effect/external (where
    a crash mid-execution leaves an ambiguous state). World ops have
    no such ambiguity so the pre-event is skipped. The post-event
    ``tool_returned`` records the fresh result for forensics.
    """
    async def invoker(args):
        return {"results": ["x"]}

    async def go():
        ctx, ev = _make_ctx()
        await dispatch_tool(
            name="web_search", args={"q": "x"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="search.0",
        )
        return ev

    ev = asyncio.run(go())
    types = [t for t, _ in ev.events]
    assert "tool_returned" in types
    assert "tool_called" not in types, (
        f"world purity must skip tool_called; got {types}"
    )


# ---------------------------------------------------------------------------
# Repeated calls in same run all execute (no in-flight cache)
# ---------------------------------------------------------------------------


def test_repeated_world_op_in_same_run_all_execute():
    """Tier 2: same world op called twice in a fresh run → invoker called twice.

    The dispatcher does NOT cache within a run. Each LLM act_turn
    that requests a world op re-fetches truth from the world. This
    is intentional — caching would require an in-memory layer that
    doesn't survive crashes anyway, and the plan deliberately defers
    that for simplicity.
    """
    invoker_calls = []

    async def invoker(args):
        invoker_calls.append(args["url"])
        return {"results": [args["url"]]}

    async def go():
        ctx, _ = _make_ctx()
        # First call
        await dispatch_tool(
            name="web_fetch", args={"url": "https://a.com"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="search.0",
        )
        # Second call — same args, same context, fresh op_invocation_id
        await dispatch_tool(
            name="web_fetch", args={"url": "https://a.com"},
            ctx=ctx, invoker=invoker,
            op_invocation_id="search.1",
        )

    asyncio.run(go())
    assert invoker_calls == ["https://a.com", "https://a.com"], (
        "world op has no in-flight cache; both calls execute"
    )
