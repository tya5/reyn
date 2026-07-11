"""Tier 2: argv[0] launcher-shim pre-resolution — the #2820 part-A fix.

resolve_real_executable strips a version-manager shim indirection OUTSIDE the
sandbox so the shim's launch-fork runs in the trusted parent, not under
(deny process-fork). These lock down: non-shim inputs are untouched, a shim is
resolved via its manager (a real fake ``pyenv`` script, not a mock — testing.md
forbids mocks), every failure path fails open to the original, and — the
load-bearing invariant — resolution does NOT weaken the sandbox policy
(``allow_subprocess`` stays False; only the argv indirection changes).
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from reyn.security.sandbox.resolve import resolve_real_executable


def _make_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_absolute_non_shim_path_is_returned_as_is():
    """Tier 2: an absolute real binary is not a shim — returned unchanged."""
    assert resolve_real_executable("/bin/sh") == "/bin/sh"


def test_bare_non_shim_command_resolves_to_absolute():
    """Tier 2: a bare command that is NOT a shim resolves to its absolute PATH
    location (no manager involved)."""
    resolved = resolve_real_executable("sh")
    assert os.path.isabs(resolved)
    assert resolved.endswith("/sh")


def test_command_not_on_path_returns_unchanged():
    """Tier 2: nothing to resolve → original argv0 back (backend then emits its own
    not-found error, unchanged behavior)."""
    assert resolve_real_executable("no_such_binary_xyzzy_2820") == "no_such_binary_xyzzy_2820"


def _fake_pyenv_layout(tmp_path: Path, *, manager_body: str) -> tuple[str, Path]:
    """Build a fake ``<tmp>/.pyenv/shims/python3`` shim + a fake ``pyenv`` manager
    and a real target binary. Returns (env_path, real_binary_path)."""
    shim_dir = tmp_path / ".pyenv" / "shims"
    shim_dir.mkdir(parents=True)
    _make_executable(shim_dir / "python3", "#!/bin/sh\necho SHOULD_NOT_RUN_THE_SHIM\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real = tmp_path / "real" / "python3"
    real.parent.mkdir()
    _make_executable(real, "#!/bin/sh\necho real\n")

    _make_executable(bin_dir / "pyenv", manager_body)

    env_path = f"{shim_dir}:{bin_dir}:{os.environ.get('PATH', '')}"
    return env_path, real


def test_pyenv_shim_resolves_to_real_binary_via_manager(tmp_path: Path):
    """Tier 2: a ``python3`` resolving to a pyenv shim is replaced by the real
    binary the manager reports — the shim indirection (and its fork) is stripped.
    Uses a real fake ``pyenv which`` script (executed for real), not a mock."""
    real = tmp_path / "real" / "python3"
    manager_body = f'#!/bin/sh\n[ "$1" = which ] && echo "{real}"\n'
    env_path, real = _fake_pyenv_layout(tmp_path, manager_body=manager_body)

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved == str(real)
    assert "/shims/" not in resolved  # the shim indirection is gone


def test_fail_open_when_manager_errors(tmp_path: Path):
    """Tier 2: manager ``which`` exits nonzero → fail open to the shim path (never
    raise, never guess). The denial (if any) then stands, explained by part B."""
    manager_body = "#!/bin/sh\nexit 3\n"
    env_path, _real = _fake_pyenv_layout(tmp_path, manager_body=manager_body)

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved.endswith("/.pyenv/shims/python3")  # unchanged shim


def test_fail_open_when_manager_returns_nonexistent_path(tmp_path: Path):
    """Tier 2: manager prints a path that does not exist → fail open (don't run a
    bogus target)."""
    manager_body = '#!/bin/sh\n[ "$1" = which ] && echo "/nonexistent/python3"\n'
    env_path, _real = _fake_pyenv_layout(tmp_path, manager_body=manager_body)

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved.endswith("/.pyenv/shims/python3")


def test_unknown_shim_manager_fails_open(tmp_path: Path):
    """Tier 2: a ``/shims/`` path we cannot attribute to a known manager is left
    unchanged rather than resolved by guessing."""
    shim_dir = tmp_path / "someviz" / "shims"
    shim_dir.mkdir(parents=True)
    _make_executable(shim_dir / "python3", "#!/bin/sh\necho x\n")
    env_path = f"{shim_dir}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved.endswith("/someviz/shims/python3")


# ── the load-bearing invariant: resolution must not weaken the boundary ──────


class _CapturingBackend:
    """Real SandboxBackend test double capturing the argv + policy it is handed."""

    name = "capture"

    def __init__(self) -> None:
        self.seen_argv: list[str] | None = None
        self.seen_allow_subprocess: bool | None = None

    def available(self) -> bool:
        return True

    def wrap_command(self, argv, policy):  # pragma: no cover - unused
        from reyn.security.sandbox.backend import WrappedCommand

        return WrappedCommand(argv=list(argv))

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None):
        from reyn.security.sandbox.backend import SandboxResult

        self.seen_argv = list(argv)
        self.seen_allow_subprocess = policy.allow_subprocess
        return SandboxResult(returncode=0, stdout=b"ok\n", stderr=b"")


@pytest.mark.asyncio
async def test_handler_substitutes_resolved_argv0_without_weakening_policy(tmp_path: Path):
    """Tier 2: the handler runs the RESOLVED argv0 (shim stripped) AND the sandbox
    policy is unchanged — allow_subprocess stays False. Stripping the shim must
    never be a backdoor to spawning; the (deny process-fork) boundary holds."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl

    real = tmp_path / "real" / "python3"
    manager_body = f'#!/bin/sh\n[ "$1" = which ] && echo "{real}"\n'
    env_path, real = _fake_pyenv_layout(tmp_path, manager_body=manager_body)

    events = EventLog()
    workspace = Workspace(events=events, base_dir=tmp_path)
    backend = _CapturingBackend()
    ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=PermissionDecl(),
        sandbox_backend=backend,
    )
    op = SandboxedExecIROp(
        kind="sandboxed_exec",
        argv=["python3", "-c", "print(1)"],
        allow_subprocess=False,  # the workload must stay unable to fork
        env_passthrough=["PATH"],
        timeout_seconds=30,
    )

    orig_path = os.environ.get("PATH", "")
    os.environ["PATH"] = env_path
    try:
        await execute_op(op, ctx)
    finally:
        os.environ["PATH"] = orig_path

    # the backend ran the real binary, not the shim
    assert backend.seen_argv is not None
    assert backend.seen_argv[0] == str(real)
    assert backend.seen_argv[1:] == ["-c", "print(1)"]
    # INVARIANT: the boundary was not weakened by the substitution
    assert backend.seen_allow_subprocess is False

    started = [e for e in events.all() if e.type == "sandboxed_exec_started"]
    assert started and started[0].data.get("argv") == ["python3", "-c", "print(1)"]
    assert started[0].data.get("argv0_resolved") == str(real)
