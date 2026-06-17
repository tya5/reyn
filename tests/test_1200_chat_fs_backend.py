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
from reyn.core.events.state_log import StateLog
from reyn.environment.host_backend import HostBackend


def _session(tmp_path: Path, *, environment_backend=None, sandbox_backend=None) -> ChatSession:
    return ChatSession(
        agent_name="b",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        environment_backend=environment_backend,
        sandbox_backend=sandbox_backend,
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


# ── exec seam (#1200 PR-F2): OpContext.sandbox_backend instance ──────────────


class _FakeSandboxBackend:
    """Minimal SandboxBackend double (structural Protocol) for identity wiring."""

    def available(self) -> bool:
        return True


def test_chat_opcontext_uses_injected_sandbox_backend(tmp_path) -> None:
    """Tier 2: the agent SandboxBackend instance reaches the chat router
    OpContext (identity) → sandboxed_exec runs on it (`ctx.sandbox_backend or
    get_default_backend`), the SAME backend as the FS seam (single-shared-sandbox)."""
    agent_sandbox = _FakeSandboxBackend()
    session = _session(tmp_path, sandbox_backend=agent_sandbox)
    ctx = session._make_router_op_context()
    assert ctx.sandbox_backend is agent_sandbox       # the SAME injected instance


def test_chat_opcontext_sandbox_backend_none_by_default(tmp_path) -> None:
    """Tier 2: with no injected sandbox backend the OpContext leaves it None →
    sandboxed_exec falls to get_default_backend (unchanged behaviour)."""
    session = _session(tmp_path)
    ctx = session._make_router_op_context()
    assert ctx.sandbox_backend is None


def test_one_instance_serves_both_seams(tmp_path) -> None:
    """Tier 2: (★the single-shared-sandbox invariant) a docker-style backend
    passed as BOTH environment_backend + sandbox_backend reaches the FS seam
    (Workspace) AND the exec seam (OpContext) as the SAME object."""
    one = HostBackend()  # stands in for a DockerEnvironmentBackend (both protocols)
    session = _session(tmp_path, environment_backend=one, sandbox_backend=one)
    ctx = session._make_router_op_context()
    assert ctx.workspace.backend is one          # FS seam
    assert ctx.sandbox_backend is one            # exec seam — same instance
