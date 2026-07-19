"""Tier 2: #2783 — StateLog/EventStore (and, for A2A remove_session, FsWatcher) must
actually be torn down on every production exit path.

Before this fix: `AgentRegistry.shutdown()` (the REPL /quit + Ctrl-C/EOF + dogfood +
`reyn pipe run` normal-exit seam) closed held MCP connections (#2714) but never
called `StateLog.aclose()` / `Session.aclose_event_store()` — both wrap a
`DurabilityWorker` whose queued-but-not-yet-written tail entries are silently
dropped when `asyncio.run()` cancels outstanding tasks at loop teardown (the same
defect class as #1765's WAL fix and #2780's EventStore fix, one layer up: those
fixed the WRITE path off-loop; this fixes the DRAIN-on-exit gap).

Separately, `reyn chat --once` (`chat.py`'s `once=True` branch) reached NEITHER
`registry.shutdown()` NOR any teardown of any kind — not just StateLog/EventStore,
MCP/FsWatcher too.

And `AgentRegistry.remove_session` (the A2A spawned-session drop seam) already
closed MCP synchronously before cancelling the session's `run()` task, but relied
on that same cancelled task's own `finally` to close FsWatcher/EventStore — a
genuine race, since the cancelled task is never awaited before `remove_session`
returns.

Real `AgentRegistry` + real `Session` instances throughout, per the testing
policy — no `mock.patch`/`MagicMock`. Observed via each seam's own public surface
(`active_path`'s file content, `is_fs_watcher_active`) rather than private state.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log, registry=holder.get("reg"))
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("owner", role="").save(tmp_path / ".reyn" / "agents" / "owner")
    return reg


def _event_store_file_contains(session: Session, needle: str) -> bool:
    path = session._event_store.active_path
    if path is None or not path.exists():
        return False
    return needle in path.read_text()


@pytest.mark.asyncio
async def test_shutdown_drains_event_store(tmp_path, monkeypatch):
    """Tier 2: `registry.shutdown()` drains the main session's EventStore so a
    queued-but-unwritten event lands on disk before the process exits (#2783).
    RED before the fix: `write()` enqueues via `submit_nowait` (fire-and-forget,
    per #2780) and nothing drained it on shutdown, so the file could be missing the
    trailing event immediately after `shutdown()` returned."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    session = reg.get_or_load("owner")

    session._chat_events.emit("budget_warn", dimension="daily_tokens")
    await reg.shutdown()

    assert _event_store_file_contains(session, "budget_warn"), (
        "registry.shutdown() must drain the main session's EventStore (#2783)"
    )


@pytest.mark.asyncio
async def test_shutdown_drains_state_log(tmp_path, monkeypatch):
    """Tier 2: `registry.shutdown()` drains the registry-wide StateLog (WAL) too —
    the same gap #1765 originally fixed for the WRITE path, now closed for the
    DRAIN-on-exit path. Uses `append_nowait` (fire-and-forget, per #1765) — the
    awaited `append()` already blocks for durability regardless of this fix, so it
    would pass even without it; `append_nowait` is the path that actually needs
    `aclose()` to drain on exit. Observed via the WAL file's own content (public:
    the file IS the durable surface), not private state."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("owner")

    reg._state_log.append_nowait("agent_archived", entity_kind="agent", name="owner")
    await reg.shutdown()

    wal_path = tmp_path / "wal.jsonl"
    assert wal_path.exists()
    assert "agent_archived" in wal_path.read_text(), (
        "registry.shutdown() must drain the shared StateLog (#2783)"
    )


@pytest.mark.asyncio
async def test_shutdown_event_store_close_is_idempotent(tmp_path, monkeypatch):
    """Tier 2: calling `aclose_event_store()` twice (once via `shutdown()`, once
    directly) must not raise — `EventStore.aclose()` is documented idempotent
    (#2780); the #2783 wiring relies on this so a second close from an overlapping
    teardown seam is a harmless no-op, not a crash."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    session = reg.get_or_load("owner")

    await reg.shutdown()
    await session.aclose_event_store()  # second close — must not raise


def test_reyn_run_once_cli_reaches_registry_shutdown(tmp_path, monkeypatch):
    """Tier 2: #2783 — `reyn chat --once` (`chat.py`'s `once=True` branch,
    delegated to from `reyn run-once`) used to return/exit with ZERO teardown of
    any kind — not just StateLog/EventStore, MCP/FsWatcher too, since it never
    reached `registry.shutdown()` at all. Drives the REAL production entry point
    (`run_once.register` → the real argparse defaults → the real `chat.run(args)`,
    the exact code changed by this fix) end to end, with only the network-facing
    LLM drive (`send_to_agent_impl`) substituted for a fast real async function (a
    same-signature stand-in, not a MagicMock/AsyncMock) — the turn-driving
    internals it replaces are already covered by #187's own tests; this test is
    about whether the NEW try/finally wrapper around it reaches
    `registry.shutdown()`. Observed via a call-through spy on the real
    `AgentRegistry.shutdown` (wraps and still calls the original — a call-count
    probe, not a mock that fakes the method's behavior), not private state."""
    import argparse

    from reyn.interfaces.cli.commands import run_once
    from reyn.runtime.registry import AgentRegistry

    monkeypatch.chdir(tmp_path)
    top = argparse.ArgumentParser()
    sub = top.add_subparsers()
    run_once.register(sub)
    args = top.parse_args(["run-once"])

    async def _fake_send(registry, *, agent_name, message, timeout=0,
                          intervention_override=None, sid=None) -> dict:
        return {"reply": "ok", "limit_stopped": False}

    monkeypatch.setattr("reyn.mcp.server.send_to_agent_impl", _fake_send)

    orig_shutdown = AgentRegistry.shutdown
    call_count = {"n": 0}

    async def _counting_shutdown(self) -> None:
        call_count["n"] += 1
        await orig_shutdown(self)

    monkeypatch.setattr(AgentRegistry, "shutdown", _counting_shutdown)
    monkeypatch.setattr("sys.stdin", io.StringIO("hi"))

    from reyn.interfaces.cli.commands import chat
    chat.run(args)

    assert call_count["n"] == 1, (
        "reyn run-once / reyn chat --once must reach registry.shutdown() exactly "
        "once so MCP/FsWatcher/StateLog/EventStore all get torn down (#2783) — "
        "before this fix it reached zero times"
    )


@pytest.mark.asyncio
async def test_remove_session_closes_event_store_synchronously_before_cancel(tmp_path, monkeypatch):
    """Tier 2: #2783 A2A race — `remove_session` must close the spawned session's
    EventStore SYNCHRONOUSLY (before `task.cancel()`, mirroring the existing MCP
    close), not rely on the cancelled `run()` task's own `finally` to get there.
    RED before the fix: the event landed only if `run()`'s finally happened to
    execute before this assertion — a genuine race, not guaranteed. Observed via
    the file's content immediately after `remove_session` returns, no further
    await needed if the fix is synchronous."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("owner")
    spawned_sid = reg.spawn_session("owner", presentation_consumer=None, intervention_bridge=None)
    spawned = reg.get_session("owner", spawned_sid)
    spawned.register_intervention_listener("test")

    spawned._chat_events.emit("budget_warn", dimension="daily_tokens")

    await reg.remove_session("owner", spawned_sid, record=False)

    assert _event_store_file_contains(spawned, "budget_warn"), (
        "remove_session must close the spawned session's EventStore synchronously, "
        "before task.cancel(), not rely on the cancelled run() task's own finally (#2783)"
    )


@pytest.mark.asyncio
async def test_archive_agent_closes_event_store_and_fs_watcher(tmp_path, monkeypatch):
    """Tier 2: #2783 — `archive_agent` (the DELETE seam for the MAIN session,
    `registry.py:805-834`) got the SAME FsWatcher/EventStore synchronous-close
    fix as `remove_session` (`registry.py:1968-1990`), added in the SAME PR — a
    separate call site with its own risk of being missed. Per the co-vet
    discipline of falsifying every call site a multi-site fix touches, not just
    one sibling path: this test targets `archive_agent` specifically, distinct
    from `test_remove_session_closes_event_store_synchronously_before_cancel`
    above (which only drives `remove_session`).

    Uses a call-through spy on `Session.aclose_event_store`/`aclose_fs_watcher`
    rather than an on-disk content check: `archive_agent` awaits
    `self._state_log.append(...)` AFTER the close block, and that await gives
    the EventStore's background drainer a chance to flush the queued write
    anyway — even WITHOUT this fix — so an on-disk assertion here would pass
    whether or not `archive_agent` actually called the close methods (a
    timing coincidence, not a real green). A call-count spy is deterministic
    regardless of drainer timing."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    session = reg.get_or_load("owner")

    calls: dict[str, int] = {"event_store": 0, "fs_watcher": 0}
    orig_aclose_event_store = session.aclose_event_store
    orig_aclose_fs_watcher = session.aclose_fs_watcher

    async def _spy_event_store() -> None:
        calls["event_store"] += 1
        await orig_aclose_event_store()

    async def _spy_fs_watcher() -> None:
        calls["fs_watcher"] += 1
        await orig_aclose_fs_watcher()

    monkeypatch.setattr(session, "aclose_event_store", _spy_event_store)
    monkeypatch.setattr(session, "aclose_fs_watcher", _spy_fs_watcher)

    await reg.archive_agent("owner", purge=False)

    assert calls["event_store"] == 1, "archive_agent must call session.aclose_event_store() (#2783)"
    assert calls["fs_watcher"] == 1, "archive_agent must call session.aclose_fs_watcher() (#2783)"
