"""Tier 2: OS invariant — force-close handoff persist (#1092 PR-D1, additive).

D1 is the ADDITIVE first half of the handoff: when a phase force-closes, the
consolidation is persisted as a checkpoint artifact (P5) + a P6 event is emitted,
WHILE the C2 FD2-from-partial outcome is kept as the fallback (the checkpoint
stays unconsumed until PR-D2 wires re-entry → no broken intermediate). Pins:

- ``persist_force_close_checkpoint`` writes a ``force_close_checkpoint`` artifact
  carrying the consolidation + emits ``phase_force_close_checkpoint_persisted``;
- it is a no-op (returns None, no event) when the phase did not force-close;
- ``RouterLoop.run_loop`` hands the force-close consolidation to the host via
  ``record_force_close`` (the detection signal), getattr-guarded;
- ``PhaseRouterLoopHost`` stores it on ``forced_close_result``.

No mocks: a real Workspace + EventLog + RouterLoop + a real capturing host.
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.chat.router_loop import RouterLoop
from reyn.core.events.events import EventLog
from reyn.core.kernel.phase_executor import persist_force_close_checkpoint
from reyn.data.workspace.workspace import Workspace
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from tests.test_router_loop import FakeRouterHost


def _finish(text: str = "consolidated handoff") -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text, tool_calls=[], finish_reason="stop",
        usage=TokenUsage(prompt_tokens=20, completion_tokens=5),
    )


def _event_types(events: EventLog) -> list[str]:
    return [e.type for e in events.all()]


# ── persist_force_close_checkpoint ───────────────────────────────────────────


def test_persist_writes_checkpoint_and_emits_event(tmp_path) -> None:
    """Tier 2: a force-close consolidation is persisted as a checkpoint artifact
    (P5, path returned) and a P6 event is emitted carrying the path."""
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    path = persist_force_close_checkpoint(
        workspace=ws, events=events, phase="draft", skill_name="s",
        run_id="run-1", forced_close_result=_finish("HANDOFF"),
    )
    assert isinstance(path, str) and path  # a stored-artifact handle
    persisted = [
        e for e in events.all()
        if e.type == "phase_force_close_checkpoint_persisted"
    ]
    assert persisted and persisted[0].data["checkpoint_path"] == path
    assert persisted[0].data["phase"] == "draft"


def test_persist_is_noop_when_not_force_closed(tmp_path) -> None:
    """Tier 2: additive safety — no force-close (forced_close_result None) →
    returns None and emits NO checkpoint event."""
    events = EventLog()
    ws = Workspace(events=events, base_dir=tmp_path)
    path = persist_force_close_checkpoint(
        workspace=ws, events=events, phase="draft", skill_name="s",
        run_id="run-1", forced_close_result=None,
    )
    assert path is None
    assert "phase_force_close_checkpoint_persisted" not in _event_types(events)


# ── detection signal: run_loop → host.record_force_close ──────────────────────


class _ForceCloseRecordingHost(FakeRouterHost):
    """FakeRouterHost that force-closes (should_force_close=True) and records the
    consolidation (record_force_close → forced_close_result) — the phase-host
    detection contract."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.forced_close_result: Any = None

    async def should_force_close(self, messages: list[dict], *, model: str) -> bool:
        return True

    def record_force_close(self, result: Any) -> None:
        self.forced_close_result = result


class _CapturingFinishLLM:
    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        return _finish("RECORDED")


@pytest.mark.asyncio
async def test_run_loop_records_force_close_consolidation_to_host() -> None:
    """Tier 2: when run_loop force-closes, it hands the consolidation finish to
    the host via record_force_close (the D1 detection signal)."""
    host = _ForceCloseRecordingHost()
    loop = RouterLoop(host=host, chain_id="d1", max_iterations=3,
                      llm_caller=_CapturingFinishLLM())
    await loop.run("do the task", [])
    assert host.forced_close_result is not None
    assert host.forced_close_result.content == "RECORDED"


@pytest.mark.asyncio
async def test_run_loop_no_record_hook_is_noop() -> None:
    """Tier 2: a host WITHOUT record_force_close (= chat) is unaffected — the
    getattr-guarded hook is a no-op (no error), leaving the path unchanged."""
    class _NoRecordHost(FakeRouterHost):
        async def should_force_close(self, messages: list[dict], *, model: str) -> bool:
            return True

    loop = RouterLoop(host=_NoRecordHost(), chain_id="d1", max_iterations=3,
                      llm_caller=_CapturingFinishLLM())
    await loop.run("do the task", [])  # must not raise


def test_phase_host_record_force_close_stores_result() -> None:
    """Tier 2: PhaseRouterLoopHost.record_force_close stores the consolidation on
    forced_close_result (initially None)."""
    from reyn.core.kernel.phase_router_host import PhaseRouterLoopHost
    from tests.test_router_loop import FakeEventLog

    host = PhaseRouterLoopHost(
        control_ir_executor=None, events=FakeEventLog(), phase="p", decl=None,
        allowed_ops=None, default_sandbox_policy=None, agent_name="a",
        agent_role="r", output_language="en", resolve_model_fn=lambda n: n,
    )
    assert host.forced_close_result is None
    r = _finish("STORED")
    host.record_force_close(r)
    assert host.forced_close_result is r
