"""Tier 2: ContainerLauncher invariants — mount-mode launch + security (#1324).

Pins the launcher contract: the `docker run` argv (security defaults +
workspace mount), mount-spec parsing, and launch/teardown lifecycle. Uses a
real injectable fake runner (no mocks) so the launch path is exercised without
a live Docker daemon — mirroring container_backend's fs_runner/runner seam.
"""
from __future__ import annotations

import os

import pytest

from reyn.environment.container_launcher import (
    DEFAULT_IMAGE,
    WORKSPACE_DEST_DEFAULT,
    ContainerLauncher,
    LaunchConfig,
    MountSpec,
    build_docker_run_argv,
    parse_mount_spec,
)
from reyn.sandbox.backend import SandboxResult


class _FakeRunner:
    """Records argv calls; returns scripted SandboxResults in order (no mocks)."""

    def __init__(self, results: list[SandboxResult]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, stdin=None, timeout=None) -> SandboxResult:
        self.calls.append(list(argv))
        if self._results:
            return self._results.pop(0)
        return SandboxResult(returncode=0, stdout=b"", stderr=b"")


def _ok(stdout: bytes = b"") -> SandboxResult:
    return SandboxResult(returncode=0, stdout=stdout, stderr=b"")


# ─── build_docker_run_argv (security-critical core) ────────────────────────────


def test_build_argv_security_defaults():
    """Tier 2: default config emits the #1324 security baseline + workspace mount."""
    cfg = LaunchConfig(workspace_root="/tmp/ws")
    argv = build_docker_run_argv(cfg)
    joined = " ".join(argv)
    assert argv[:3] == ["docker", "run", "-d"]
    assert "--cap-drop ALL" in joined
    assert "--network none" in joined  # network off by default = exfiltration gate
    assert "--read-only" in joined
    assert "--tmpfs /tmp" in joined
    # workspace mounted rw at the fixed default dest.
    assert f"{os.path.abspath('/tmp/ws')}:{WORKSPACE_DEST_DEFAULT}:rw" in argv
    assert argv[-3:] == [DEFAULT_IMAGE, "sleep", "infinity"]


def test_build_argv_network_on_omits_none():
    """Tier 2: network=True does NOT add --network none (operator opted in)."""
    argv = build_docker_run_argv(LaunchConfig(workspace_root="/tmp/ws", network=True))
    assert "none" not in argv


def test_build_argv_read_only_disabled():
    """Tier 2: read_only_rootfs=False omits --read-only / --tmpfs."""
    argv = build_docker_run_argv(
        LaunchConfig(workspace_root="/tmp/ws", read_only_rootfs=False)
    )
    assert "--read-only" not in argv


def test_build_argv_additional_mounts_and_name():
    """Tier 2: additional mounts emit -v entries; name emits --name."""
    cfg = LaunchConfig(
        workspace_root="/tmp/ws",
        name="reyn-dev",
        mounts=[MountSpec(host="/data", container="/mnt/data", mode="ro")],
    )
    argv = build_docker_run_argv(cfg)
    assert "--name" in argv and "reyn-dev" in argv
    assert f"{os.path.abspath('/data')}:/mnt/data:ro" in argv


def test_build_argv_explicit_user_overrides_default():
    """Tier 2: an explicit non-root user is honored verbatim."""
    argv = build_docker_run_argv(LaunchConfig(workspace_root="/tmp/ws", user="1234:5678"))
    assert "--user" in argv
    assert "1234:5678" in argv


# ─── parse_mount_spec ──────────────────────────────────────────────────────────


def test_parse_mount_spec_two_part_defaults_rw():
    """Tier 2: a 2-part host:container spec defaults the mode to rw."""
    spec = parse_mount_spec("/host/path:/container/path")
    assert spec.host == "/host/path"
    assert spec.container == "/container/path"
    assert spec.mode == "rw"


def test_parse_mount_spec_three_part_ro():
    """Tier 2: a 3-part spec carries the explicit ro mode."""
    spec = parse_mount_spec("/h:/c:ro")
    assert spec.mode == "ro"


@pytest.mark.parametrize("bad", ["/onlyone", "/h:/c:bogus", "/h:", ":/c", "/h:/c:rw:extra"])
def test_parse_mount_spec_rejects_malformed(bad):
    """Tier 2: malformed specs raise ValueError (bad arity or unknown mode)."""
    with pytest.raises(ValueError):
        parse_mount_spec(bad)


# ─── ContainerLauncher lifecycle (injectable runner) ──────────────────────────


def test_launch_returns_container_id():
    """Tier 2: launch returns the trimmed container id from docker run stdout."""
    runner = _FakeRunner([_ok(b"abc123\n")])
    launcher = ContainerLauncher(runner=runner)
    cid = launcher.launch(LaunchConfig(workspace_root="/tmp/ws"))
    assert cid == "abc123"
    assert runner.calls[0][:3] == ["docker", "run", "-d"]


def test_launch_nonzero_raises():
    """Tier 2: a non-zero docker run exit raises RuntimeError."""
    runner = _FakeRunner([SandboxResult(returncode=1, stdout=b"", stderr=b"boom")])
    with pytest.raises(RuntimeError, match="launch failed"):
        ContainerLauncher(runner=runner).launch(LaunchConfig(workspace_root="/tmp/ws"))


def test_launch_empty_id_raises():
    """Tier 2: an empty container id raises rather than returning a bad handle."""
    runner = _FakeRunner([_ok(b"   \n")])
    with pytest.raises(RuntimeError, match="empty container id"):
        ContainerLauncher(runner=runner).launch(LaunchConfig(workspace_root="/tmp/ws"))


def test_launch_persistent_reuses_existing():
    """Tier 2: persistent+name with an existing container reuses it (no docker run)."""
    runner = _FakeRunner([_ok(b"existing99\n")])  # the `docker ps -q` lookup
    launcher = ContainerLauncher(runner=runner)
    cid = launcher.launch(
        LaunchConfig(workspace_root="/tmp/ws", persistent=True, name="reyn-dev")
    )
    assert cid == "existing99"
    # Only the ps lookup ran; no `docker run`.
    assert all("run" not in c[:2] for c in runner.calls)
    assert runner.calls[0][:3] == ["docker", "ps", "-q"]


def test_launch_runs_setup_command():
    """Tier 2: setup_command runs (docker exec) inside the container after launch."""
    runner = _FakeRunner([_ok(b"cid\n"), _ok(b"")])  # run, then exec
    launcher = ContainerLauncher(runner=runner)
    launcher.launch(LaunchConfig(workspace_root="/tmp/ws", setup_command="pip install x"))
    assert runner.calls[1][:3] == ["docker", "exec", "cid"]
    assert "pip install x" in runner.calls[1]


def test_teardown_removes_container():
    """Tier 2: teardown issues docker rm -f and reports success."""
    runner = _FakeRunner([_ok(b"")])
    assert ContainerLauncher(runner=runner).teardown("cid") is True
    assert runner.calls[0] == ["docker", "rm", "-f", "cid"]
