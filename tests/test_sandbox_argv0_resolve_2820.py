"""Tier 2: argv[0] launcher-shim pre-resolution — the #2820 part-A fix (filesystem-only).

resolve_real_executable strips a version-manager shim indirection by reading the
manager's ON-DISK layout — no subprocess, no exec — so the shim's launch-fork
never runs under (deny process-fork). These lock down: non-shim inputs untouched,
a pyenv/rbenv shim resolved via its ``versions/<v>/bin`` tree, an attacker-crafted
version string rejected (never a traversal or an exec), asdf/mise shims fail open
WITHOUT the manager being invoked (closing the mise config-exec surface,
CVE-2026-33646), and — the load-bearing invariant — resolution does NOT weaken
the sandbox policy (``allow_subprocess`` stays False).

The security regression guard (``test_crafted_cwd_config_has_no_parent_side_effect``)
exercises the INVOKE side-effect the block was about, not just the return value:
a crafted per-directory config with an exec-marker payload must produce NO
parent-side side-effect during resolution.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from reyn.security.sandbox.resolve import resolve_real_executable


def _make_executable(path: Path, body: str = "#!/bin/sh\necho x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _pyenv_layout(tmp_path: Path, version: str) -> tuple[Path, Path]:
    """Build ``<tmp>/.pyenv`` with a python3 shim and a real python3 under
    ``versions/<version>/bin``. Returns (pyenv_root, real_binary)."""
    root = tmp_path / ".pyenv"
    _make_executable(root / "shims" / "python3", "#!/bin/sh\necho SHIM_SHOULD_NOT_RUN\n")
    real = root / "versions" / version / "bin" / "python3"
    _make_executable(real, "#!/bin/sh\necho real\n")
    return root, real


def test_absolute_non_shim_path_is_returned_as_is():
    """Tier 2: an absolute real binary is not a shim — returned unchanged."""
    assert resolve_real_executable("/bin/sh") == "/bin/sh"


def test_bare_non_shim_command_resolves_to_absolute():
    """Tier 2: a bare command that is NOT a shim resolves to its absolute PATH
    location (no manager, no subprocess)."""
    resolved = resolve_real_executable("sh")
    assert os.path.isabs(resolved)
    assert resolved.endswith("/sh")


def test_command_not_on_path_returns_unchanged():
    """Tier 2: nothing to resolve → original argv0 back."""
    assert resolve_real_executable("no_such_binary_xyzzy_2820") == "no_such_binary_xyzzy_2820"


def test_pyenv_shim_resolves_via_local_version_file(tmp_path: Path, monkeypatch):
    """Tier 2: a ``python3`` that resolves to a pyenv shim is replaced by the real
    binary named by the nearest ``.python-version`` — read as DATA from disk, no
    subprocess. The shim indirection (and its fork) is gone."""
    root, real = _pyenv_layout(tmp_path, "3.12.7")
    (tmp_path / ".python-version").write_text("3.12.7\n")
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.delenv("PYENV_VERSION", raising=False)
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved == str(real)
    assert "/shims/" not in resolved


def test_pyenv_global_version_when_no_local_file(tmp_path: Path, monkeypatch):
    """Tier 2: with no ``.python-version`` in the tree, the global
    ``<root>/version`` selects the binary."""
    root, real = _pyenv_layout(tmp_path, "3.11.9")
    (root / "version").write_text("3.11.9\n")
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.delenv("PYENV_VERSION", raising=False)
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    workdir = tmp_path / "work"
    workdir.mkdir()
    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(workdir))
    assert resolved == str(real)


def test_pyenv_version_env_overrides_file(tmp_path: Path, monkeypatch):
    """Tier 2: ``PYENV_VERSION`` wins over the file (as pyenv itself does)."""
    root, real = _pyenv_layout(tmp_path, "3.10.4")
    (tmp_path / ".python-version").write_text("3.12.7\n")  # would lose to the env
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.setenv("PYENV_VERSION", "3.10.4")
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved == str(real)


def test_system_version_fails_open_to_shim(tmp_path: Path, monkeypatch):
    """Tier 2: ``.python-version`` of ``system`` means "not a managed version" →
    fail open to the shim (no versions/system/bin path invented)."""
    root, _real = _pyenv_layout(tmp_path, "3.12.7")
    (tmp_path / ".python-version").write_text("system\n")
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.delenv("PYENV_VERSION", raising=False)
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved.endswith("/.pyenv/shims/python3")


def test_nonexistent_version_fails_open(tmp_path: Path, monkeypatch):
    """Tier 2: a version with no installed ``versions/<v>/bin/python3`` → fail open
    (never fabricate a target)."""
    root, _real = _pyenv_layout(tmp_path, "3.12.7")
    (tmp_path / ".python-version").write_text("9.9.9\n")  # not installed
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.delenv("PYENV_VERSION", raising=False)
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved.endswith("/.pyenv/shims/python3")


@pytest.mark.parametrize(
    "payload",
    [
        "../../../../etc/passwd",   # path traversal
        "$(touch pwned)",           # shell substitution
        "{{ exec('touch pwned') }}",  # template exec (mise-style)
        "3.12.7/../../../etc",      # embedded separator
        "/absolute/evil",           # absolute
    ],
)
def test_malicious_version_token_is_rejected(tmp_path: Path, monkeypatch, payload: str):
    """Tier 2: an attacker-crafted ``.python-version`` never becomes a path — the
    version token is strictly validated, so a traversal/exec payload fails open to
    the shim instead of building or running anything."""
    root, _real = _pyenv_layout(tmp_path, "3.12.7")
    (tmp_path / ".python-version").write_text(payload + "\n")
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.delenv("PYENV_VERSION", raising=False)
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved.endswith("/.pyenv/shims/python3")  # fail open, no path built


def test_oversize_version_file_is_read_bounded(tmp_path: Path, monkeypatch):
    """Tier 2: a ``.python-version`` in the (attacker-writable) cwd is read with a
    byte cap, so a huge planted file cannot DoS resolution. A valid token at the
    head still resolves; megabytes of trailing junk are never read into memory."""
    root, real = _pyenv_layout(tmp_path, "3.12.7")
    # valid token first line, then ~8 MB of junk that must never be fully read
    (tmp_path / ".python-version").write_text("3.12.7\n" + ("A" * (8 * 1024 * 1024)))
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.delenv("PYENV_VERSION", raising=False)
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved == str(real)  # head token resolved, no unbounded read


def test_device_symlink_version_file_is_skipped(tmp_path: Path, monkeypatch):
    """Tier 2: a ``.python-version`` symlinked to a character device (/dev/zero)
    is not a regular file → skipped (never opened for an endless read), and
    resolution falls through to the global version."""
    root, real = _pyenv_layout(tmp_path, "3.11.9")
    (root / "version").write_text("3.11.9\n")
    devnull_target = Path("/dev/zero")
    if devnull_target.exists():
        (tmp_path / ".python-version").symlink_to(devnull_target)
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.delenv("PYENV_VERSION", raising=False)
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved == str(real)  # device symlink skipped, global used


def test_malicious_version_from_env_is_also_rejected(tmp_path: Path, monkeypatch):
    """Tier 2: token validation applies to the ENV source too (not just files) —
    a crafted PYENV_VERSION cannot become a path any more than a crafted file can."""
    root, _real = _pyenv_layout(tmp_path, "3.12.7")
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.setenv("PYENV_VERSION", "../../../../etc")
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved.endswith("/.pyenv/shims/python3")  # fail open, no path built


def test_resolved_bin_symlink_escaping_versions_root_fails_open(tmp_path: Path, monkeypatch):
    """Tier 2: even a valid version whose ``versions/<v>/bin/python3`` is a symlink
    pointing OUT of ``<root>/versions/`` fails open — the realpath containment
    check refuses to hand back a target outside the managed tree."""
    root = tmp_path / ".pyenv"
    _make_executable(root / "shims" / "python3", "#!/bin/sh\necho x\n")
    outside = tmp_path / "outside" / "python3"
    _make_executable(outside, "#!/bin/sh\necho evil\n")
    bin_dir = root / "versions" / "3.12.7" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3").symlink_to(outside)  # escapes versions/
    (tmp_path / ".python-version").write_text("3.12.7\n")
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.delenv("PYENV_VERSION", raising=False)
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved.endswith("/.pyenv/shims/python3")  # containment refused the escape


def test_mise_shim_fails_open_without_reading_config(tmp_path: Path, monkeypatch):
    """Tier 2: a mise shim is recognized but NEVER resolved — the manager is not
    invoked and its (possibly template-exec'ing) config is not even read. This is
    what closes the reported config-exec surface (CVE-2026-33646)."""
    shim = tmp_path / ".local" / "share" / "mise" / "shims" / "python3"
    _make_executable(shim, "#!/bin/sh\necho x\n")
    # a malicious mise config in cwd — must be entirely ignored
    (tmp_path / ".mise.toml").write_text('[tools]\npython = "{{ exec(\'touch pwned\') }}"\n')
    env_path = f"{shim.parent}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved == str(shim)  # fail open to the shim, unchanged
    assert not (tmp_path / "pwned").exists()  # config was never evaluated


def test_asdf_shim_fails_open(tmp_path: Path, monkeypatch):
    """Tier 2: an asdf shim also fails open (deferred — not resolved this PR)."""
    shim = tmp_path / ".asdf" / "shims" / "python3"
    _make_executable(shim, "#!/bin/sh\necho x\n")
    env_path = f"{shim.parent}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))
    assert resolved == str(shim)


def test_crafted_cwd_config_has_no_parent_side_effect(tmp_path: Path, monkeypatch):
    """Tier 2: security-regression falsify test (#2822 co-vet) — the load-bearing one.

    An attacker writes a per-directory config whose payload WOULD run a command
    (create a sentinel) if any manager evaluated it. Resolution must produce NO
    parent-side side-effect — because it never invokes a manager and reads a
    version file only as a validated token. Asserts on the INVOKE side-effect
    (sentinel absence), not merely the return value.
    """
    root, _real = _pyenv_layout(tmp_path, "3.12.7")
    sentinel = tmp_path / "SENTINEL_EXECUTED"
    # payloads that create the sentinel iff something exec's them
    (tmp_path / ".python-version").write_text(f"$(touch {sentinel})\n")
    (tmp_path / ".mise.toml").write_text(
        f'[tools]\npython = "{{{{ exec(\'touch {sentinel}\') }}}}"\n'
    )
    (tmp_path / ".tool-versions").write_text(f"python $(touch {sentinel})\n")
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.delenv("PYENV_VERSION", raising=False)
    env_path = f"{root / 'shims'}:{os.environ.get('PATH', '')}"

    resolved = resolve_real_executable("python3", env_path=env_path, cwd=str(tmp_path))

    assert not sentinel.exists(), "resolution executed a crafted-config payload (parent-side RCE)"
    assert resolved.endswith("/.pyenv/shims/python3")  # fail open, nothing run


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
async def test_handler_substitutes_resolved_argv0_without_weakening_policy(tmp_path: Path, monkeypatch):
    """Tier 2: the handler runs the RESOLVED argv0 (shim stripped) AND the sandbox
    policy is unchanged — allow_subprocess stays False. Stripping the shim must
    never be a backdoor to spawning; the (deny process-fork) boundary holds."""
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime import execute_op
    from reyn.core.op_runtime.context import OpContext
    from reyn.data.workspace.workspace import Workspace
    from reyn.schemas.models import SandboxedExecIROp
    from reyn.security.permissions.permissions import PermissionDecl

    root, real = _pyenv_layout(tmp_path, "3.12.7")
    (tmp_path / ".python-version").write_text("3.12.7\n")
    monkeypatch.setenv("PYENV_ROOT", str(root))
    monkeypatch.delenv("PYENV_VERSION", raising=False)
    monkeypatch.setenv("PATH", f"{root / 'shims'}:{os.environ.get('PATH', '')}")

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

    await execute_op(op, ctx)

    # the backend ran the real binary, not the shim
    assert backend.seen_argv is not None
    assert backend.seen_argv[0] == str(real)
    assert backend.seen_argv[1:] == ["-c", "print(1)"]
    # INVARIANT: the boundary was not weakened by the substitution
    assert backend.seen_allow_subprocess is False

    started = [e for e in events.all() if e.type == "sandboxed_exec_started"]
    assert started and started[0].data.get("argv") == ["python3", "-c", "print(1)"]
    assert started[0].data.get("argv0_resolved") == str(real)
