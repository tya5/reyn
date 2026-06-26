"""Tier 2: #2180 — one SHARED Task backend (one connection) per agent.

#2128 made the ``tasks.db`` agent-keyed but each SESSION still constructed its OWN
``SqliteTaskBackend`` over the shared file → N connections, N independent
``asyncio.Lock``s that don't serialise across connections (the #2125 "database is
locked" source) + a ``restore_to_seq`` file-swap that strands sibling connections.

#2180 (A): the agent's backend is registry-OWNED, ONE per agent, built lazily + cached,
handed to every session of the agent via the SINGLE construction seam
``AgentRegistry.task_backend_for``. One instance ⟹ one connection ⟹ the per-instance lock
serialises every write (restores the ``busy_timeout=0`` premise) and ``restore_to_seq`` is
the only connection touching the file. Closed + evicted on agent teardown (purge /
rewind-drop), NOT on last-session-drop (it's agent-shared).

Falsification (real ``AgentRegistry`` + real ``SqliteTaskBackend``, no mocks):
- the N-connection write-race + restore file-swap are REAL and DETERMINISTIC (a sibling
  connection makes a write / a restore raise "database is locked");
- (A) removes both by construction: one instance per agent (``is`` identity), restore
  through it is clean, the agent-shared task visibility is preserved.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from reyn.runtime.registry import AgentRegistry
from reyn.task import SqliteTaskBackend, Task


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _registry(tmp_path: Path) -> AgentRegistry:
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=None,
    )


def _task(tid: str) -> Task:
    return Task(task_id=tid, name=tid, assignee="x", requester="y")


# ── (A) the single-seam invariant: one instance per agent ───────────────────


def test_one_shared_backend_instance_per_agent(tmp_path) -> None:
    """Tier 2: (LOAD-BEARING) ``task_backend_for`` returns the SAME instance for repeated
    calls on one agent — so every session of the agent shares ONE connection (the
    single-connection-by-construction invariant that removes the #2125 cross-connection
    race). RED if the accessor built a fresh backend per call (the old per-session model
    that opened N connections to the shared file)."""
    reg = _registry(tmp_path)
    first = reg.task_backend_for("worker")
    second = reg.task_backend_for("worker")
    assert first is second


def test_distinct_agents_get_distinct_backends(tmp_path) -> None:
    """Tier 2: the backend is per-AGENT keyed (like ``_sessions``), NOT a process
    singleton — two agents get two distinct instances (their ``tasks.db`` files differ)."""
    reg = _registry(tmp_path)
    assert reg.task_backend_for("a") is not reg.task_backend_for("b")


@pytest.mark.asyncio
async def test_same_agent_task_visibility_is_shared(tmp_path) -> None:
    """Tier 2: (no-regression) the agent-shared task-visibility semantic #2128 established
    is preserved — a task written through the agent's backend is visible through that same
    shared instance (= what EVERY session of the agent sees, since they all resolve to it),
    and it lands at the agent-keyed path. RED if (A) had silently isolated per-session
    state."""
    reg = _registry(tmp_path)
    backend = reg.task_backend_for("worker")
    await backend.create(_task("t1"))
    # every session of "worker" resolves to this same instance → all see t1.
    assert reg.task_backend_for("worker") is backend
    got = await backend.get("t1")
    assert got is not None and got.task_id == "t1"
    # agent-keyed path under project_root (shared across the agent's sessions).
    assert (tmp_path / ".reyn" / "agents" / "worker" / "state" / "tasks.db").is_file()


# ── the N-connection hazards are REAL + deterministic (why (A) is needed) ────


@pytest.mark.asyncio
async def test_two_connections_one_file_write_collides(tmp_path) -> None:
    """Tier 2: the #2125 cross-connection WRITE race is real + deterministic. ``busy_timeout=0``
    means a second connection's write fails IMMEDIATELY while another holds the file's write
    lock — exactly the collision N per-session backend instances (N connections, N
    independent locks) can hit. A held ``BEGIN IMMEDIATE`` on a sibling connection → a real
    backend ``create`` raises "database is locked". (A)'s single instance makes this
    impossible: the one ``asyncio.Lock`` serialises every write so two writes never overlap."""
    db = tmp_path / "tasks.db"
    backend = SqliteTaskBackend(str(db))
    holder = sqlite3.connect(str(db))  # a sibling connection — the N-th instance's analog
    holder.execute("PRAGMA busy_timeout=0")
    holder.execute("BEGIN IMMEDIATE")  # hold the file's write lock
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await backend.create(_task("t1"))
    finally:
        holder.rollback()
        holder.close()
        backend.close()


@pytest.mark.asyncio
async def test_restore_with_sibling_connection_raises_locked(tmp_path) -> None:
    """Tier 2: (RED on N-instances) the restore file-swap hazard is real + deterministic.
    ``restore_to_seq`` closes its connection, swaps the db file, drops the ``-wal``/``-shm``
    side-files, then reopens with ``PRAGMA journal_mode=WAL`` — which needs the lock a SIBLING
    connection still holds → "database is locked". So with N per-session connections a rewind
    restore can FAIL outright. RED here, GREEN in the single-instance case below."""
    db = tmp_path / "tasks.db"
    primary = SqliteTaskBackend(str(db))
    sibling = SqliteTaskBackend(str(db))  # the N-th connection (a 2nd session's backend)
    await primary.create(_task("d1"))
    await primary.snapshot_generation(5)  # gen 5 = {d1}
    await primary.create(_task("d2"))
    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            await primary.restore_to_seq(5)
    finally:
        sibling.close()
        primary.close()


@pytest.mark.asyncio
async def test_restore_through_single_shared_instance_is_clean(tmp_path) -> None:
    """Tier 2: (GREEN on (A)) with ONE instance per agent (no sibling connection), the same
    ``restore_to_seq`` succeeds cleanly — it closes + reopens THE one connection and the
    restored generation is correct (sees the snapshotted ``d1``, not the post-snapshot
    ``d2``). The pair with the test above is the RED-on-N / GREEN-on-(A) falsification of the
    restore hazard."""
    reg = _registry(tmp_path)
    backend = reg.task_backend_for("worker")  # the agent's SOLE instance
    await backend.create(_task("d1"))
    await backend.snapshot_generation(5)
    await backend.create(_task("d2"))
    await backend.restore_to_seq(5)  # no sibling → no lock
    assert await backend.get("d2") is None     # restored past d2
    assert await backend.get("d1") is not None  # d1 (in the generation) survives


# ── close lifecycle: agent teardown closes + evicts (not last-session-drop) ──


@pytest.mark.asyncio
async def test_close_task_backend_closes_and_evicts(tmp_path) -> None:
    """Tier 2: ``_close_task_backend`` (the agent-teardown half) CLOSES the connection (a
    public read then raises ``ProgrammingError`` — the lock is released) AND evicts the cache
    entry, so a later ``task_backend_for`` rebuilds a FRESH instance rather than handing back
    a closed handle. RED if close didn't fire (read succeeds) or didn't evict (same closed
    instance returned)."""
    reg = _registry(tmp_path)
    first = reg.task_backend_for("worker")
    reg._close_task_backend("worker")
    with pytest.raises(sqlite3.ProgrammingError):
        await first.get("anything")  # closed → lock released
    second = reg.task_backend_for("worker")
    assert second is not first  # evicted → rebuilt


@pytest.mark.asyncio
async def test_agent_drop_closes_and_evicts_backend(tmp_path) -> None:
    """Tier 2: the agent-teardown path (#2103 rewind-drop ``_drop_agent``, which rmtrees the
    agent dir) closes + evicts the agent's shared backend BEFORE the rmtree — releasing the
    connection's handle on ``tasks.db`` ahead of the dir removal. RED if the drop left the
    connection open (a leaked handle over a removed dir) or kept the stale cache entry."""
    reg = _registry(tmp_path)
    first = reg.task_backend_for("worker")
    reg._drop_agent("worker")
    with pytest.raises(sqlite3.ProgrammingError):
        await first.get("anything")  # closed by the drop
    assert reg.task_backend_for("worker") is not first  # evicted


@pytest.mark.asyncio
async def test_purge_via_remove_closes_and_evicts_backend(tmp_path) -> None:
    """Tier 2: (the THIRD agent-teardown path — lead-caught) ``remove(name, purge=True)``,
    the live ``archive_agent(purge=True)`` hard-delete, closes + evicts the agent's shared
    backend BEFORE its rmtree — alongside ``_drop_agent`` + ``_purge_archived_below``.
    Without the close, the warmed cache entry keeps a dangling handle over the deleted inode
    AND is never evicted → a NEW agent reusing the name gets the stale handle and writes to
    the wrong/deleted file (a name-reuse correctness hazard, not just a leak). RED on the
    version missing the line-684 close. Asserts closed (ProgrammingError) + evicted (a
    re-create rebuilds a fresh instance)."""
    reg = _registry(tmp_path)
    first = reg.task_backend_for("worker")  # warm it (also creates the agent dir)
    reg.remove("worker", purge=True)  # the hard-delete escape hatch
    with pytest.raises(sqlite3.ProgrammingError):
        await first.get("anything")  # closed → handle released ahead of the rmtree
    assert reg.task_backend_for("worker") is not first  # evicted → rebuilt fresh


@pytest.mark.asyncio
async def test_concurrent_creates_through_shared_instance_all_land(tmp_path) -> None:
    """Tier 2: (A)'s one shared instance serialises concurrent writes on its single
    ``asyncio.Lock`` — N concurrent ``create``s through the agent's backend ALL land (no
    "database is locked"), the property the per-instance lock guarantees once there is only
    ONE connection. (With the old N-connection model these would race the file lock.)"""
    reg = _registry(tmp_path)
    backend = reg.task_backend_for("worker")
    await asyncio.gather(*(backend.create(_task(f"t{i}")) for i in range(12)))
    for i in range(12):
        assert await backend.get(f"t{i}") is not None
