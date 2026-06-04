"""Tier 2: chat FS-seam backend injection (#1200 PR-F1, additive).

#1200 threads the agent's EnvironmentBackend INSTANCE to the chat axis so chat
file ops run on the SAME backend as the OSRuntime path (and plan, which delegates
tool exec to chat via _PlanStepHost). F1 is the FS seam: ChatSession gains an
``environment_backend`` param → its router Workspace runs IO on it. Additive —
None falls back to the workspace's HostBackend default (unchanged behaviour); the
exec seam (sandbox_backend string via sandbox_config) already flows agent-level,
so this is safe-to-land regardless of the (pending) exec instance-vs-string call.

No mocks: real ChatSession + real EnvironmentBackend instances.
"""
from __future__ import annotations

from pathlib import Path

from reyn.chat.session import ChatSession
from reyn.environment.host_backend import HostBackend
from reyn.events.state_log import StateLog


def _session(tmp_path: Path, *, environment_backend=None) -> ChatSession:
    return ChatSession(
        agent_name="b",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        environment_backend=environment_backend,
    )


def test_chat_workspace_uses_injected_backend(tmp_path) -> None:
    """Tier 2: the agent EnvironmentBackend instance reaches the chat router
    Workspace (identity) — the FS seam fix."""
    agent_backend = HostBackend()
    session = _session(tmp_path, environment_backend=agent_backend)
    ws = session._make_router_op_context().workspace
    assert ws.backend is agent_backend          # the SAME injected instance


def test_chat_workspace_defaults_to_host_backend(tmp_path) -> None:
    """Tier 2: with no injected backend the Workspace keeps its HostBackend
    default — additive, behaviour unchanged (safe-to-land)."""
    session = _session(tmp_path)
    ws = session._make_router_op_context().workspace
    assert isinstance(ws.backend, HostBackend)
    # and it is NOT some specific foreign instance (default-constructed).
    assert ws.backend is not HostBackend()       # distinct default instance
