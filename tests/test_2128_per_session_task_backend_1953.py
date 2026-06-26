"""Tier 2: #2128 — per-session/multi-AGENT Task-backend restore + prune correctness.

The registry's single ``_task_backend`` pointer was last-wins (one agent's backend), so:
- RESTORE (`_restore_task_active`) reached only the last-constructed agent → a multi-AGENT
  rewind left every OTHER agent's task substrate un-restored.
- PRUNE (`_prune_generations_below`) touched only that one backend → every other agent's
  ``task-generations/`` grew unbounded, INCLUDING agents with NO loaded session.

The fix: RESTORE derives every LOADED agent's rewind backend at use-time
(``_rewind_backends``, one per agent — the tasks.db is agent-keyed/shared); PRUNE is
DISK-driven over ``list_names()`` so it covers UNLOADED agents too (a path-based
``SqliteTaskGenerationStore`` — pure unlink, no live connection touched).

Real AgentRegistry + StateLog + real SqliteTaskBackend / on-disk generation files (no mocks).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.task import Task, TaskState
from reyn.task.factory import create_task_backend


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=lambda p: None, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _active_seq(log: StateLog, agent: str, text: str) -> int:
    return await log.append("inbox_put", target=agent, msg_id=text, msg_kind="user",
                            payload={"text": text})


def _task(tid: str) -> Task:
    return Task(task_id=tid, name=tid, assignee="s", requester="r", status=TaskState.PENDING)


# ── restore: every loaded AGENT's backend (not just the last-constructed) ────────────


@pytest.mark.asyncio
async def test_restore_touches_every_loaded_agents_backend(tmp_path):
    """Tier 2: (LOAD-BEARING) a multi-AGENT rewind restores EVERY loaded agent's task
    backend — each reverts to its captured generation. RED on the single-pointer (only the
    last-constructed agent's backend was restored; the other agent's post-generation task
    would survive)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "agenta")
    _seed_agent(tmp_path, "agentb")
    # agent-keyed paths (distinct parent dirs → distinct task-generations/ dirs), as in
    # production .reyn/agents/<name>/state/tasks.db.
    (tmp_path / "a" / "state").mkdir(parents=True)
    (tmp_path / "b" / "state").mkdir(parents=True)
    back_a = create_task_backend("sqlite", path=str(tmp_path / "a" / "state" / "tasks.db"))
    back_b = create_task_backend("sqlite", path=str(tmp_path / "b" / "state" / "tasks.db"))

    # each agent: a kept task captured in a generation at an ACTIVE seq, then a live task
    # created AFTER the generation (which a restore must revert away).
    seq_a = await _active_seq(reg.state_log, "agenta", "a")
    await back_a.create(_task("keep_a"))
    await back_a.snapshot_generation(seq_a)
    await back_a.create(_task("live_a"))
    seq_b = await _active_seq(reg.state_log, "agentb", "b")
    await back_b.create(_task("keep_b"))
    await back_b.snapshot_generation(seq_b)
    await back_b.create(_task("live_b"))

    reg._sessions["agenta"] = {"main": SimpleNamespace(task_backend=back_a)}
    reg._sessions["agentb"] = {"main": SimpleNamespace(task_backend=back_b)}

    await reg._restore_task_active(at_or_below=max(seq_a, seq_b))

    # BOTH backends restored to their generation → kept tasks present, live tasks reverted
    assert await back_a.get("keep_a") is not None
    assert await back_a.get("live_a") is None
    assert await back_b.get("keep_b") is not None
    assert await back_b.get("live_b") is None  # RED on single-pointer (agentb un-restored)


@pytest.mark.asyncio
async def test_rewind_backends_one_per_loaded_session(tmp_path):
    """Tier 2: #2186 supersedes the #2128 one-per-AGENT model — `_rewind_backends` returns
    one backend per LOADED SESSION. The tasks.db is now per-SESSION (a task lives in its
    assignee session's own ledger), so each session's ledger is its own rewind substrate
    and must be restored independently (per-session FILES dissolve the #2125 lock — no
    shared-file double-swap). De-duplicated by backend IDENTITY: a session that resolves a
    sibling's ledger via the cross-ledger resolver shares the instance → restored once."""
    reg = _make_registry(tmp_path)
    # distinct per-session ledgers (one per session, distinct files)
    back_main = create_task_backend("sqlite", path=str(tmp_path / "main.db"))
    back_s1 = create_task_backend("sqlite", path=str(tmp_path / "s1.db"))
    back_b = create_task_backend("sqlite", path=str(tmp_path / "b.db"))
    reg._sessions["agenta"] = {
        "main": SimpleNamespace(task_backend=back_main),
        "s1": SimpleNamespace(task_backend=back_s1),
    }
    reg._sessions["agentb"] = {"main": SimpleNamespace(task_backend=back_b)}
    backs = reg._rewind_backends()
    # one per loaded SESSION — every distinct per-session ledger is present (RED on the old
    # one-per-agent break, which would drop agenta's second session ledger).
    assert back_main in backs and back_s1 in backs and back_b in backs
    # id-dedup: the SAME instance shared across two sessions (resolver-shared) is restored
    # ONCE (no redundant double file-swap of the one connection).
    reg._sessions["agentc"] = {"main": SimpleNamespace(task_backend=back_b)}
    deduped = reg._rewind_backends()
    assert [b for b in deduped if b is back_b] == [back_b]  # appears exactly once


# ── prune: covers UNLOADED agents (the Q4 unbounded-growth fix) ──────────────────────


@pytest.mark.asyncio
async def test_prune_covers_unloaded_agents_task_generations(tmp_path):
    """Tier 2: (LOAD-BEARING) `_prune_generations_below` prunes the task-generations of
    EVERY on-disk agent — including one with NO loaded session — so an unloaded agent's
    task-gen DBs don't grow unbounded. RED on the single-pointer (it reached at most one
    loaded agent; the unloaded agent's below-floor generation survived)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "loaded")
    _seed_agent(tmp_path, "unloaded")
    # both agents have on-disk task-generations: one below the floor (prune), one at/above (keep)
    for name in ("loaded", "unloaded"):
        gd = tmp_path / ".reyn" / "agents" / name / "state" / "task-generations"
        gd.mkdir(parents=True)
        (gd / "task-gen-1.db").write_bytes(b"x")   # below floor 3 → prune
        (gd / "task-gen-5.db").write_bytes(b"x")   # at/above floor 3 → keep
    # only "loaded" has a live session; "unloaded" exists on disk only
    reg._sessions["loaded"] = {"main": SimpleNamespace(task_backend=None)}

    await reg._prune_generations_below(3)

    for name in ("loaded", "unloaded"):
        gd = tmp_path / ".reyn" / "agents" / name / "state" / "task-generations"
        assert not (gd / "task-gen-1.db").exists(), f"{name}: below-floor gen not pruned"
        assert (gd / "task-gen-5.db").exists(), f"{name}: at-floor gen wrongly pruned"


@pytest.mark.asyncio
async def test_prune_skips_agents_without_a_task_generations_dir(tmp_path):
    """Tier 2: (the is_dir() guard) an agent with NO task-generations dir (non-rewind / never
    snapshotted) is a no-op — prune must NOT create an empty task-generations/ for it (the
    store ctor mkdir's the dir, so the guard is mandatory)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "bare")
    await reg._prune_generations_below(3)
    assert not (tmp_path / ".reyn" / "agents" / "bare" / "state" / "task-generations").exists()
