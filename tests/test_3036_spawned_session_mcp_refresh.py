"""Tier 2: #3036 — a programmatically-spawned session's MCP roster is fresh at spawn, not
frozen at the registry's boot-time session_factory closure.

Root cause (dogfood-coder, #3036, 100% reproduction): `refresh_mcp_servers()` (the
`#2372` roster re-read + tool-probe chain) previously fired ONLY from
`Session._run_router_loop`'s turn-boundary hook (chat's per-user-message hot-reload
safe-point). A session `AgentRegistry.spawn_session_recorded` spawns programmatically
— an `agent`-step ephemeral worker (`spawn_ephemeral_session`), a pipeline
driver-session (`_spawn_pipeline_driver_session`, `mode="persistent"`), or a
`delegate_to_agent` sub-agent — never fires that chat-turn-boundary event itself, so
its `_mcp_servers` stayed whatever the registry's `session_factory` closure captured
at REGISTRY construction (boot time), even for a server `mcp_install` wrote to the
IN-set `.reyn/config/mcp.yaml` moments before this exact spawn (the RAG turnkey flow:
install in the chat session, then `pipeline__run` spawns the ingest driver-session —
topology-confirmed as ALWAYS two sessions with install strictly preceding the spawn,
never a mid-run mutation of an already-spawned session, so a spawn-time-only refresh
is sufficient for this family member).

Fix: `AgentRegistry.spawn_session_recorded` now calls `spawned_session.
refresh_mcp_servers()` right after construction (before returning the sid to the
caller), for every mode — the single funnel all three programmatic-spawn call sites
share.

Mirrors `tests/test_mcp_hot_reload_2372.py`'s pattern (install to the IN-set, assert
via the public `_router_host.get_mcp_servers()` router-facing enumeration) but drives
it through a REAL `AgentRegistry.spawn_session_recorded` — the actual production
call path every ephemeral/driver/delegate spawn takes — rather than calling
`refresh_mcp_servers()` directly (which would not exercise the fix at all).

FALSIFY: revert the `await spawned_session.refresh_mcp_servers()` call added to
`spawn_session_recorded` → the spawned session's roster stays whatever the registry's
session_factory captured at construction (empty, in this test's factory) even though
the server was installed to disk before the spawn — RED.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _install_server_in_config(project_root: Path, name: str) -> None:
    """Write a new MCP server to the IN-set `.reyn/config/mcp.yaml` — where
    `mcp_install` writes, mirroring the RAG SKILL.md's `mcp__install_local` flow."""
    cfg = project_root / ".reyn" / "config" / "mcp.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        yaml.safe_dump({"mcp": {"servers": {name: {"command": "/nonexistent", "description": "d"}}}}),
        encoding="utf-8",
    )


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """A registry whose `session_factory` — mirroring every real frontend's boot-time
    closure (`registry_bootstrap.py`, `scoped_session_factory.py`) — captures the
    MCP roster ONCE, before any install happens. Every session it constructs
    (main + every programmatic spawn) starts with this EMPTY snapshot; only the fix
    under test (a spawn-time `refresh_mcp_servers()` call) can make a freshly
    spawned session see a server installed after the registry was built."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    state_log = StateLog(tmp_path / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=False,
            mcp_servers={},  # the boot-time snapshot: no servers yet
        )
        s.register_intervention_listener("test")
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    AgentProfile.new("alice", role="").save(tmp_path / ".reyn" / "agents" / "alice")
    return reg


def _server_names(session: Session) -> list[str]:
    return [s["name"] for s in session._router_host.get_mcp_servers()]


@pytest.mark.asyncio
async def test_ephemeral_spawn_sees_server_installed_before_spawn(tmp_path, monkeypatch):
    """Tier 2: #3036 — an `agent`-step ephemeral worker (`mode="ephemeral"`, the
    `spawn_ephemeral_session` path) enumerates a server installed to the IN-set BEFORE
    the spawn, with no restart and no explicit `refresh_mcp_servers()` call from the
    test — the registry's spawn seam must do it. RED if the spawn-time refresh is
    reverted (the ephemeral worker would inherit the factory's empty boot-time
    snapshot forever, reproducing the dogfood-observed `mcp_servers={}`)."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")  # the live main session — pre-existing, unaffected

    _install_server_in_config(tmp_path, "reyn_chunker")  # disk write BEFORE the spawn

    eph_sid = await reg.spawn_session_recorded(
        "alice", mode="ephemeral", presentation_consumer=None, intervention_bridge=None,
    )
    eph = reg._peek_session("alice", eph_sid)

    assert "reyn_chunker" in _server_names(eph), (
        "an ephemeral agent-step worker must see a server installed before its own "
        "spawn — its MCP roster must not be frozen at the registry's boot-time "
        "session_factory snapshot"
    )


@pytest.mark.asyncio
async def test_persistent_driver_session_spawn_sees_server_installed_before_spawn(tmp_path, monkeypatch):
    """Tier 2: #3036 — the SAME gap, `mode="persistent"` — the pipeline driver-session
    path (`_spawn_pipeline_driver_session`, the session that actually runs
    `rag_ingest.ingest`'s X1 pre-flight `call_mcp_tool` steps) is NOT the
    `spawn_ephemeral_session` wrapper the #3036 architect verdict named as the hook
    point — it calls `registry.spawn_session_recorded(mode="persistent")` directly.
    Scoping the fix to `mode == "ephemeral"` only would leave the actual RAG-flow
    session (persistent) still stale. RED if the fix is narrowed to the ephemeral
    branch instead of covering every `spawn_session_recorded` call unconditionally."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    reg.get_or_load("alice")

    _install_server_in_config(tmp_path, "reyn_vector_store")

    driver_sid = await reg.spawn_session_recorded(
        "alice", mode="persistent", presentation_consumer=None, intervention_bridge=None,
    )
    driver = reg._peek_session("alice", driver_sid)

    assert "reyn_vector_store" in _server_names(driver), (
        "a persistent driver-session spawn (the pipeline driver's own mode) must see "
        "a server installed before its spawn — the fix must not be scoped to "
        "mode == 'ephemeral' only"
    )


@pytest.mark.asyncio
async def test_preexisting_main_session_is_not_retroactively_refreshed_by_a_later_spawn(tmp_path, monkeypatch):
    """Tier 2: #3036 — the fix is scoped to the NEWLY spawned session only; it must not
    reach back and refresh the pre-existing main/caller session as a side effect (that
    remains the chat turn-boundary's job, unchanged by this PR). Proves no accidental
    over-broad refresh was introduced."""
    monkeypatch.chdir(tmp_path)
    reg = _make_registry(tmp_path)
    main = reg.get_or_load("alice")
    assert _server_names(main) == []  # boot-time empty snapshot, no turn has run yet

    _install_server_in_config(tmp_path, "reyn_markitdown")
    await reg.spawn_session_recorded(
        "alice", mode="ephemeral", presentation_consumer=None, intervention_bridge=None,
    )

    assert _server_names(main) == [], (
        "spawning a child session must not retroactively refresh the pre-existing "
        "main session's own MCP roster — that stays the chat turn-boundary's job"
    )
