"""Tier 2: #2187 S1 — the Task backend is GLOBAL (one per process).

S1 reverts the #2180 per-AGENT / #2186 per-SESSION backend splits: a task is first-class
(task ⊥ agent/session), so its store is a single global ``.reyn/state/tasks.db`` — ONE
registry-owned instance / ONE connection. The #2128 rewind/prune MECHANISM
(``snapshot_generation`` / ``restore_to_seq`` / disk prune) is unchanged — only single-ized
(per-agent → global). Supersedes test_2180 (per-agent connection model) + test_2128
(per-agent rewind/prune multiplicity), both deleted.

Real AgentRegistry + StateLog + real SqliteTaskBackend / on-disk generation files (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.task import Task, TaskState


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=lambda _p: None, state_log=state_log,
    )


async def _active_seq(log: StateLog, agent: str, text: str) -> int:
    return await log.append("inbox_put", target=agent, msg_id=text, msg_kind="user",
                            payload={"text": text})


def _task(tid: str) -> Task:
    return Task(task_id=tid, name=tid, assignee="s", requester="r", status=TaskState.PENDING)


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


@pytest.mark.asyncio
async def test_restore_touches_the_global_backend(tmp_path):
    """Tier 2: (the #2128 mechanism, single-ized) a rewind restores THE global backend to
    its captured generation — a task captured in a generation survives, a task created AFTER
    it is reverted. RED if _restore_task_active no longer reaches the (now global) backend."""
    reg = _make_registry(tmp_path)
    backend = reg.task_backend
    seq = await _active_seq(reg.state_log, "a", "x")
    await backend.create(_task("keep"))
    await backend.snapshot_generation(seq)
    await backend.create(_task("live"))                              # AFTER the generation

    await reg._restore_task_active(at_or_below=seq)

    assert await backend.get("keep") is not None                     # captured task survives
    assert await backend.get("live") is None                         # post-generation reverted


@pytest.mark.asyncio
async def test_rewind_backends_is_the_single_global_backend(tmp_path):
    """Tier 2: ``_rewind_backends`` is the ONE global backend (built + rewind-participating),
    not a per-agent list. Empty before any task activity (uses the field, not the lazy
    property, so it never builds an empty db just to check). RED on the #2128 per-agent
    derivation."""
    reg = _make_registry(tmp_path)
    before = reg._rewind_backends()
    assert not before                                                # nothing built yet → empty
    backend = reg.task_backend                                       # build it
    after = reg._rewind_backends()
    assert backend in after and all(b is backend for b in after)     # exactly the one global


@pytest.mark.asyncio
async def test_prune_covers_the_global_task_generations(tmp_path):
    """Tier 2: (the #2128 prune, single-ized) ``_prune_generations_below`` prunes the GLOBAL
    task-generations dir (``.reyn/state/task-generations``) — below-floor generations dropped,
    at/above-floor kept. RED on the per-agent prune (which iterated per-agent dirs that no
    longer hold the task substrate)."""
    reg = _make_registry(tmp_path)
    gd = tmp_path / ".reyn" / "state" / "task-generations"
    gd.mkdir(parents=True)
    (gd / "task-gen-1.db").write_bytes(b"x")                         # below floor 3 → prune
    (gd / "task-gen-5.db").write_bytes(b"x")                         # at/above floor 3 → keep

    await reg._prune_generations_below(3)

    assert not (gd / "task-gen-1.db").exists()                       # below-floor pruned
    assert (gd / "task-gen-5.db").exists()                           # at/above-floor kept
