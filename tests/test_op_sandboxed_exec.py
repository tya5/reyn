"""Tier 2: sandboxed_exec op + SandboxPolicy + NoopBackend invariants (FP-0017).

Verifies:
- SandboxPolicy constructs with defaults.
- NoopBackend.available() is always True.
- NoopBackend.run(["echo", "hi"], ...) returns expected output.
- sandboxed_exec op dispatches through `execute_op` and emits P6 events.
- Wall-clock timeout enforces via subprocess timeout.
- registry: OP_KIND_MODEL_MAP and OP_PURITY include "sandboxed_exec".

No mocks of collaborators — real EventLog, Workspace, NoopBackend, dispatcher.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.events.events import EventLog
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.registry import (
    ALL_OP_KINDS,
    OP_KIND_MODEL_MAP,
    OP_PURITY,
    OpPurity,
)
from reyn.permissions.permissions import PermissionDecl
from reyn.sandbox import (
    NoopBackend,
    SandboxBackend,
    SandboxPolicy,
    SandboxResult,
    get_default_backend,
)
from reyn.sandbox import noop_backend as _noop_module
from reyn.schemas.models import SandboxedExecIROp
from reyn.workspace.workspace import Workspace

# ─── 1. SandboxPolicy ────────────────────────────────────────────────────────


def test_policy_defaults():
    """Tier 2: SandboxPolicy() applies safe-default field values."""
    p = SandboxPolicy()
    assert p.network is False
    assert p.read_paths == []
    assert p.write_paths == []
    assert p.allow_subprocess is False
    assert p.env_passthrough == []
    assert p.timeout_seconds == 60


def test_policy_custom_fields():
    """Tier 2: SandboxPolicy accepts custom field values."""
    p = SandboxPolicy(
        network=True,
        read_paths=["/tmp"],
        write_paths=["/var/out"],
        allow_subprocess=True,
        env_passthrough=["PATH", "HOME"],
        timeout_seconds=5,
    )
    assert p.network is True
    assert p.read_paths == ["/tmp"]
    assert p.write_paths == ["/var/out"]
    assert p.allow_subprocess is True
    assert p.env_passthrough == ["PATH", "HOME"]
    assert p.timeout_seconds == 5


# ─── 2. NoopBackend ──────────────────────────────────────────────────────────


def test_noop_backend_always_available():
    """Tier 2: NoopBackend.available() returns True unconditionally."""
    assert NoopBackend().available() is True


def test_noop_backend_satisfies_protocol():
    """Tier 2: NoopBackend conforms to the SandboxBackend Protocol."""
    backend = NoopBackend()
    assert isinstance(backend, SandboxBackend)
    assert backend.name == "noop"


def test_get_default_backend_returns_protocol_conformant_backend():
    """Tier 2: get_default_backend() returns a Protocol-conformant available backend.

    Since FP-0017 Components B+C landed, the default factory is platform-aware
    (= Seatbelt on Darwin, Landlock on Linux 5.13+, Noop fallback elsewhere or
    when the platform backend reports unavailable). This test pins only the
    invariants the factory contract guarantees, not the specific backend name.
    """
    backend = get_default_backend()
    assert isinstance(backend, SandboxBackend)
    assert backend.available() is True
    assert backend.name in {"noop", "seatbelt", "landlock"}


@pytest.mark.asyncio
async def test_noop_run_echo():
    """Tier 2: NoopBackend.run(['echo', 'hi']) returns expected output."""
    backend = NoopBackend()
    policy = SandboxPolicy(env_passthrough=["PATH"])
    result = await backend.run(["echo", "hi"], policy)
    assert isinstance(result, SandboxResult)
    assert result.returncode == 0
    assert b"hi" in result.stdout
    assert result.truncated is False


@pytest.mark.asyncio
async def test_noop_run_timeout():
    """Tier 2: NoopBackend wall-clock timeout returns returncode=-1 + message."""
    backend = NoopBackend()
    policy = SandboxPolicy(timeout_seconds=1, env_passthrough=["PATH"])
    result = await backend.run(["sleep", "5"], policy)
    assert result.returncode == -1
    assert b"timed out" in result.stderr.lower() or b"timeout" in result.stderr.lower()


@pytest.mark.asyncio
async def test_noop_run_nonzero_exit():
    """Tier 2: NoopBackend returns non-zero exit code for failing commands."""
    backend = NoopBackend()
    policy = SandboxPolicy(env_passthrough=["PATH"])
    # `false` exits with status 1 on POSIX
    result = await backend.run(["false"], policy)
    assert result.returncode != 0


# ─── 3. Registry wiring ──────────────────────────────────────────────────────


def test_registry_includes_sandboxed_exec():
    """Tier 2: OP_KIND_MODEL_MAP and ALL_OP_KINDS include 'sandboxed_exec'."""
    assert "sandboxed_exec" in OP_KIND_MODEL_MAP
    assert OP_KIND_MODEL_MAP["sandboxed_exec"] is SandboxedExecIROp
    assert "sandboxed_exec" in ALL_OP_KINDS


def test_op_purity_includes_sandboxed_exec():
    """Tier 2: OP_PURITY classifies sandboxed_exec as external (= same as shell)."""
    assert OP_PURITY["sandboxed_exec"] == OpPurity.external


# ─── 4. Op dispatch + events ──────────────────────────────────────────────────


def _make_ctx() -> tuple[OpContext, EventLog]:
    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
    )
    return ctx, events


@pytest.mark.asyncio
async def test_dispatch_emits_started_and_completed():
    """Tier 2: sandboxed_exec dispatch through execute_op emits both P6 events.

    Backend-agnostic: the factory picks per-platform (Noop / Seatbelt / Landlock);
    we assert the dispatch contract holds (status / events / stdout) and that
    the recorded backend name matches whatever the factory returned.
    """
    ctx, events = _make_ctx()
    # /bin/echo for portability — Seatbelt's deny-default profile doesn't
    # implicitly resolve bare names from PATH on first exec.
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["/bin/echo", "hello"],
        env_passthrough=["PATH"],
        timeout_seconds=10,
    )
    result = await execute_op(op, ctx, caller="control_ir")
    assert result["status"] == "ok"
    assert result["kind"] == "sandboxed_exec"
    assert result["backend"] in {"noop", "seatbelt", "landlock"}
    assert result["returncode"] == 0
    assert "hello" in result["stdout"]

    event_types = [e.type for e in events.all()]
    assert "sandboxed_exec_started" in event_types
    assert "sandboxed_exec_completed" in event_types


@pytest.mark.asyncio
async def test_dispatch_timeout_status():
    """Tier 2: sandboxed_exec dispatch surfaces timeout as status='timeout'."""
    ctx, _events = _make_ctx()
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["/bin/sleep", "5"],
        env_passthrough=["PATH"],
        timeout_seconds=1,
    )
    result = await execute_op(op, ctx, caller="control_ir")
    # returncode -1 surfaces as either "timeout" status; the handler maps -1 -> "timeout".
    assert result["returncode"] == -1
    assert result["status"] == "timeout"


# ─── 5. Noop one-shot warning ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_emits_warning_once(caplog):
    """Tier 2: NoopBackend emits the no-enforcement WARN exactly once per process."""
    _noop_module._reset_warning_for_tests()
    backend = NoopBackend()
    policy = SandboxPolicy(env_passthrough=["PATH"])

    import logging
    with caplog.at_level(logging.WARNING, logger="reyn.sandbox.noop_backend"):
        await backend.run(["echo", "1"], policy)
        await backend.run(["echo", "2"], policy)

    warns = [r for r in caplog.records if "no isolation enforced" in r.message]
    assert len(warns) == 1, f"expected exactly one warning, got {len(warns)}: {warns}"
