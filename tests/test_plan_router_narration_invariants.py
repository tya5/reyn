"""Tier 2: FP-0025 C — router narration invariants for plan completion.

Pins the contract that plan completion drives router synthesis via the
``plan_completed`` inbox path (symmetric with FP-0012 ``skill_completed``):

1. ``spawn_plan_task`` on clean exit enqueues a ``plan_completed`` inbox
   message (= not a direct ``agent`` outbox emit).
2. ``_handle_plan_completed`` injects a ``[task_completed] kind=plan`` user-role
   message into history so the router LLM sees step_results.
3. ``_handle_plan_completed`` calls ``_run_router_loop`` exactly once (=
   the synthesis turn).

No mocks of collaborators — real Session + real PlanExecutionResult.
``_run_router_loop`` is stubbed via monkeypatch.setattr (not MagicMock/patch)
so the LLM is not invoked in the invariant tests (Tier 2 scope: inbox
enqueue / history inject / turn invocation; not LLM response content).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.planner import PlanExecutionResult
from reyn.chat.session import Session
from reyn.core.events.state_log import StateLog
from reyn.core.plan import PlanRegistry


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> Session:
    return Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "wal.jsonl"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


# ── 1. spawn_plan_task enqueues plan_completed inbox on clean exit ────────


@pytest.mark.asyncio
async def test_plan_completion_emits_plan_completed_inbox(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: FP-0025 C — spawn_plan_task enqueues plan_completed inbox on
    clean exit instead of emitting terminal text directly to outbox.

    Contract: after a PlanRuntime finishes cleanly, the ``plan_completed``
    inbox kind must be present with payload keys ``plan_id``, ``goal``,
    ``step_results``, ``n_steps``, ``chain_id``. The inbox is the trigger
    for router narration synthesis.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    # Stub _run_router_loop so no real LLM call fires (Tier 2 scope).
    router_calls: list[tuple[str, str]] = []

    async def _stub_run_router_loop(self, user_text: str, chain_id: str) -> None:
        router_calls.append((user_text, chain_id))

    monkeypatch.setattr(Session, "_run_router_loop", _stub_run_router_loop)

    # Build a minimal fake runtime that returns a clean PlanExecutionResult.
    fake_result = PlanExecutionResult(
        text="synthesised",
        step_results={"s1": "step one output", "s2": "step two output"},
        step_failures={},
        plan_goal="read and compare two files",
        n_steps=2,
    )

    class _FakeRuntime:
        plan_id = "fp0025c_test"

        async def run(self):
            return fake_result

    # Drain any prior inbox messages.
    while not session.inbox.empty():
        session.inbox.get_nowait()

    plan_id = "fp0025c_test"
    await session._plan_runner.spawn_plan_task(
        plan_id=plan_id,
        runtime=_FakeRuntime(),
        chain_id=f"plan_{plan_id}",
        parent_chain_id="parent_chain_001",
    )

    # Allow the background task to complete.
    await asyncio.gather(*session.running_plans.values(), return_exceptions=True)

    # Collect all inbox items.
    inbox_items: list[tuple[str, dict]] = []
    while not session.inbox.empty():
        inbox_items.append(session.inbox.get_nowait())

    plan_completed_payloads = [
        payload for kind, payload in inbox_items
        if kind == "plan_completed"
    ]
    assert len(plan_completed_payloads) >= 1, (
        f"Expected at least one plan_completed inbox message; "
        f"inbox items: {inbox_items!r}"
    )

    payload = plan_completed_payloads[0]
    assert payload["plan_id"] == plan_id
    assert payload["goal"] == "read and compare two files"
    assert payload["n_steps"] == 2
    assert "s1" in payload["step_results"]
    assert "s2" in payload["step_results"]


# ── 2. _handle_plan_completed injects user-role message ──────────────────


@pytest.mark.asyncio
async def test_handle_plan_completed_injects_user_message(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: FP-0025 C — _handle_plan_completed appends a user-role
    [task_completed] kind=plan message to session.history carrying plan_id and
    step_results text, so the router LLM sees step outputs.

    Observation: read session.history (public attribute) after the handler
    returns; the last user-role message must contain ``[task_completed] kind=plan``
    and the goal text.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    # Stub _run_router_loop to prevent real LLM call.
    async def _noop_router_loop(self, user_text: str, chain_id: str) -> None:
        pass

    monkeypatch.setattr(Session, "_run_router_loop", _noop_router_loop)

    payload = {
        "plan_id": "p_narrate_001",
        "chain_id": "chain_narrate_001",
        "goal": "explain the project structure",
        "step_results": {"read_step": "README has 3 sections"},
        "n_steps": 1,
    }

    history_before = len(session.history)
    await session._handle_plan_completed(payload)

    # A new history entry must have been appended.
    assert len(session.history) > history_before, (
        "Expected at least one new history entry after _handle_plan_completed"
    )

    # The injected message must be user-role and contain [task_completed] kind=plan.
    injected = [
        m for m in session.history[history_before:]
        if m.role == "user" and "[task_completed] kind=plan" in m.text
    ]
    assert len(injected) >= 1, (
        f"Expected a user-role [task_completed] kind=plan history entry; "
        f"new entries: {[m.text[:80] for m in session.history[history_before:]]!r}"
    )

    msg = injected[0]
    assert "explain the project structure" in msg.text
    assert "plan_id=p_narrate_001" in msg.text
    assert msg.meta.get("source") == "plan_completion"
    assert msg.meta.get("plan_id") == "p_narrate_001"


# ── 3. _handle_plan_completed calls _run_router_loop once ────────────────


@pytest.mark.asyncio
async def test_handle_plan_completed_runs_router_turn(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: FP-0025 C — _handle_plan_completed invokes _run_router_loop
    exactly once so the router LLM synthesises step results into a user reply.

    Observation: count calls to the stubbed _run_router_loop; must be == 1
    after one _handle_plan_completed invocation.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    router_call_count = 0

    async def _counting_router_loop(self, user_text: str, chain_id: str) -> None:
        nonlocal router_call_count
        router_call_count += 1

    monkeypatch.setattr(Session, "_run_router_loop", _counting_router_loop)

    payload = {
        "plan_id": "p_synth_002",
        "chain_id": "chain_synth_002",
        "goal": "compare README.md and CLAUDE.md",
        "step_results": {"r1": "readme: 5 sections", "r2": "claude: 8 sections"},
        "n_steps": 2,
    }

    await session._handle_plan_completed(payload)

    assert router_call_count == 1, (
        f"Expected _run_router_loop called once; got {router_call_count}"
    )


# ── 4. FP-0027: _handle_plan_completed injects step_failures when present ───


@pytest.mark.asyncio
async def test_handle_plan_completed_injects_step_failures_when_present(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: FP-0027 — _handle_plan_completed includes step_failures in
    the injected user-role message when the plan had failed steps.

    Contract: when the ``plan_completed`` payload carries a non-empty
    ``step_failures`` dict, the injected history message must contain
    ``step_failures:`` text so the router LLM sees which steps failed
    and can account for gaps in its synthesis.

    When ``step_failures`` is absent or empty, the injected message must
    NOT contain ``step_failures:`` (= no empty boilerplate).
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    async def _noop_router_loop(self, user_text: str, chain_id: str) -> None:
        pass

    monkeypatch.setattr(Session, "_run_router_loop", _noop_router_loop)

    # ── Case A: plan with a failed step ─────────────────────────────────
    payload_with_failures = {
        "plan_id": "p_fail_001",
        "chain_id": "chain_fail_001",
        "goal": "read three files and compare",
        "step_results": {"s1": "file A: 120 lines", "s3": "comparison done"},
        "step_failures": {"s2": "TimeoutError('file B timed out')"},
        "n_steps": 3,
    }

    history_before = len(session.history)
    await session._handle_plan_completed(payload_with_failures)

    injected = [
        m for m in session.history[history_before:]
        if m.role == "user" and "[task_completed] kind=plan" in m.text
    ]
    assert len(injected) >= 1, "Expected a [task_completed] kind=plan history entry"
    msg = injected[0]
    assert "step_failures" in msg.text, (
        f"Expected 'step_failures' in injected message; got: {msg.text[:200]!r}"
    )
    assert "TimeoutError" in msg.text, (
        "Expected the failure text to appear in the injected message"
    )

    # ── Case B: plan with no failures — step_failures must not appear ────
    history_before2 = len(session.history)
    payload_no_failures = {
        "plan_id": "p_ok_002",
        "chain_id": "chain_ok_002",
        "goal": "read and summarise",
        "step_results": {"s1": "file A content"},
        "step_failures": {},
        "n_steps": 1,
    }
    await session._handle_plan_completed(payload_no_failures)

    injected2 = [
        m for m in session.history[history_before2:]
        if m.role == "user" and "[task_completed] kind=plan" in m.text
    ]
    assert len(injected2) >= 1
    msg2 = injected2[0]
    assert "step_failures" not in msg2.text, (
        "step_failures section must not appear when there are no failures"
    )
