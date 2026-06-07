"""Tier 2c: #1341 build-based devcontainer — REAL docker build + launch.

The unit tests (``test_devcontainer_build_1341``) pin the build ARGV + tag +
inspect-then-build order against an injectable runner, but are blind to whether
a real ``docker build`` actually produces a *working* image from the constructed
argv — the same gap that hid the #1356→#1363 docker-exec argv bug (units green,
real-daemon re-smoke caught it; lead pin: fake_backend_unit_misses_real_integration).
This test closes it: a minimal build-based devcontainer is built on demand by the
REAL ``ContainerLauncher`` and launched, and we assert the Dockerfile's RUN marker
is present in the running container (proving the BUILT image — not the default —
was used). Content-addressed rebuild-on-change is exercised against the daemon too.

Skipped when no Docker daemon is reachable (CI-safe; runs on native Linux docker).
Uses ``python:3.12-slim`` (fast public pull). Containers/images use unique tags
+ are always cleaned up in a finally (xdist-safe; no leaks).
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from reyn.environment.container_backend import DockerEnvironmentBackend
from reyn.environment.container_launcher import (
    BuildSpec,
    ContainerLauncher,
    LaunchConfig,
    _devcontainer_image_tag,
    load_devcontainer_config,
)

_DOCKER_AVAILABLE = DockerEnvironmentBackend(
    container="_probe", repo_dir="/"
).available()

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not _DOCKER_AVAILABLE, reason="no reachable Docker daemon (docker info failed)"
    ),
]


def _write_build_devcontainer(ws: Path, marker: str, *, extra_run: str = "") -> None:
    """Write a workspace with a build-based devcontainer (minimal Dockerfile)."""
    dc = ws / ".devcontainer"
    dc.mkdir(parents=True, exist_ok=True)
    (ws / ".reyn").mkdir(exist_ok=True)
    body = (
        "FROM python:3.12-slim\n"
        f"RUN echo {marker} > /built_marker.txt\n"
        f"{extra_run}"
    )
    (dc / "Dockerfile").write_text(body)
    (dc / "devcontainer.json").write_text('{"build": {"dockerfile": "Dockerfile"}}')


def _exec(container_id: str, *cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", container_id, *cmd],
        capture_output=True, timeout=60, check=False,
    )


def _rmi(tag: str) -> None:
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True,
                   timeout=60, check=False)


def test_build_based_devcontainer_builds_and_runs(tmp_path: Path) -> None:
    """Tier 2c: #1341 — a build-based devcontainer is REALLY built + launched, and
    the Dockerfile's RUN marker is present in the container (proves the built image
    was used, end-to-end through the constructed `docker build` argv)."""
    ws = tmp_path / "ws"
    marker = f"built-{uuid.uuid4().hex[:8]}"
    _write_build_devcontainer(ws, marker)

    dc = load_devcontainer_config(str(ws))
    assert dc is not None and dc.buildable
    spec = BuildSpec(dockerfile=dc.dockerfile, context=dc.build_context,
                     build_args=dc.build_args, target=dc.build_target)
    tag = _devcontainer_image_tag(spec)
    config = LaunchConfig(workspace_root=str(ws), image=tag, build=spec)

    launcher = ContainerLauncher()
    container_id = launcher.launch(config, timeout=600)
    try:
        # the built image carries the Dockerfile's RUN marker
        res = _exec(container_id, "cat", "/built_marker.txt")
        assert res.returncode == 0, res.stderr.decode(errors="replace")
        assert res.stdout.decode().strip() == marker
        # the content-addressed image tag actually exists locally
        inspect = subprocess.run(
            ["docker", "image", "inspect", tag],
            capture_output=True, timeout=60, check=False,
        )
        assert inspect.returncode == 0
    finally:
        launcher.teardown(container_id)
        _rmi(tag)


def test_rebuild_on_dockerfile_change(tmp_path: Path) -> None:
    """Tier 2c: #1341 — changing the Dockerfile yields a new content-addressed tag
    and a freshly-built image (F2 rebuild-on-change), live against the daemon."""
    ws = tmp_path / "ws"
    m1 = f"v1-{uuid.uuid4().hex[:8]}"
    _write_build_devcontainer(ws, m1)
    dc = load_devcontainer_config(str(ws))
    spec1 = BuildSpec(dockerfile=dc.dockerfile, context=dc.build_context)
    tag1 = _devcontainer_image_tag(spec1)
    launcher = ContainerLauncher()
    cid1 = launcher.launch(LaunchConfig(workspace_root=str(ws), image=tag1, build=spec1), timeout=600)
    try:
        assert _exec(cid1, "cat", "/built_marker.txt").stdout.decode().strip() == m1
    finally:
        launcher.teardown(cid1)

    # change the Dockerfile → new tag → rebuild
    m2 = f"v2-{uuid.uuid4().hex[:8]}"
    _write_build_devcontainer(ws, m2)
    spec2 = BuildSpec(dockerfile=dc.dockerfile, context=dc.build_context)
    tag2 = _devcontainer_image_tag(spec2)
    assert tag2 != tag1, "changed Dockerfile must yield a new content-addressed tag"
    cid2 = launcher.launch(LaunchConfig(workspace_root=str(ws), image=tag2, build=spec2), timeout=600)
    try:
        assert _exec(cid2, "cat", "/built_marker.txt").stdout.decode().strip() == m2
    finally:
        launcher.teardown(cid2)
        _rmi(tag1)
        _rmi(tag2)
