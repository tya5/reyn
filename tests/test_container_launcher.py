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
    REYN_BASE_IMAGE,
    WORKSPACE_DEST_DEFAULT,
    ContainerLauncher,
    LaunchConfig,
    MountSpec,
    build_docker_run_argv,
    load_devcontainer_config,
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
    # Non-base image → ensure_image is a no-op, so the first call is docker run.
    cid = launcher.launch(LaunchConfig(workspace_root="/tmp/ws", image="img:test"))
    assert cid == "abc123"
    assert runner.calls[0][:3] == ["docker", "run", "-d"]


def test_launch_nonzero_raises():
    """Tier 2: a non-zero docker run exit raises RuntimeError."""
    runner = _FakeRunner([SandboxResult(returncode=1, stdout=b"", stderr=b"boom")])
    with pytest.raises(RuntimeError, match="launch failed"):
        ContainerLauncher(runner=runner).launch(
            LaunchConfig(workspace_root="/tmp/ws", image="img:test")
        )


def test_launch_empty_id_raises():
    """Tier 2: an empty container id raises rather than returning a bad handle."""
    runner = _FakeRunner([_ok(b"   \n")])
    with pytest.raises(RuntimeError, match="empty container id"):
        ContainerLauncher(runner=runner).launch(
            LaunchConfig(workspace_root="/tmp/ws", image="img:test")
        )


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
    launcher.launch(
        LaunchConfig(
            workspace_root="/tmp/ws", image="img:test", setup_command="pip install x"
        )
    )
    assert runner.calls[1][:3] == ["docker", "exec", "cid"]
    assert "pip install x" in runner.calls[1]


def test_teardown_removes_container():
    """Tier 2: teardown issues docker rm -f and reports success."""
    runner = _FakeRunner([_ok(b"")])
    assert ContainerLauncher(runner=runner).teardown("cid") is True
    assert runner.calls[0] == ["docker", "rm", "-f", "cid"]


# ─── ensure_image (bundled reyn base, build-on-demand) ─────────────────────────


def test_ensure_image_builds_base_when_absent():
    """Tier 2: a missing reyn base image is built from the bundled Dockerfile."""
    runner = _FakeRunner([
        SandboxResult(returncode=1, stdout=b"", stderr=b"No such image"),  # inspect
        _ok(b""),  # build
    ])
    ContainerLauncher(runner=runner).ensure_image(REYN_BASE_IMAGE)
    assert runner.calls[0][:3] == ["docker", "image", "inspect"]
    build = runner.calls[1]
    assert build[:4] == ["docker", "build", "-t", REYN_BASE_IMAGE]
    assert "-f" in build and build[-1].endswith("environment")  # context = dockerfile dir


def test_ensure_image_skips_build_when_present():
    """Tier 2: a present reyn base image is a no-op (inspect succeeds, no build)."""
    runner = _FakeRunner([_ok(b"sha256:...")])  # inspect succeeds
    ContainerLauncher(runner=runner).ensure_image(REYN_BASE_IMAGE)
    assert all("build" not in c[:2] for c in runner.calls)
    (only_call,) = runner.calls
    assert only_call[:3] == ["docker", "image", "inspect"]


def test_ensure_image_noop_for_non_base_image():
    """Tier 2: a non-base image is left to docker run to pull — ensure does nothing."""
    runner = _FakeRunner([])
    ContainerLauncher(runner=runner).ensure_image("python:3.12-slim")
    assert runner.calls == []


def test_ensure_image_build_failure_raises():
    """Tier 2: a failed base-image build raises RuntimeError."""
    runner = _FakeRunner([
        SandboxResult(returncode=1, stdout=b"", stderr=b"absent"),  # inspect
        SandboxResult(returncode=1, stdout=b"", stderr=b"build broke"),  # build
    ])
    with pytest.raises(RuntimeError, match="base image build failed"):
        ContainerLauncher(runner=runner).ensure_image(REYN_BASE_IMAGE)


def test_launch_builds_base_image_then_runs():
    """Tier 2: launching with the default (base) image builds it on demand first."""
    runner = _FakeRunner([
        SandboxResult(returncode=1, stdout=b"", stderr=b"absent"),  # inspect
        _ok(b""),  # build
        _ok(b"cid42\n"),  # run
    ])
    cid = ContainerLauncher(runner=runner).launch(LaunchConfig(workspace_root="/tmp/ws"))
    assert cid == "cid42"
    assert runner.calls[0][:3] == ["docker", "image", "inspect"]
    assert runner.calls[1][:2] == ["docker", "build"]
    assert runner.calls[2][:3] == ["docker", "run", "-d"]


# ─── devcontainer.json awareness (#1324 follow-up b) ──────────────────────────


def _write_devcontainer(tmp_path, body: str, *, root_level: bool = False):
    if root_level:
        p = tmp_path / ".devcontainer.json"
    else:
        (tmp_path / ".devcontainer").mkdir(exist_ok=True)
        p = tmp_path / ".devcontainer" / "devcontainer.json"
    p.write_text(body, encoding="utf-8")
    return p


def test_devcontainer_image_and_postcreate(tmp_path):
    """Tier 2: image → cfg.image, postCreateCommand → cfg.setup_command (once-after-create)."""
    _write_devcontainer(tmp_path, '{"image": "python:3.12", "postCreateCommand": "pip install -e ."}')
    cfg = load_devcontainer_config(str(tmp_path))
    assert cfg is not None
    assert cfg.image == "python:3.12"
    assert cfg.setup_command == "pip install -e ."
    assert cfg.build_based is False


def test_devcontainer_jsonc_comments_and_trailing_commas(tmp_path):
    """Tier 2: JSONC (// and /* */ comments + trailing commas) parses (spec-compliant)."""
    _write_devcontainer(tmp_path, """
    {
      // line comment
      "image": "node:20", /* block */
      "remoteUser": "node",
    }
    """)
    cfg = load_devcontainer_config(str(tmp_path))
    assert cfg is not None
    assert cfg.image == "node:20"
    assert cfg.user == "node"


def test_devcontainer_mounts_string_and_user(tmp_path):
    """Tier 2: string-form mounts → MountSpec; remoteUser → cfg.user."""
    _write_devcontainer(
        tmp_path,
        '{"image": "x", "remoteUser": "dev", '
        '"mounts": ["source=/host/d,target=/c/d,type=bind", "source=/ro,target=/c/ro,readonly"]}',
    )
    cfg = load_devcontainer_config(str(tmp_path))
    assert cfg is not None
    assert cfg.user == "dev"
    m0, m1 = cfg.mounts
    assert (m0.host, m0.container, m0.mode) == ("/host/d", "/c/d", "rw")
    assert (m1.host, m1.container, m1.mode) == ("/ro", "/c/ro", "ro")


def test_devcontainer_build_based_flagged(tmp_path):
    """Tier 2: a dockerFile/build devcontainer is flagged build_based (not yet supported)."""
    _write_devcontainer(tmp_path, '{"build": {"dockerfile": "Dockerfile"}}')
    cfg = load_devcontainer_config(str(tmp_path))
    assert cfg is not None
    assert cfg.build_based is True
    assert cfg.image is None


def test_devcontainer_root_level_location(tmp_path):
    """Tier 2: the root-level .devcontainer.json location is also detected."""
    _write_devcontainer(tmp_path, '{"image": "rootimg"}', root_level=True)
    cfg = load_devcontainer_config(str(tmp_path))
    assert cfg is not None and cfg.image == "rootimg"


def test_devcontainer_absent_returns_none(tmp_path):
    """Tier 2: no devcontainer.json → None (caller uses defaults)."""
    assert load_devcontainer_config(str(tmp_path)) is None


def test_devcontainer_malformed_returns_none(tmp_path):
    """Tier 2: a malformed devcontainer.json returns None (must not crash a launch)."""
    _write_devcontainer(tmp_path, '{"image": "x"  "broken"')
    assert load_devcontainer_config(str(tmp_path)) is None
