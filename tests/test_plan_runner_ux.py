"""Tier 2: plan_runner UX surface — English summary + completion marker (issue #180).

Pre-fix:
  - ``spawn_plan_task`` emitted a Japanese hardcoded plan summary
    (``"以下の計画で実行します:\\n1. ..."``) that English-locale users
    saw untranslated every plan start (finding #1).
  - On clean exit no log marker was emitted — the sticky's
    "plan step N/M done" disappeared and the conv pane only had the
    initial summary block, leaving no persistent confirmation that
    the plan finished (finding #3).

Fix:
  1. Plan summary text is now English ("Executing plan:\\n1. ...").
  2. On clean exit, plan_runner emits a second system marker
     ``"plan complete: N/M steps succeeded · <plan_id>"`` (or
     ``"plan complete with errors: ..."`` when any step failed).

Finding #2 (= forward plan-step context to SkillActivityRow detail)
is M/P2 and lives in PlanRuntime + skill_runner, not plan_runner —
out of scope for this PR.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from reyn.chat.outbox import OutboxMessage
from reyn.chat.services.plan_runner import PlanRunner


@dataclass
class _FakeStep:
    description: str


@dataclass
class _FakePlan:
    steps: list[_FakeStep]


@dataclass
class _FakeResult:
    plan_goal: str = "demo goal"
    step_results: list[Any] = field(default_factory=list)
    step_failures: list[Any] = field(default_factory=list)
    n_steps: int = 0


class _FakeRuntime:
    """Stand-in PlanRuntime for the test — has .plan + an async run()."""

    def __init__(self, plan: _FakePlan, result: _FakeResult | None) -> None:
        self.plan = plan
        self._result = result

    async def run(self) -> _FakeResult | None:
        return self._result


@dataclass
class _FakeJournal:
    """Stand-in journal — record_plan_aborted is the only method we hit."""

    aborted: list[dict] = field(default_factory=list)

    async def record_plan_aborted(self, **kwargs) -> None:
        self.aborted.append(kwargs)


class _FakeRouterHost:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_plan_decomposition(self, *, plan_id: str) -> None:
        self.deleted.append(plan_id)


async def _build_runner(*, outbox_calls: list[OutboxMessage]):
    """Construct a real PlanRunner whose outbox callbacks capture messages."""

    enqueued: list[dict] = []

    async def _put_outbox(msg: OutboxMessage) -> None:
        outbox_calls.append(msg)

    async def _enqueue_plan_completed(**kwargs) -> None:
        enqueued.append(kwargs)

    runner = PlanRunner(
        agent_name="alpha",
        put_outbox=_put_outbox,
        enqueue_plan_completed=_enqueue_plan_completed,
        journal=_FakeJournal(),
        get_router_host=_FakeRouterHost,  # callable returning a fresh host
    )
    return runner, enqueued


# ── finding #1: English summary ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_summary_is_english() -> None:
    """Tier 2: spawn_plan_task emits "Executing plan:\\n..." (not Japanese)."""
    outbox: list[OutboxMessage] = []
    runner, _ = await _build_runner(outbox_calls=outbox)

    plan = _FakePlan(steps=[
        _FakeStep("first thing"),
        _FakeStep("second thing"),
    ])
    runtime = _FakeRuntime(plan, _FakeResult(n_steps=2))

    await runner.spawn_plan_task(
        plan_id="p-abc", runtime=runtime, chain_id="c-1",
    )
    # Let the inner task run to completion.
    await asyncio.gather(*runner.running_plans.values(), return_exceptions=True)

    summary_msgs = [m for m in outbox if m.meta.get("source") == "plan_summary"]
    assert len(summary_msgs) == 1
    assert summary_msgs[0].kind == "system"
    assert summary_msgs[0].text.startswith("Executing plan:")
    assert "first thing" in summary_msgs[0].text
    assert "second thing" in summary_msgs[0].text
    assert "以下の計画" not in summary_msgs[0].text


@pytest.mark.asyncio
async def test_plan_summary_carries_plan_id_meta() -> None:
    """Tier 2: summary message meta includes plan_id (= unchanged contract)."""
    outbox: list[OutboxMessage] = []
    runner, _ = await _build_runner(outbox_calls=outbox)
    plan = _FakePlan(steps=[_FakeStep("s")])
    runtime = _FakeRuntime(plan, _FakeResult(n_steps=1))
    await runner.spawn_plan_task(
        plan_id="p-zzz", runtime=runtime, chain_id="c-1",
    )
    await asyncio.gather(*runner.running_plans.values(), return_exceptions=True)
    summary = next(m for m in outbox if m.meta.get("source") == "plan_summary")
    assert summary.meta["plan_id"] == "p-zzz"


# ── finding #3: completion marker ────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_complete_marker_emitted_on_success() -> None:
    """Tier 2: clean exit + result emits "plan complete: N/N steps succeeded"."""
    outbox: list[OutboxMessage] = []
    runner, _ = await _build_runner(outbox_calls=outbox)
    plan = _FakePlan(steps=[_FakeStep("a"), _FakeStep("b"), _FakeStep("c")])
    result = _FakeResult(
        n_steps=3,
        step_results=["r1", "r2", "r3"],
        step_failures=[],
    )
    runtime = _FakeRuntime(plan, result)

    await runner.spawn_plan_task(
        plan_id="p-success", runtime=runtime, chain_id="c-1",
    )
    await asyncio.gather(*runner.running_plans.values(), return_exceptions=True)

    completion_msgs = [
        m for m in outbox if m.meta.get("source") == "plan_complete"
    ]
    assert len(completion_msgs) == 1
    assert completion_msgs[0].kind == "system"
    assert "plan complete:" in completion_msgs[0].text
    assert "3/3" in completion_msgs[0].text
    assert "p-success" in completion_msgs[0].text


@pytest.mark.asyncio
async def test_plan_complete_marker_reports_partial_failure() -> None:
    """Tier 2: when step_failures non-empty, marker uses the with-errors form.

    Pins the distinction so the user can tell "fully succeeded" from
    "some steps failed but plan reached completion".
    """
    outbox: list[OutboxMessage] = []
    runner, _ = await _build_runner(outbox_calls=outbox)
    plan = _FakePlan(steps=[_FakeStep("a"), _FakeStep("b"), _FakeStep("c")])
    result = _FakeResult(
        n_steps=3,
        step_results=["r1"],
        step_failures=["f1", "f2"],
    )
    runtime = _FakeRuntime(plan, result)

    await runner.spawn_plan_task(
        plan_id="p-partial", runtime=runtime, chain_id="c-1",
    )
    await asyncio.gather(*runner.running_plans.values(), return_exceptions=True)

    completion = next(
        m for m in outbox if m.meta.get("source") == "plan_complete"
    )
    assert "plan complete with errors:" in completion.text
    assert "1/3" in completion.text
    assert "2 failed" in completion.text


@pytest.mark.asyncio
async def test_plan_complete_marker_skipped_when_result_is_none() -> None:
    """Tier 2: no marker is emitted when runtime.run() returns None.

    Aborts / workflow exits that produce no result fall into a separate
    visible-state class — the runtime's own ``plan_run_interrupted``
    event covers them. The marker is reserved for the success path.
    """
    outbox: list[OutboxMessage] = []
    runner, _ = await _build_runner(outbox_calls=outbox)
    plan = _FakePlan(steps=[_FakeStep("a")])
    runtime = _FakeRuntime(plan, None)  # run() returns None

    await runner.spawn_plan_task(
        plan_id="p-abort", runtime=runtime, chain_id="c-1",
    )
    await asyncio.gather(*runner.running_plans.values(), return_exceptions=True)

    completion_msgs = [
        m for m in outbox if m.meta.get("source") == "plan_complete"
    ]
    assert completion_msgs == []
    # The summary still fired (= pre-execution, doesn't depend on result).
    summary_msgs = [m for m in outbox if m.meta.get("source") == "plan_summary"]
    assert len(summary_msgs) == 1
