"""Tier 2: #2187 S1 — the Task backend is GLOBAL (one per process).

S1 reverts the #2180 per-AGENT / #2186 per-SESSION backend splits: a task is first-class
(task ⊥ agent/session), so its store is a single global ``.reyn/state/tasks.db`` — ONE
registry-owned instance / ONE connection. Supersedes test_2180 (per-agent connection model).

The Task backend is the EXTERNAL MASTER of task-state (#2187) and is NOT rewound by
time-travel — Reyn rewinds only its internal trajectory (runtime + workspace substrates).

Real AgentRegistry + StateLog + real SqliteTaskBackend (no mocks).
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=lambda _p: None, state_log=state_log,
    )


def test_global_backend_is_one_shared_instance(tmp_path):
    """Tier 2: (THE HEADLINE) ``registry.task_backend`` is ONE global instance — repeated
    access returns the same object, and there is no per-agent keying (the whole concept of a
    different backend for a different agent is gone). The db is the global
    ``.reyn/state/tasks.db`` (the same path the A2A/web server uses). RED on the #2180
    per-agent model (which keyed a distinct backend per agent name)."""
    reg = _make_registry(tmp_path)
    assert reg.task_backend is reg.task_backend                       # one shared instance
    assert (tmp_path / ".reyn" / "state" / "tasks.db").is_file()      # global path
    assert not hasattr(reg, "task_backend_for")                       # per-agent seam removed
