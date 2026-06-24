"""Tier 2: #2103 S1bc — the session_spawn tool + the spawn_session_recorded seam.

session_spawn (router-only, async-dispatch) spawns a fresh-context session under the
agent + records it (config-complete session_spawned + per-session config.yaml narrowing)
+ submits the task. The spawned session RUNS the task; routing the result BACK is the
S1bc-exec follow-on (Stage-4), so the tool returns a spawn-ack (#1822 not-external).

No mocks: real AgentRegistry + a real Session factory + StateLog for the seam; a
recording callback for the handler→dispatch wiring (the run-loop e2e is the integration,
exercised by the broad suite).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.tools.session_spawn import SESSION_SPAWN, _handle
from reyn.tools.types import RouterCallerState, ToolContext


def _registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        return Session(agent_name=profile.name, state_log=state_log)

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    reg.create("worker")
    return reg


# ── spawn_session_recorded — the action-layer seam ──────────────────────────


@pytest.mark.asyncio
async def test_spawn_session_recorded_emits_config_complete_event(tmp_path: Path) -> None:
    """Tier 2: spawn_session_recorded emits a config-complete session_spawned WAL event
    (entity_kind/name/sid/mode/narrowing) — the create-record the rewind primitive +
    a future re-materialise read. (The narrowing's runtime EFFECT on the LIVE session is
    asserted via the production path in the next test.)"""
    reg = _registry(tmp_path)
    sid = await reg.spawn_session_recorded(
        "worker", mode="ephemeral", narrowing={"tool_deny": ["sandboxed_exec"]},
    )
    ev = next(
        e for e in reg.state_log.iter_from(0)
        if e.get("kind") == "session_spawned" and e.get("sid") == sid
    )
    assert ev["entity_kind"] == "session" and ev["name"] == "worker"
    assert ev["mode"] == "ephemeral" and ev["narrowing"] == {"tool_deny": ["sandboxed_exec"]}


@pytest.mark.asyncio
async def test_spawn_session_recorded_enforces_narrowing_on_live_session(tmp_path: Path) -> None:
    """Tier 2: #2126 — the PRODUCTION path. spawn_session_recorded re-resolves the
    spawned session's profile WITH its sid and re-injects it into the LIVE session, so the
    run-loop tool gate enforces the spawner's narrowing.

    The factory builds the session with the sid=None resolution (= every real frontend
    caller), so before the #2126 re-inject the live session's narrowing was None despite
    the written config.yaml — the write-wired/read-dead defect. The prior test asserted
    via ``resolved_profile_for(sid=sid)``, which hand-feeds the sid the production path
    never passes (a false-green; the per-session layer only loads with a sid). Here we
    read the per-turn effective narrowing the live tool gate actually consults. Strip the
    re-inject in spawn_session_recorded → the live gate sees no narrowing → RED."""
    reg = _registry(tmp_path)
    sid = await reg.spawn_session_recorded(
        "worker", mode="persistent", narrowing={"tool_deny": ["delete_file"]},
    )
    session = reg.get_session("worker", sid)
    # the live tool gate reads _effective_contextual_for_turn(); a fresh spawned session
    # has no untrusted-content history → it returns the injected _contextual_permission.
    effective = session._effective_contextual_for_turn()
    assert effective is not None, (
        "#2126: the live spawned session must enforce the spawner's narrowing "
        "(was None — write-wired/read-dead: config.yaml written but never re-injected)"
    )
    # #2132: BOTH invocable forms must be denied — the bare ``delete_file`` AND the native
    # qualified ``file__delete`` the production enumerate-all catalog advertises. Asserting
    # only the bare form was the false-green that hid the partial enforcement.
    assert {"delete_file", "file__delete"} <= effective.tool_deny


@pytest.mark.asyncio
async def test_spawn_session_recorded_no_narrowing_is_inert(tmp_path: Path) -> None:
    """Tier 2: no narrowing → the per-session S1a layer stays inert (resolved_profile_for
    is (None, ∅), the public surface), but session_spawned is still emitted
    (rewind-tracking is unconditional)."""
    reg = _registry(tmp_path)
    sid = await reg.spawn_session_recorded("worker", mode="persistent", narrowing=None)
    assert reg.resolved_profile_for("worker", sid=sid) == (None, frozenset())  # inert
    assert any(e.get("kind") == "session_spawned" for e in reg.state_log.iter_from(0))


# ── the tool: registration + schema + handler dispatch ──────────────────────


def test_session_spawn_registered_with_schema() -> None:
    """Tier 2: session_spawn is router-callable + its schema gates the spawn-time mode
    (ephemeral|persistent) + requires request."""
    from reyn.tools import get_default_registry
    assert "session_spawn" in get_default_registry()
    params = SESSION_SPAWN.parameters
    assert params["properties"]["mode"]["enum"] == ["ephemeral", "persistent"]
    assert params["required"] == ["request"]
    assert SESSION_SPAWN.gates.router == "allow" and SESSION_SPAWN.gates.phase == "deny"


@pytest.mark.asyncio
async def test_handle_dispatches_to_spawn_session_fn() -> None:
    """Tier 2: the handler forwards (request, mode, narrowing) to
    spawn_session_fn and returns its ack (the tool→callback wiring)."""
    seen: dict = {}

    async def _fake_spawn_session_fn(*, request, mode, narrowing):
        seen.update(request=request, mode=mode, narrowing=narrowing)
        return {"status": "spawned", "sid": "abc", "mode": mode}

    ctx = ToolContext(
        events=None, permission_resolver=None, workspace=None, caller_kind="router",
        router_state=RouterCallerState(spawn_session_fn=_fake_spawn_session_fn),
    )
    result = await _handle({"request": "do X", "mode": "ephemeral"}, ctx)
    assert seen == {"request": "do X", "mode": "ephemeral", "narrowing": None}
    assert result["status"] == "spawned" and result["sid"] == "abc"


@pytest.mark.asyncio
async def test_handle_rejects_invalid_mode() -> None:
    """Tier 2: an out-of-enum mode → error-shape (no dispatch)."""
    async def _never(**_kw):
        raise AssertionError("must not dispatch on invalid mode")

    ctx = ToolContext(
        events=None, permission_resolver=None, workspace=None, caller_kind="router",
        router_state=RouterCallerState(spawn_session_fn=_never),
    )
    result = await _handle({"request": "x", "mode": "bogus"}, ctx)
    assert result["status"] == "error" and result["kind"] == "invalid_mode"


@pytest.mark.asyncio
async def test_handle_requires_spawn_session_fn() -> None:
    """Tier 2: a host without session-spawn support → RuntimeError (mis-wiring), not a
    silent success."""
    ctx = ToolContext(
        events=None, permission_resolver=None, workspace=None, caller_kind="router",
        router_state=RouterCallerState(spawn_session_fn=None),
    )
    with pytest.raises(RuntimeError, match="spawn_session_fn"):
        await _handle({"request": "x"}, ctx)
