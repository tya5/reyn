"""Tier 2: #1200 3-axis sandbox uniformity review-gate (wave closeout, test-only).

The #1200 deliverable is the WIRING — the agent's backend reaches chat's FS seam
(Workspace, F1) + exec seam (OpContext, F2) as ONE instance, and plan inherits it.
This pins the review-gate invariant: chat/plan/phase must share a single agent
sandbox instance; any axis wiring a DIFFERENT backend is a reject.

- **chat**: one injected backend reaches BOTH seams as the same object (F2's
  single-shared invariant; re-asserted here as the uniformity gate).
- **plan**: `_PlanStepHost` owns NO backend — it delegates tool exec to its parent
  (chat), so plan runs on chat's backend (the "1 seam, not 2" runtime basis).
- **phase**: OSRuntime receives the agent's backend on the working path
  (agent.py) — the reference axis, unchanged by #1200.

Per-frontend entry-point activation is deferred-as-tracked (#1289); this verifies
the wired-but-inert capability is load-bearing-correct.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.planner import Plan, PlanStep, _PlanStepHost
from reyn.chat.session import Session
from reyn.core.events.state_log import StateLog
from reyn.environment.host_backend import HostBackend

# ── chat: one agent backend → both seams (the review-gate invariant) ─────────


def test_chat_shares_one_instance_across_fs_and_exec_seams(tmp_path: Path) -> None:
    """Tier 2: one agent backend reaches chat's FS seam (Workspace.backend) AND
    exec seam (OpContext.sandbox_backend) as the SAME object — chat does not
    diverge its two seams (review-gate: differ = reject)."""
    agent_backend = HostBackend()
    session = Session(
        agent_name="u",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        environment_backend=agent_backend,
        sandbox_backend=agent_backend,
    )
    ctx = session._make_router_op_context()
    assert ctx.workspace.backend is agent_backend     # FS seam
    assert ctx.sandbox_backend is agent_backend       # exec seam — same instance


# ── plan: owns no backend, delegates tool exec to chat (inherits its backend) ─


class _RecordingParent:
    """Minimal parent host recording the tool-exec ops _PlanStepHost forwards."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def file_write(self, path: str, content: str) -> dict:
        self.calls.append(("file_write", (path, content)))
        return {"ok": True}

    async def file_read(self, path: str) -> str:
        self.calls.append(("file_read", (path,)))
        return "data"


@pytest.mark.asyncio
async def test_plan_step_delegates_tool_exec_to_parent(tmp_path: Path) -> None:
    """Tier 2: a plan step's file ops delegate to the parent (chat) host — plan
    owns no backend, so it runs on chat's backend. This is the runtime basis for
    "#1200 is 1 seam, not 2": fixing chat auto-covers plan via delegation."""
    parent = _RecordingParent()
    host = _PlanStepHost(
        plan=Plan(goal="g", steps=(PlanStep(id="s1", description="d", tools=()),)),
        step=PlanStep(id="s1", description="d", tools=()),
        prior_results={},
        parent=parent,
    )
    await host.file_write("a.txt", "x")
    await host.file_read("a.txt")
    assert ("file_write", ("a.txt", "x")) in parent.calls
    assert ("file_read", ("a.txt",)) in parent.calls


def test_plan_step_host_owns_no_backend(tmp_path: Path) -> None:
    """Tier 2: _PlanStepHost wires NO backend of its own (no environment_backend /
    sandbox_backend / Workspace attrs) — it cannot diverge from chat's backend
    because it has none (the review-gate, structurally)."""
    host = _PlanStepHost(
        plan=Plan(goal="g", steps=(PlanStep(id="s1", description="d", tools=()),)),
        step=PlanStep(id="s1", description="d", tools=()),
        prior_results={},
        parent=_RecordingParent(),
    )
    assert not hasattr(host, "_environment_backend")
    assert not hasattr(host, "_sandbox_backend")
