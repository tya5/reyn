"""Tier 2: pipeline-A2 — spawn_ephemeral_session, a programmatic non-LLM seam.

``spawn_ephemeral_session`` (``runtime/session_api.py``) wraps the SAME
``AgentRegistry.spawn_session_recorded`` primitive the ``session_spawn`` LLM tool
reaches via ``RouterCallerState.spawn_session_fn`` — but calls it directly, with
no ``RouterLoopHost`` / ``RouterCallerState`` / router-loop object anywhere on
the path. These tests assert gate-equivalence (same WAL event shape, same
narrowing enforcement) against the existing tool-path tests
(``tests/test_2103_s1bc_session_spawn_tool.py``), plus the router-free property
that is the whole point of this seam.

Real AgentRegistry + real Session factory + real StateLog (no mocks).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import spawn_ephemeral_session


def _registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return Session(
            agent_name=profile.name, state_log=state_log,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    reg.create("worker")
    return reg


# ── gate-equivalence: same WAL event shape as the tool path ─────────────────


@pytest.mark.asyncio
async def test_spawn_ephemeral_session_emits_config_complete_event(tmp_path: Path) -> None:
    """Tier 2: spawn_ephemeral_session emits a config-complete session_spawned WAL
    event (entity_kind/name/sid/mode="ephemeral") — the identical event shape
    ``reg.spawn_session_recorded`` emits for the LLM tool path."""
    reg = _registry(tmp_path)
    sid = await spawn_ephemeral_session(reg, identity="worker", presentation_consumer=None, intervention_bridge=None)
    ev = next(
        e for e in reg.state_log.iter_from(0)
        if e.get("kind") == "session_spawned" and e.get("sid") == sid
    )
    assert ev["entity_kind"] == "session"
    assert ev["name"] == "worker"
    assert ev["mode"] == "ephemeral"
    # the session is registered + reachable via the same public surface the tool
    # path's spawned session is reachable through.
    assert reg.get_session("worker", sid) is not None


# ── programmatic: no RouterLoopHost / RouterCallerState anywhere ────────────


@pytest.mark.asyncio
async def test_spawn_ephemeral_session_works_with_no_router_state(tmp_path: Path) -> None:
    """Tier 2: the whole point — a plain non-LLM caller with only an
    AgentRegistry reaches the primitive. No RouterLoopHost / RouterCallerState /
    ToolContext is constructed anywhere in this test, and session_api.py imports
    neither at module scope (grep-checked below)."""
    reg = _registry(tmp_path)
    sid = await spawn_ephemeral_session(reg, identity="worker", presentation_consumer=None, intervention_bridge=None)
    assert isinstance(sid, str) and sid


def test_session_api_module_has_no_router_loop_import() -> None:
    """Tier 2: session_api.py's import statements do not name router_loop /
    RouterCallerState / ToolContext — the programmatic seam is structurally
    decoupled from the router loop (not merely by prose convention). Parses
    only the ``import``/``from ... import`` nodes so a doc-comment mentioning
    these names (to explain what the seam bypasses) doesn't false-fail."""
    import ast

    import reyn.runtime.session_api as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
            imported_names.update(a.name for a in node.names)
    assert not any("router_loop" in n for n in imported_names)
    assert "RouterCallerState" not in imported_names
    assert "ToolContext" not in imported_names


# ── narrowing applied: reuses the existing ⊆-parent enforcement ─────────────


@pytest.mark.asyncio
async def test_spawn_ephemeral_session_narrowing_applied(tmp_path: Path) -> None:
    """Tier 2: narrowing passed to spawn_ephemeral_session is persisted + enforced
    on the LIVE spawned session — the identical #2126 re-inject the tool path
    exercises, reached via the programmatic seam instead of the tool handler."""
    reg = _registry(tmp_path)
    sid = await spawn_ephemeral_session(
        reg, identity="worker", narrowing={"tool_deny": ["delete_file"]},
    presentation_consumer=None, intervention_bridge=None)
    session = reg.get_session("worker", sid)
    effective = session._effective_contextual_for_turn()
    assert effective is not None, (
        "narrowing passed through spawn_ephemeral_session must be enforced on "
        "the live session (⊆-parent), not merely persisted to config.yaml"
    )
    assert {"delete_file", "file__delete"} <= effective.tool_deny


@pytest.mark.asyncio
async def test_spawn_ephemeral_session_no_narrowing_is_inert(tmp_path: Path) -> None:
    """Tier 2: no narrowing → the per-session capability layer stays inert
    (byte-identical to the no-narrowing tool-path case)."""
    reg = _registry(tmp_path)
    sid = await spawn_ephemeral_session(reg, identity="worker", presentation_consumer=None, intervention_bridge=None)
    assert reg.resolved_profile_for("worker", sid=sid) == (None, frozenset())
