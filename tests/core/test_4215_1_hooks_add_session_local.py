"""Tier 2: #4215① — ``hooks_add`` writes ONLY to the calling session's own
per-session layer (``<session_state_dir>/hooks.yaml``), never a layer shared
with any other session or agent.

Supersedes #2088's scope-aware write (see the deleted
``test_2088_hooks_add_scope_aware.py``, same PR): #2088 gave a NAMED agent its
own per-agent layer (``.reyn/agents/<name>/hooks.yaml``) but a session's
``hooks_add`` write there was still visible to every OTHER session of that
SAME agent — and the default/unnamed agent still wrote the GLOBAL layer
(``.reyn/config/hooks.yaml``), visible to every session, named or not. This
was the owner's structural objection (issue #4215, quoting the owner):
"hooks は受動的なので、他の人による登録で自身に直接影響を受けるのが良くない"
(hooks are reactive, so being directly affected by someone else's
registration is undesirable). #4215① closes it by fixing the write target to
the session's OWN isolated layer, the SAME path
``Session._read_per_session_hooks`` (#2285's "4th, most-specific" COMBINE
layer) already reads from — no new read-side mechanism, only the write side
changes.

Real objects throughout — real ``Session``/``HotReloader``/``EventLog``/
``StateLog``; a real ``ToolContext``. No mocks.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.runtime.hot_reload import HotReloader, set_active_hot_reloader
from reyn.tools.hooks import _handle_hooks_add
from reyn.tools.types import ToolContext
from tests._support.agent_session import make_session


@pytest.fixture(autouse=True)
def _reset_active_reloader():
    yield
    set_active_hot_reloader(None)


def _ctx(root: Path, *, session_state_dir: "Path | None" = None) -> ToolContext:
    return ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=root), caller_kind="router",
        session_state_dir=session_state_dir,
    )


# ── Gate 1: the session-scoped write lands where the loader reads it ────────


@pytest.mark.asyncio
async def test_session_scoped_write_lands_at_session_state_dir(tmp_path: Path) -> None:
    """Tier 2: a session's hooks_add writes
    <session_state_dir>/hooks.yaml, NOT the global layer — the gate that
    prevents a write-only feature: strip ``ctx.session_state_dir`` handling
    in ``_hooks_yaml_path`` (fall back to always the global path) and this
    goes RED (the session-local file never exists)."""
    set_active_hot_reloader(HotReloader(project_root=tmp_path, events=EventLog()))
    session_dir = tmp_path / "session-a"

    result = await _handle_hooks_add(
        {"on": "turn_end", "message": "my-own-hook"},
        _ctx(tmp_path, session_state_dir=session_dir),
    )

    assert result["status"] == "ok"
    session_path = session_dir / "hooks.yaml"
    global_path = tmp_path / ".reyn" / "config" / "hooks.yaml"
    assert session_path.exists()
    assert not global_path.exists()  # did NOT leak into the global layer


@pytest.mark.asyncio
async def test_e2e_session_local_hook_fires_via_real_session(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: full self-reload E2E (mirrors #2073's crown-jewel test) — a
    session's hooks_add write is picked up by ITS OWN
    Session._build_hook_registry COMBINE (via ``_read_per_session_hooks``,
    #2285) at the next turn boundary and the hook actually fires, observed
    via the public inbox. The strongest form of gate 1: the write is
    consumed by the real runtime dispatch path, not merely present on
    disk."""
    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="s4215-agent",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    session_dir = Path(session._snapshot_path).parent  # the SAME dir _read_per_session_hooks reads

    ctx = ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=tmp_path), caller_kind="router",
        hot_reloader=session._hot_reloader, session_state_dir=session_dir,
    )
    result = await _handle_hooks_add(
        {"on": "turn_end", "message": "session-local-fires", "wake": True}, ctx,
    )
    assert result["reload_scheduled"] is True
    assert Path(result["path"]) == session_dir / "hooks.yaml"

    await session._hot_reloader.apply_pending()
    await session._hook_dispatcher.dispatch("turn_end", {})

    texts = set()
    while not session.inbox.empty():
        _kind, payload = session.inbox.get_nowait()
        texts.add(payload.get("text"))
    assert "session-local-fires" in texts


# ── Gate 2: non-session/test contexts fall back to the global layer ─────────


@pytest.mark.asyncio
async def test_no_session_state_dir_writes_global_unchanged(tmp_path: Path) -> None:
    """Tier 2: ctx.session_state_dir absent (non-session/test contexts — the
    CLI plugin/pipe ToolContext factories) — falls back to the pre-#4215
    global layer, same fallback shape ``agent_name``'s own None-fallback
    used to be."""
    set_active_hot_reloader(HotReloader(project_root=tmp_path, events=EventLog()))
    result = await _handle_hooks_add({"on": "turn_end", "message": "hi"}, _ctx(tmp_path))
    assert result["status"] == "ok"
    assert (tmp_path / ".reyn" / "config" / "hooks.yaml").exists()


# ── Gate 3: isolation — the owner's actual concern ───────────────────────────


@pytest.mark.asyncio
async def test_one_sessions_hook_write_is_invisible_to_a_sibling_session(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the #4215 owner concern, directly — a hook one session adds via
    hooks_add must NOT fire for a DIFFERENT session (even of the same agent
    name), because #4215① writes to a per-SESSION layer, not a layer shared
    across sessions (#2088's per-agent layer, or the pre-#2088 global one).

    Both sessions' reloaders are FORCED to reload (``request_reload`` +
    ``apply_pending``, not merely the reload ``hooks_add`` itself schedules
    on session A's OWN reloader) — session B must independently re-read and
    rebuild its OWN combine, so a leak into a SHARED layer (global, or a
    layer keyed only on ``agent_name``) would show up in session B's own
    fired hooks regardless of which session triggered the reload. Without
    forcing session B's reload too, this test is a false green: a write can
    land ANYWHERE and session B would never rebuild its registry to notice —
    verified by observation while writing this test (only forcing session
    A's reload left the assertion passing even against a same-agent-shared
    write). Falsify: pass ``session_state_dir=tmp_path`` to BOTH contexts
    (simulating a shared layer) and this goes RED — sibling session B's
    inbox then also contains "only-for-a"."""
    monkeypatch.chdir(tmp_path)
    session_a = make_session(
        agent_name="shared-agent", state_log=StateLog(tmp_path / "a.wal"),
        snapshot_path=tmp_path / "session-a" / "snap.json",
    )
    session_b = make_session(
        agent_name="shared-agent", state_log=StateLog(tmp_path / "b.wal"),
        snapshot_path=tmp_path / "session-b" / "snap.json",
    )

    ctx_a = ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=tmp_path), caller_kind="router",
        hot_reloader=session_a._hot_reloader,
        session_state_dir=Path(session_a._snapshot_path).parent,
    )
    await _handle_hooks_add(
        {"on": "turn_end", "message": "only-for-a", "wake": True}, ctx_a,
    )

    await session_a._hot_reloader.apply_pending()
    session_b._hot_reloader.request_reload(source="test")  # force B to independently rebuild
    await session_b._hot_reloader.apply_pending()
    await session_a._hook_dispatcher.dispatch("turn_end", {})
    await session_b._hook_dispatcher.dispatch("turn_end", {})

    texts_a = set()
    while not session_a.inbox.empty():
        _kind, payload = session_a.inbox.get_nowait()
        texts_a.add(payload.get("text"))
    texts_b = set()
    while not session_b.inbox.empty():
        _kind, payload = session_b.inbox.get_nowait()
        texts_b.add(payload.get("text"))

    assert "only-for-a" in texts_a
    assert "only-for-a" not in texts_b


# ── Gate 4: precedence — session-local is ADDITIVE with the other layers ────


@pytest.mark.asyncio
async def test_global_and_session_local_hooks_are_additive_not_overriding(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: precedence semantics, established from
    Session._build_hook_registry (startup ∪ runtime(global) ∪ per-agent ∪
    per-session — an ADDITIVE combine, see that method's docstring +
    docs/concepts/runtime/config-hot-reload.md's COMBINE table): an
    operator-defined global hook and this session's OWN session-local hook
    on the SAME lifecycle point BOTH fire — neither shadows the other.
    Falsifies a "session-local overrides global" (or vice versa) reading."""
    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="precedence-agent", state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
    )

    # Write the GLOBAL hook (non-session ctx — no session_state_dir).
    global_ctx = ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=tmp_path), caller_kind="router",
        hot_reloader=session._hot_reloader,
    )
    await _handle_hooks_add(
        {"on": "turn_end", "message": "global-hook", "wake": True}, global_ctx,
    )
    # Write the SESSION-LOCAL hook (this session's own isolated layer).
    session_ctx = ToolContext(
        events=EventLog(), permission_resolver=None,
        workspace=SimpleNamespace(root=tmp_path), caller_kind="router",
        hot_reloader=session._hot_reloader,
        session_state_dir=Path(session._snapshot_path).parent,
    )
    await _handle_hooks_add(
        {"on": "turn_end", "message": "session-local-hook", "wake": True}, session_ctx,
    )

    await session._hot_reloader.apply_pending()
    await session._hook_dispatcher.dispatch("turn_end", {})

    texts = set()
    while not session.inbox.empty():
        _kind, payload = session.inbox.get_nowait()
        texts.add(payload.get("text"))
    assert "global-hook" in texts
    assert "session-local-hook" in texts
