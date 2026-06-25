"""Tier 2: #2103 S1bc — session-spawn rewind-drop + the remove_session teardown seam.

Wires the session side of the #2103 as-of-cut DROP primitive: ``session_spawned`` is a
real CREATE-event (unioned into ``_LIFECYCLE_CREATE_KINDS``); on rewind-to-before-spawn
the primitive calls ``_drop_session`` → ``remove_session`` to tear the session down (no
empty-snapshot orphan). The teardown is full (rmtree) — the global WAL is the durable
source (the session_spawned record + session_id-routed entries survive), so a
forward-checkout re-materialises from the WAL, not the dir.

Real AgentRegistry + StateLog + on-disk session dirs (no mocks); the real rewind_to →
_materialize_rewind path.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import WAL_EVENT_KINDS, StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _LIFECYCLE_CREATE_KINDS, AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    # default create_event_kinds → _LIFECYCLE_CREATE_KINDS (includes session_spawned)
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def _make_session_dir(reg: AgentRegistry, name: str, sid: str) -> Path:
    d = reg._session_state_dir(name, sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "snapshot.json").write_text("{}", encoding="utf-8")
    return d


async def _put(log: StateLog, agent: str, text: str) -> int:
    return await log.append("inbox_put", target=agent, msg_id=text, msg_kind="user",
                            payload={"text": text})


async def _spawn_event(log: StateLog, name: str, sid: str) -> int:
    return await log.append(
        "session_spawned", entity_kind="session", name=name, sid=sid,
        mode="ephemeral", narrowing=None,
    )


# ── the kinds are registered ────────────────────────────────────────────────


def test_session_spawned_is_a_registered_create_kind() -> None:
    """Tier 2: session_spawned is in BOTH the WAL allowlist (appendable) AND the
    lifecycle create-event set (drives the as-of-cut drop), alongside agent_created."""
    assert "session_spawned" in WAL_EVENT_KINDS
    assert "session_vanished" in WAL_EVENT_KINDS
    assert "session_spawned" in _LIFECYCLE_CREATE_KINDS
    assert "agent_created" in _LIFECYCLE_CREATE_KINDS  # union preserved (no clobber)


# ── rewind-drop via the real primitive ──────────────────────────────────────


@pytest.mark.asyncio
async def test_session_spawned_after_cut_is_dropped(tmp_path) -> None:
    """Tier 2: the headline — rewind-to-before a session's spawn DROPS it (no orphan
    dir), via the real rewind_to → _materialize_rewind → _drop_session → remove_session."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "worker")
    victim_dir = _make_session_dir(reg, "worker", "task1")
    log = reg.state_log
    await _put(log, "worker", "pre")              # seq 1 (the rewind target)
    await _spawn_event(log, "worker", "task1")    # seq 2 — spawned AFTER the cut

    await reg.rewind_to(1)                         # cut = 1 < spawn-seq 2

    assert not victim_dir.exists()                # the spawned session torn down
    assert reg.get_session("worker", "task1") is None


@pytest.mark.asyncio
async def test_session_spawned_at_or_before_cut_is_kept(tmp_path) -> None:
    """Tier 2: boundary — a session spawned at-or-below the cut existed as-of-cut →
    kept (reconstructed), not dropped."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "worker")
    kept_dir = _make_session_dir(reg, "worker", "task1")
    log = reg.state_log
    await _spawn_event(log, "worker", "task1")    # seq 1 — spawned AT the cut
    await _put(log, "worker", "v1")               # seq 2

    await reg.rewind_to(1)                         # cut == spawn-seq → existed as-of-cut

    assert kept_dir.is_dir()                       # kept


# ── remove_session — the teardown seam ──────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_session_tears_down_spawned(tmp_path) -> None:
    """Tier 2: remove_session drops a spawned session in-memory + on-disk; get_session
    then None."""
    reg = _make_registry(tmp_path)
    reg._sessions.setdefault("worker", {})["s1"] = SimpleNamespace()
    d = _make_session_dir(reg, "worker", "s1")
    assert await reg.remove_session("worker", "s1") is True
    assert reg.get_session("worker", "s1") is None
    assert not d.exists()


@pytest.mark.asyncio
async def test_remove_session_refuses_main(tmp_path) -> None:
    """Tier 2: the main session is the agent's — not removable via this seam."""
    reg = _make_registry(tmp_path)
    with pytest.raises(ValueError, match="cannot remove the main session"):
        await reg.remove_session("worker", "main")


@pytest.mark.asyncio
async def test_remove_session_unknown_is_noop(tmp_path) -> None:
    """Tier 2: unknown (name, sid) → False (idempotent — the rewind drop can call it
    without pre-checking existence)."""
    reg = _make_registry(tmp_path)
    assert await reg.remove_session("worker", "never") is False


# ── #2125 rewind-across-spawn: connection-close + atomicity ──────────────────


@pytest.mark.asyncio
async def test_rewind_restore_failure_does_not_drop_the_session_dir(tmp_path) -> None:
    """Tier 2: #2125 atomicity — the destructive per-session rmtree is DEFERRED until the
    substrate restores succeed. A restore-failure must NOT leave the dir dropped (tui's
    'dirs dropped despite checkout failed'). Without the (b)-split (rmtree inline at drop),
    the dir would be gone despite the failed restore → RED."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "worker")
    victim_dir = _make_session_dir(reg, "worker", "task1")
    log = reg.state_log
    await _put(log, "worker", "pre")              # seq 1 (the rewind target)
    await _spawn_event(log, "worker", "task1")    # seq 2 — spawned AFTER the cut

    async def _boom(*, at_or_below: int) -> None:
        raise RuntimeError("simulated restore failure")

    # real callable (not a mock) — the restore raises mid-_materialize_rewind, AFTER the
    # post-cut session was detached but BEFORE the deferred rmtree.
    reg._restore_workspace_active = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="simulated restore failure"):
        await reg.rewind_to(1)

    assert victim_dir.exists(), (
        "#2125: a failed restore must not commit the destructive session drop"
    )


@pytest.mark.asyncio
async def test_remove_session_closes_dropped_backend_restore_uses_survivor(tmp_path) -> None:
    """Tier 2: #2125 + #2128 — remove_session CLOSES the dropped session's per-session
    Task-backend (a separate sqlite connection to the agent's shared tasks.db), releasing
    its lock. With #2128, _restore_task_active derives the rewind backends from the CURRENT
    loaded sessions (``_rewind_backends``), so the dropped session is simply no longer
    iterated — restore touches the survivor, never the now-closed dropped handle (no
    re-point pointer needed; the #2125 interim is subsumed). Strip the close() → restore on
    a still-open shared file is fine; the load-bearing property is that the dropped session
    is OUT of the derive set."""
    import sqlite3

    from reyn.task.factory import create_task_backend

    reg = _make_registry(tmp_path)
    db = tmp_path / "tasks.db"
    survivor_backend = create_task_backend("sqlite", path=str(db))
    dropped_backend = create_task_backend("sqlite", path=str(db))  # 2nd connection, same file

    async def _noop() -> None:
        return None

    main = SimpleNamespace(
        task_backend=survivor_backend, cancel_inflight=_noop, await_quiescent=_noop,
    )
    spawned = SimpleNamespace(
        task_backend=dropped_backend, cancel_inflight=_noop, await_quiescent=_noop,
    )
    reg._sessions["worker"] = {"main": main, "s1": spawned}
    _make_session_dir(reg, "worker", "s1")
    # a captured generation on the SURVIVOR at an active WAL seq → _restore_task_active
    # has real work that touches the backend's connection (else it no-ops without probing).
    seq = await _put(reg.state_log, "worker", "x")
    await survivor_backend.snapshot_generation(seq)

    await reg.remove_session("worker", "s1")

    # the dropped backend's connection is closed (a public DB read now raises) → lock released.
    with pytest.raises(sqlite3.ProgrammingError):
        await dropped_backend.get("any-task-id")
    # #2128: _restore_task_active derives from the loaded sessions — only the survivor
    # remains, so its generation is restored without error and the closed dropped backend
    # is never touched (it's out of _sessions). A path that still reached the dropped
    # connection would raise ProgrammingError inside restore_to_seq.
    await reg._restore_task_active(at_or_below=seq)
