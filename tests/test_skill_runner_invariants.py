"""Tier 2: OS invariant tests for SkillRunner (FP-0019 Wave 1b).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock usage.  Real EventLog, real asyncio.Queue, real SkillRunner.
- No private-state assertions beyond the public ``running_names()`` surface.
- Event observation flows through ``events.all()`` (EventLog public read accessor).
- Each test docstring's first line starts with ``Tier 2: ...``.

Spawn path note (#2104 PR2): ``spawn()`` was removed when chat-mode skill
dispatch became synchronous via ``run_skill_awaitable``.  The async background
path is now ``spawn_resumed_skill`` (auto-resume).  Tests 1, 2, 4 exercise
``spawn_resumed_skill``; test 3 exercises ``run_skill_awaitable`` for the P6
event emission invariant.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.runtime.forwarder import ChatEventForwarder
from reyn.skill.skill_runner import SkillRunner

# ---------------------------------------------------------------------------
# Helpers — minimal fakes and SkillRunner factory
# ---------------------------------------------------------------------------


class _FakeRunResult:
    """Minimal RunResult substitute."""
    status: str = "finished"
    data: dict | None = None
    error: str | None = None
    ok: bool = True

    def __init__(self, status: str = "finished", data: dict | None = None):
        self.status = status
        self.data = data or {}


class _FakeBudgetCheck:
    allowed: bool = True
    hard_dimension: str | None = None
    detail: str | None = None
    context: dict
    warn_dimensions: list

    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.hard_dimension = None
        self.detail = None
        self.context = {}
        self.warn_dimensions = []


class _FakeBudget:
    """Minimal BudgetGateway stand-in that always allows spawns."""

    def check_pre_spawn(self, *, chain_id: str, skill: str) -> _FakeBudgetCheck:
        return _FakeBudgetCheck(allowed=True)

    def record_spawn(self, *, chain_id: str, skill: str) -> None:
        pass

    def extend_chain_calls(self, *, chain_id: str, skill: str, additional: int) -> int:
        return additional


class _FakeAgent:
    """Minimal Agent that returns a scripted result without LLM."""

    def __init__(self, result: _FakeRunResult, *, block_on: asyncio.Event | None = None):
        self._result = result
        self._block_on = block_on

    async def run(self, skill: Any, input_artifact: dict, **kwargs) -> _FakeRunResult:
        if self._block_on is not None:
            await self._block_on.wait()
        return self._result


class _FakePlan:
    """Duck-typed ResumePlan with the three fields spawn_resumed_skill uses."""

    def __init__(self, skill_name: str, run_id: str, skill_input: dict) -> None:
        self.skill_name = skill_name
        self.run_id = run_id
        self.skill_input = skill_input


class _FakeDecision:
    """Duck-typed ResumeDecision with the .plan attribute spawn_resumed_skill accesses."""

    def __init__(self, skill_name: str, run_id: str, skill_input: dict | None = None) -> None:
        self.plan = _FakePlan(skill_name, run_id, skill_input or {})


def _make_runner(
    *,
    result: _FakeRunResult | None = None,
    block_on: asyncio.Event | None = None,
    allowed_skills: list[str] | None = None,
) -> tuple[SkillRunner, EventLog, asyncio.Queue]:
    """Return a (SkillRunner, EventLog, outbox_queue) 3-tuple."""
    events = EventLog()
    outbox: asyncio.Queue = asyncio.Queue()

    _result = result or _FakeRunResult()

    def _build_agent(run_id, skill_name, *, subscribers=None) -> _FakeAgent:
        return _FakeAgent(_result, block_on=block_on)

    async def _put_outbox(msg) -> None:
        await outbox.put(msg)

    def _accumulate(result) -> None:
        pass

    def _drop_interventions(run_id) -> None:
        pass

    def _get_skill_registry():
        return None

    async def _ask_budget_extension(**kwargs) -> bool:
        return False

    runner = SkillRunner(
        event_log=events,
        agent_name="test_agent",
        output_language=None,
        mcp_servers=None,
        allowed_skills=allowed_skills,
        budget=_FakeBudget(),
        state_log=None,
        build_agent_fn=_build_agent,
        put_outbox=_put_outbox,
        accumulate=_accumulate,
        drop_interventions_for_run=_drop_interventions,
        get_skill_registry=_get_skill_registry,
        ask_budget_extension=_ask_budget_extension,
        make_subscribers=lambda skill_name, run_id=None: [
            ChatEventForwarder(skill_name, outbox, run_id=run_id),
        ],
        format_refusal=lambda check: "refused",
        format_warn=lambda dim, ctx: "warn",
    )
    return runner, events, outbox


# ---------------------------------------------------------------------------
# Invariant 1: spawn_resumed_skill adds run_id to running_names; cleanup removes it
# ---------------------------------------------------------------------------


def test_dispatch_spawns_task_in_running_dict(tmp_path, monkeypatch):
    """Tier 2: spawn_resumed_skill() registers the task in running_names() before
    the coroutine completes, and removes it via the done-callback after finish.

    Verified by:
      1. Calling spawn_resumed_skill() with a blocking agent (block_on event not set).
      2. Asserting running_names() contains the new run_id.
      3. Releasing the block and waiting for the task.
      4. Asserting running_names() is empty.

    No assertions on private dicts — observation via public running_names().
    """
    # Patch resolve_skill_path and load_dsl_skill in skill_runner module so
    # the runner doesn't try to load a real skill file.
    import reyn.skill.skill_runner as sr_mod

    dummy_dir = tmp_path / "fake_skill"
    dummy_dir.mkdir()

    monkeypatch.setattr(sr_mod, "resolve_skill_path", lambda name: (dummy_dir, tmp_path))
    monkeypatch.setattr(sr_mod, "load_dsl_skill", lambda path, *, skill_root: object())

    block = asyncio.Event()
    runner, events, outbox = _make_runner(block_on=block)

    async def _run():
        # Nothing running yet.
        assert runner.running_names() == []

        # Spawn a skill — the blocking agent holds the task open.
        decision = _FakeDecision("fake", "fake_run_0001")
        await runner.spawn_resumed_skill(decision)

        # Task is now registered.
        names = runner.running_names()
        (_, ) = names  # exactly one task registered

        # Release the block so the task can complete.
        block.set()

        # Give the event loop a chance to run the done-callback.
        for _ in range(5):
            await asyncio.sleep(0)

        # Task must be cleaned up.
        assert runner.running_names() == [], (
            f"Expected running_names() empty after finish, got {runner.running_names()}"
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Invariant 2: cancel_all() during shutdown is graceful (no unhandled raise)
# ---------------------------------------------------------------------------


def test_cancel_all_during_shutdown_graceful(tmp_path, monkeypatch):
    """Tier 2: cancel_all() cancels in-flight tasks, suppresses
    CancelledError, and leaves no unhandled exception.

    Verified by spawning a blocking task, calling cancel_all(), and
    asserting that: (a) cancel_all() completes without raising, and
    (b) running_names() is empty after cancel_all() returns.

    The done-callback (_drop_interventions_for_run) is exercised
    implicitly — if it raised, the gather would surface it.
    """
    import reyn.skill.skill_runner as sr_mod

    dummy_dir = tmp_path / "fake_skill"
    dummy_dir.mkdir()

    monkeypatch.setattr(sr_mod, "resolve_skill_path", lambda name: (dummy_dir, tmp_path))
    monkeypatch.setattr(sr_mod, "load_dsl_skill", lambda path, *, skill_root: object())

    block = asyncio.Event()  # never set — tasks block forever until cancelled
    runner, events, outbox = _make_runner(block_on=block)

    async def _run():
        decision = _FakeDecision("fake", "fake_run_0002")
        await runner.spawn_resumed_skill(decision)
        assert len(runner.running_names()) == 1

        # cancel_all must not raise.
        await runner.cancel_all()

        # All tasks cleaned up.
        assert runner.running_names() == [], (
            f"running_names() must be empty after cancel_all, "
            f"got {runner.running_names()}"
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Invariant 3: run_skill_awaitable() emits skill_run_spawned event (P6 audit)
# ---------------------------------------------------------------------------


def test_dispatch_emits_skill_run_spawned_event(tmp_path, monkeypatch):
    """Tier 2: run_skill_awaitable() must emit ``skill_run_spawned`` via the
    injected event_log before the skill starts, regardless of whether the
    skill eventually succeeds or fails.

    Verified by: running a skill that completes immediately (no block),
    then asserting at least one ``skill_run_spawned`` event was emitted
    with the expected ``skill`` field.

    P6 invariant: every state change must produce an event. The spawn
    is the state change; the event must precede completion.

    Note: the live dispatch path is ``run_skill_awaitable`` (#2104 PR2
    made skill dispatch synchronous, removing the background ``spawn()``).
    """
    import reyn.skill.skill_runner as sr_mod

    dummy_dir = tmp_path / "fake_skill"
    dummy_dir.mkdir()

    monkeypatch.setattr(sr_mod, "resolve_skill_path", lambda name: (dummy_dir, tmp_path))
    monkeypatch.setattr(sr_mod, "load_dsl_skill", lambda path, *, skill_root: object())

    runner, events, outbox = _make_runner()

    async def _run():
        await runner.run_skill_awaitable({"skill": "my_skill", "input": {}}, chain_id="c1")

    asyncio.run(_run())

    emitted = events.all()
    spawned = [e for e in emitted if e.type == "skill_run_spawned"]
    assert len(spawned) >= 1, (
        f"Expected at least 1 skill_run_spawned event, got {len(spawned)}"
    )
    assert spawned[0].data.get("skill") == "my_skill", (
        f"skill_run_spawned.data['skill'] must be 'my_skill', "
        f"got {spawned[0].data.get('skill')!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 4: wait_for_completion() lets tasks finish naturally
# (B27-H4 fix: the grace window allows natural task completion before cancel)
# ---------------------------------------------------------------------------


def test_wait_for_completion_drains_running_skills(tmp_path, monkeypatch):
    """Tier 2: wait_for_completion() waits for in-flight tasks to finish
    naturally so the done-callback cleans up running_names().

    B27-H4 root cause: ``_drain_on_shutdown`` used to call ``cancel_all()``
    immediately; ``wait_for_completion`` provides a grace window so tasks
    finish naturally before the hard cancel.

    Verified by:
      1. Spawning a skill backed by a blocking fake agent (block_on set).
      2. Releasing the block *concurrently* (simulating an LLM call that
         completes during the grace window).
      3. Calling ``wait_for_completion(timeout_sec=5.0)``.
      4. Asserting ``running_names()`` is empty after the wait.
    """
    import reyn.skill.skill_runner as sr_mod

    dummy_dir = tmp_path / "fake_skill"
    dummy_dir.mkdir()

    monkeypatch.setattr(sr_mod, "resolve_skill_path", lambda name: (dummy_dir, tmp_path))
    monkeypatch.setattr(sr_mod, "load_dsl_skill", lambda path, *, skill_root: object())

    block = asyncio.Event()
    runner, events, outbox = _make_runner(block_on=block)

    async def _run():
        # Spawn a skill whose agent blocks until the event fires.
        decision = _FakeDecision("test_skill", "fake_run_0003")
        await runner.spawn_resumed_skill(decision)
        assert len(runner.running_names()) == 1, "Task must be registered"

        # Release the block shortly after, simulating the LLM call finishing
        # during the shutdown grace window.
        async def _release_after_tick():
            await asyncio.sleep(0)  # yield so the skill task starts running
            block.set()

        asyncio.create_task(_release_after_tick())

        # wait_for_completion gives the task the chance to complete naturally.
        await runner.wait_for_completion(timeout_sec=5.0)

    asyncio.run(_run())

    # After wait_for_completion the task is done; cleanup callback fired.
    assert runner.running_names() == [], (
        f"running_names() must be empty after wait_for_completion, "
        f"got {runner.running_names()}"
    )
