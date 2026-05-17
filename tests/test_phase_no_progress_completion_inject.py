"""Tier 2: regression guard for B33 W6 NEW-1.

Pins the invariant that _run_one_skill enqueues ``skill_completed`` to the
session inbox on ALL non-cancelled terminal paths — including the
phase_no_progress abort path (WorkflowAbortedError) that B33 W6 observed
skipping ``skill_completion_injected``.

Root cause (B33 W6 analysis):
    src/reyn/chat/services/skill_runner.py — the ``except Exception`` block
    for the agent.run() call path placed ``_enqueue_skill_completed`` AFTER
    ``await _put_outbox(...)``. If ``_put_outbox`` raised (e.g. closed session,
    outbox queue issue), the enqueue was silently skipped.

Fix (same file): use try/except/else/finally so ``_enqueue_skill_completed``
is always called when ``_terminal_status`` is set — regardless of which
intermediate await failed inside the exception handler.

Tests:
- test_skill_completed_enqueued_on_workflow_aborted_phase_no_progress:
    Drives ``_run_one_skill`` with an agent that raises WorkflowAbortedError
    (the exception type produced by phase_no_progress and LLM-initiated
    aborts alike). Asserts ``skill_completed`` is in the inbox with
    ``status="error"`` and that the payload shape is complete.

- test_skill_completion_injected_fires_on_workflow_aborted:
    Full round-trip: _run_one_skill + session inbox → _handle_skill_completed
    → ``skill_completion_injected`` event. Mirrors the s-fp12-completion-2
    assertion that WAS working, and s-fp11-1 which was NOT.

- test_skill_completed_enqueued_even_when_put_outbox_raises:
    Exercises the "put_outbox raises mid-handler" scenario that was the
    underlying silent failure mode in the pre-fix code.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.session import ChatSession
from reyn.config import SafetyConfig, TimeoutConfig
from reyn.events.state_log import StateLog
from reyn.kernel.runtime_types import WorkflowAbortedError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(tmp_path: Path, *, agent_name: str = "test_agent") -> ChatSession:
    safety = SafetyConfig(timeout=TimeoutConfig(chain_seconds=60.0))
    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        safety=safety,
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


class _AbortingAgent:
    """Fake agent that raises WorkflowAbortedError — simulating phase_no_progress."""

    def __init__(self, reason: str = "phase_no_progress: identical output") -> None:
        self._reason = reason

    async def run(self, skill, input_artifact, **kwargs):  # noqa: ANN001
        raise WorkflowAbortedError(self._reason)


class _FakeAgent:
    """Fake agent that returns a scripted RunResult without LLM."""

    def __init__(self, run_result) -> None:  # noqa: ANN001
        self._run_result = run_result

    async def run(self, skill, input_artifact, **kwargs):  # noqa: ANN001
        return self._run_result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_completed_enqueued_on_workflow_aborted_phase_no_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: _run_one_skill enqueues skill_completed inbox on WorkflowAbortedError.

    B33 W6 NEW-1 regression guard: the phase_no_progress abort path
    (WorkflowAbortedError) MUST produce a ``skill_completed`` inbox entry so
    that ``_handle_skill_completed`` → ``skill_completion_injected`` can fire.

    Pre-fix, the ``except Exception`` handler called ``_put_outbox`` before
    ``_enqueue_skill_completed``; if ``_put_outbox`` raised the enqueue was
    silently skipped. The fix uses try/finally to guarantee the enqueue on all
    non-cancelled terminal paths regardless of intermediate failures.

    Observation: read session.inbox directly after awaiting _run_one_skill —
    the message must be present with status="error" and the WorkflowAbortedError
    message in data.error.
    """
    monkeypatch.chdir(tmp_path)

    import reyn.chat.services.skill_runner as skill_runner_mod

    dummy_skill_dir = tmp_path / "dummy_skill"
    dummy_skill_dir.mkdir()

    monkeypatch.setattr(
        skill_runner_mod, "resolve_skill_path",
        lambda name: (dummy_skill_dir, tmp_path),
    )
    monkeypatch.setattr(
        skill_runner_mod, "load_dsl_skill",
        lambda path, *, skill_root: object(),
    )

    session = _make_session(tmp_path)
    session.is_attached = True

    abort_reason = "phase_no_progress: Phase 'build_skill' produced identical output"
    session._skill_runner._build_agent_fn = (
        lambda run_id, skill_name, **kw: _AbortingAgent(abort_reason)
    )

    run_id = "20260517T000000Z_skill_builder_aabb"
    chain_id = "chain-b33-w6-001"

    # Drain prior inbox messages so we see only what _run_one_skill enqueues.
    while not session.inbox.empty():
        session.inbox.get_nowait()

    await session._skill_runner._run_one_skill(
        run_id, "skill_builder",
        {"type": "user_message", "data": {"text": "build invalid cyclic skill"}},
        chain_id=chain_id,
    )

    # Inbox MUST have a skill_completed entry (not empty).
    assert not session.inbox.empty(), (
        "B33 W6 NEW-1: inbox must contain skill_completed after WorkflowAbortedError; "
        "was empty — skill_completion_injected would never fire"
    )
    kind, payload = await asyncio.wait_for(session.inbox.get(), timeout=1.0)

    assert kind == "skill_completed", (
        f"expected kind='skill_completed', got {kind!r}"
    )
    assert payload["run_id"] == run_id, (
        f"run_id mismatch: {payload['run_id']!r}"
    )
    assert payload["skill"] == "skill_builder"
    assert payload["status"] == "error", (
        f"WorkflowAbortedError must produce status='error', got {payload['status']!r}"
    )
    assert payload["chain_id"] == chain_id
    assert abort_reason in payload["data"].get("error", ""), (
        f"abort reason not in data.error: {payload['data']}"
    )


@pytest.mark.asyncio
async def test_skill_completion_injected_fires_on_workflow_aborted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: skill_completion_injected event fires after WorkflowAbortedError abort.

    Full round-trip: _run_one_skill raises WorkflowAbortedError →
    skill_completed inbox enqueued → _handle_skill_completed consumes it →
    skill_completion_injected event emitted on the session's chat event log.

    This covers the B33 W6 NEW-1 end-to-end path: both the
    phase_no_progress abort and the LLM-initiated abort at plan_skill must
    produce the injection event so the router can narrate the failure.
    """
    monkeypatch.chdir(tmp_path)

    import reyn.chat.services.skill_runner as skill_runner_mod

    dummy_skill_dir = tmp_path / "dummy_skill"
    dummy_skill_dir.mkdir()

    monkeypatch.setattr(
        skill_runner_mod, "resolve_skill_path",
        lambda name: (dummy_skill_dir, tmp_path),
    )
    monkeypatch.setattr(
        skill_runner_mod, "load_dsl_skill",
        lambda path, *, skill_root: object(),
    )

    session = _make_session(tmp_path)
    session.is_attached = True

    abort_reason = "phase_no_progress: Phase 'plan_skill' identical to rejected"
    session._skill_runner._build_agent_fn = (
        lambda run_id, skill_name, **kw: _AbortingAgent(abort_reason)
    )

    run_id = "20260517T000000Z_skill_builder_ccdd"
    chain_id = "chain-b33-w6-002"

    while not session.inbox.empty():
        session.inbox.get_nowait()

    # Step 1: run the skill (raises WorkflowAbortedError → enqueues skill_completed).
    await session._skill_runner._run_one_skill(
        run_id, "skill_builder",
        {"type": "user_message", "data": {"text": "cyclic graph a→b→a"}},
        chain_id=chain_id,
    )

    # Step 2: consume the inbox entry via _handle_skill_completed.
    # We call it directly since we have the payload from the inbox.
    assert not session.inbox.empty(), (
        "inbox must have skill_completed after abort for _handle_skill_completed to run"
    )
    kind, payload = await asyncio.wait_for(session.inbox.get(), timeout=1.0)
    assert kind == "skill_completed"

    # Step 3: call _handle_skill_completed with a stubbed router so the LLM
    # call doesn't fire. We only care that skill_completion_injected is emitted.
    async def _noop_router_loop(injected_text: str, chain_id: str) -> None:  # noqa: ARG001
        pass

    original_router = session._run_router_loop
    session._run_router_loop = _noop_router_loop  # type: ignore[method-assign]
    try:
        await session._handle_skill_completed(payload)
    finally:
        session._run_router_loop = original_router  # type: ignore[method-assign]

    # skill_completion_injected MUST appear in the session's chat events.
    injected_events = [
        e for e in session._chat_events.all()
        if e.type == "skill_completion_injected"
    ]
    assert injected_events, (
        "B33 W6 NEW-1: skill_completion_injected must fire after WorkflowAbortedError "
        "abort; got no such event — user narration is silently skipped"
    )
    ev = injected_events[0]
    assert ev.data["run_id"] == run_id
    assert ev.data["skill"] == "skill_builder"
    assert ev.data["status"] == "error"


@pytest.mark.asyncio
async def test_skill_completed_enqueued_even_when_put_outbox_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: skill_completed inbox enqueued even when _put_outbox raises mid-handler.

    Pre-fix code placed _enqueue_skill_completed AFTER await _put_outbox inside
    the except Exception block. If _put_outbox raised, the enqueue was silently
    skipped. This test exercises that exact failure scenario and asserts the
    post-fix guarantee: the enqueue fires regardless.

    Mechanism: replace _put_outbox with a coroutine that raises RuntimeError.
    Assert that skill_completed still lands in session.inbox.
    """
    monkeypatch.chdir(tmp_path)

    import reyn.chat.services.skill_runner as skill_runner_mod

    dummy_skill_dir = tmp_path / "dummy_skill"
    dummy_skill_dir.mkdir()

    monkeypatch.setattr(
        skill_runner_mod, "resolve_skill_path",
        lambda name: (dummy_skill_dir, tmp_path),
    )
    monkeypatch.setattr(
        skill_runner_mod, "load_dsl_skill",
        lambda path, *, skill_root: object(),
    )

    session = _make_session(tmp_path)
    session.is_attached = True

    abort_reason = "phase_no_progress: no-progress loop detected"
    session._skill_runner._build_agent_fn = (
        lambda run_id, skill_name, **kw: _AbortingAgent(abort_reason)
    )

    # Replace _put_outbox with a version that raises to simulate a closed
    # or unavailable outbox. The pre-fix code would silently drop the
    # skill_completed enqueue; the post-fix finally block must still fire.
    async def _raising_put_outbox(msg) -> None:  # noqa: ANN001, ARG001
        raise RuntimeError("outbox closed during shutdown")

    session._skill_runner._put_outbox = _raising_put_outbox  # type: ignore[assignment]

    run_id = "20260517T000000Z_skill_builder_eeff"
    chain_id = "chain-b33-w6-003"

    while not session.inbox.empty():
        session.inbox.get_nowait()

    await session._skill_runner._run_one_skill(
        run_id, "skill_builder",
        {"type": "user_message", "data": {"text": "build cyclic skill"}},
        chain_id=chain_id,
    )

    # Even with a broken _put_outbox, skill_completed MUST be in the inbox.
    assert not session.inbox.empty(), (
        "skill_completed must be enqueued even when _put_outbox raises; "
        "pre-fix code silently dropped the enqueue in this scenario"
    )
    kind, payload = await asyncio.wait_for(session.inbox.get(), timeout=1.0)
    assert kind == "skill_completed"
    assert payload["status"] == "error"
    assert payload["run_id"] == run_id
