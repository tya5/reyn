"""Tier 1/2: #1953 slice R — the Task backend participates in session rewind.

The sqlite Task backend is the 3rd rewind substrate (beside runtime-snapshot +
workspace): ``cut_generation`` captures a full-DB copy at each WAL boundary seq
and ``_materialize_rewind`` restores the nearest *active* generation <= the
target — symmetric with ``WorkspaceVersionStore``. In-memory / external backends
opt out (``supports_rewind=False``).

Real AgentRegistry + real Session + real sqlite + real StateLog/WAL; a no-LLM
``_FakeTurnDriver`` drives the genuine ``_run_router_loop`` so a real
``cut_generation`` fires (only the task write is simulated). No mocks.

Merge gates (lead): §7c falsifying-rewind (a task created after the rewind target
is gone after the rewind — RED on current main, which never restores the task
backend → GREEN) + §7d crash-recovery-untouched (``recover_rewind_if_needed``
still recovers with a task backend attached, and a non-rewinding backend leaves
the path inert).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.task import InMemoryTaskBackend, SqliteTaskBackend, Task, TaskState

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for the workspace substrate",
)


# ── Tier 1: interface contract ───────────────────────────────────────────────


def test_sqlite_opts_into_rewind_inmemory_opts_out():
    """Tier 1: sqlite advertises supports_rewind=True; in-memory (and any
    non-durable/external backend) advertises False (opt-out — external state
    cannot be rewound)."""
    assert SqliteTaskBackend.supports_rewind is True
    assert InMemoryTaskBackend.supports_rewind is False


@pytest.mark.asyncio
async def test_inmemory_rewind_hooks_are_noops():
    """Tier 1: the opt-out backend no-ops the substrate hooks (the OS guards on
    supports_rewind, but the hooks must be safe to call regardless)."""
    b = InMemoryTaskBackend()
    await b.snapshot_generation(1)
    await b.restore_to_seq(1)
    assert await b.generation_seqs() == []
    assert await b.prune_generations_below(5) == 0


@pytest.mark.asyncio
async def test_sqlite_snapshot_restore_prune_unit(tmp_path):
    """Tier 2: the sqlite substrate primitive — capture@1, create-after, capture@2,
    restore_to_seq(1) drops the post-1 task; prune bounds storage."""
    b = SqliteTaskBackend(tmp_path / "state" / "tasks.db")
    await b.create(Task(task_id="t1", name="t1", assignee="s", requester="r",
                        status=TaskState.PENDING))
    await b.snapshot_generation(1)
    await b.create(Task(task_id="t2", name="t2", assignee="s", requester="r",
                        status=TaskState.PENDING))
    await b.snapshot_generation(2)
    assert await b.generation_seqs() == [1, 2]
    await b.restore_to_seq(1)
    assert await b.get("t1") is not None
    assert await b.get("t2") is None          # created after gen-1 → gone
    # writable after restore (reopened connection)
    await b.create(Task(task_id="t3", name="t3", assignee="s", requester="r",
                        status=TaskState.PENDING))
    assert await b.get("t3") is not None
    assert await b.prune_generations_below(2) == 1   # gen-1 dropped
    assert await b.generation_seqs() == [2]


# ── shared real-registry harness (mirrors test_concurrent_multiagent_rewind_1580) ─


class _TaskTurnDriver:
    """No-LLM driver: creates a Task (keyed by user_text) in the session's backend
    + a runtime inbox marker, so the real loop's genuine cut_generation captures
    BOTH the runtime snapshot AND (slice R) the task-db generation."""

    def __init__(self, session: Session, backend) -> None:
        self._session = session
        self._backend = backend

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        if self._backend is not None:
            await self._backend.create(Task(
                task_id=user_text, name=user_text, assignee="sess",
                requester="req", status=TaskState.PENDING,
            ))
        await self._session._journal.append_inbox(
            kind="user_message", payload={"turn": user_text},
        )

    def request_cancel(self) -> None:
        return None

    def is_cancel_requested(self) -> bool:
        return False


def _registry(tmp_path: Path, *, backend_kind: str):
    """One-agent registry whose session carries a task backend of ``backend_kind``
    ('sqlite' rewinds, 'inmem' opts out, 'none' threads nothing). Returns
    (registry, session, backend)."""
    from reyn.core.events.state_log import StateLog

    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict[str, object] = {}

    def _factory(profile: AgentProfile) -> Session:
        state = tmp_path / ".reyn" / "agents" / profile.name / "state"
        snap = state / "snapshot.json"
        if backend_kind == "sqlite":
            tb: object | None = SqliteTaskBackend(state / "tasks.db")
        elif backend_kind == "inmem":
            tb = InMemoryTaskBackend()
        else:
            tb = None
        holder["backend"] = tb
        return Session(
            agent_name=profile.name, state_log=state_log, snapshot_path=snap,
            task_backend=tb,
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    AgentProfile.new("alpha", role="").save(tmp_path / ".reyn" / "agents" / "alpha")
    sess = reg.get_or_load("alpha")
    sess.register_intervention_listener("test")
    sess._loop_driver = _TaskTurnDriver(sess, holder["backend"])
    return reg, sess, holder["backend"]


# ── §7c: falsifying-rewind (the headline merge gate) ─────────────────────────


@pytest.mark.asyncio
async def test_rewind_restores_task_substrate(tmp_path):
    """Tier 2: §7c falsifying-rewind — rewinding to turn-1's boundary restores the
    task db to its as-of-seq state — the task created in turn 2 is GONE.

    RED on current main: ``_materialize_rewind`` restores runtime + workspace but
    NOT the task backend, so t2 survives the rewind. GREEN with slice R. Revert
    the slice-R src commits and this assertion fails."""
    reg, sess, tb = _registry(tmp_path, backend_kind="sqlite")

    await sess._run_router_loop("t1", "c1")
    seq1 = sess.current_snapshot.applied_seq
    await sess._run_router_loop("t2", "c1")

    assert await tb.get("t1") is not None
    assert await tb.get("t2") is not None        # pre-rewind: both live

    await reg.rewind_to(seq1)                      # global rewind to turn-1 boundary

    assert await tb.get("t1") is not None         # captured at/below seq1 → kept
    assert await tb.get("t2") is None             # created after seq1 → restored away


@pytest.mark.asyncio
async def test_rewind_then_new_turn_recaptures_active_branch(tmp_path):
    """Tier 2: is_active honor — after rewinding past turn 2 and running a NEW turn,
    a later rewind to that new boundary restores the post-rewind active branch
    (the abandoned turn-2 generation is never chosen)."""
    reg, sess, tb = _registry(tmp_path, backend_kind="sqlite")

    await sess._run_router_loop("t1", "c1")
    seq1 = sess.current_snapshot.applied_seq
    await sess._run_router_loop("t2", "c1")       # abandoned by the rewind below
    await reg.rewind_to(seq1)
    assert await tb.get("t2") is None

    await sess._run_router_loop("t3", "c1")        # new active-branch turn
    seq3 = sess.current_snapshot.applied_seq
    assert await tb.get("t3") is not None
    await reg.rewind_to(seq3)                       # rewind to the new boundary
    assert await tb.get("t1") is not None
    assert await tb.get("t3") is not None          # active branch kept
    assert await tb.get("t2") is None              # abandoned gen never restored


# ── §7d: crash-recovery untouched + opt-out inert ────────────────────────────


@pytest.mark.asyncio
async def test_rewind_with_inmemory_backend_is_inert(tmp_path):
    """Tier 2: opt-out — a non-rewinding (in-memory) task backend leaves the rewind
    path inert — the rewind still cuts runtime/workspace and does NOT error, and
    the in-memory task survives (it is not a rewind substrate)."""
    reg, sess, tb = _registry(tmp_path, backend_kind="inmem")
    await sess._run_router_loop("t1", "c1")
    seq1 = sess.current_snapshot.applied_seq
    await sess._run_router_loop("t2", "c1")
    await reg.rewind_to(seq1)                       # must not raise on the task path
    # in-memory opts out → not restored (its state is ephemeral, not a substrate).
    assert await tb.get("t2") is not None


@pytest.mark.asyncio
async def test_rewind_with_no_backend_is_inert(tmp_path):
    """Tier 2: §7d crash-recovery-untouched — with NO task backend threaded (the
    A2A/web + crash-recovery-before-session-load case) the rewind/recovery path is
    a clean no-op — runtime still cuts, nothing errors."""
    reg, sess, _tb = _registry(tmp_path, backend_kind="none")
    await sess._run_router_loop("t1", "c1")
    seq1 = sess.current_snapshot.applied_seq
    await sess._run_router_loop("t2", "c1")
    res = await reg.rewind_to(seq1)                 # no task backend → no-op task path
    assert res is not None                          # runtime rewind still happened


@pytest.mark.asyncio
async def test_crash_recovery_replays_rewind_with_task_backend(tmp_path):
    """Tier 2: §7d — recover_rewind_if_needed (the crash-recovery entry that
    replays a pending rewind at restart) runs cleanly with a task backend attached
    — the existing runtime recovery is untouched and the task substrate restore
    rides the SAME idempotent path (re-running it does not error)."""
    reg, sess, tb = _registry(tmp_path, backend_kind="sqlite")
    await sess._run_router_loop("t1", "c1")
    seq1 = sess.current_snapshot.applied_seq
    await sess._run_router_loop("t2", "c1")
    await reg.rewind_to(seq1)
    assert await tb.get("t2") is None
    # re-run the recovery path (simulates a restart after the rewind record): it is
    # idempotent and must not error or resurrect t2.
    await reg.recover_rewind_if_needed()
    assert await tb.get("t1") is not None
    assert await tb.get("t2") is None
