"""Tier 2c: mount-mode real-docker launch → bind → state_dir coherence + security.

#1332: every other container test drives an injectable fake/recording runner
(``test_container_launcher`` argv-pin, ``test_container_backend_1115_stage2``
local-interpreter Fake, ``test_swe_bench_runner_container_1115`` recording
runner, ``test_run_container_backend_flags_1115`` no-docker Namespace). Those
verify the *caller's* logic but are blind to the real backend's own construction
— that the pinned ``docker run -v`` argv actually yields a working bind mount,
that host ``workspace_root/.reyn`` and in-container ``/workspace/.reyn`` are the
SAME physical dir (the part2 coherence claim), and that the security flags
actually apply. This is the one thing units cannot prove; it needs a live daemon.

Skipped when no Docker daemon is reachable (``DockerEnvironmentBackend.available``
= ``docker info``), so it is CI-safe on hosts without Docker and runs for free on
GitHub Actions ubuntu (which has Docker). Uses ``python:3.12-slim`` — a fast
public pull with python3 + bash + coreutils so the REAL FS-op / ``run()`` exec
snippets are exercised; the bundled reyn-base image (apt build, #1329) is NOT
needed to prove the mount/coherence/security mechanics, which are image-agnostic.

The launched container uses an auto-generated id (no fixed name) so xdist
``-n auto`` parallel workers never collide, and is always torn down in a finally.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reyn.environment.container_backend import DockerEnvironmentBackend
from reyn.environment.container_launcher import (
    WORKSPACE_DEST_DEFAULT,
    ContainerLauncher,
    LaunchConfig,
)
from reyn.sandbox.policy import SandboxPolicy

# Lightweight public image: python3 + bash + coreutils present, fast to pull.
_E2E_IMAGE = "python:3.12-slim"

# Skip gate: a probe backend (no real container yet) answers `docker info`.
_DOCKER_AVAILABLE = DockerEnvironmentBackend(
    container="_probe", repo_dir="/"
).available()

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not _DOCKER_AVAILABLE, reason="no reachable Docker daemon (docker info failed)"
    ),
]


def _container_exists(container_id: str) -> bool:
    """True if a container with ``container_id`` still exists (running or not)."""
    res = subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", f"id={container_id}"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return bool(res.stdout.strip())


@pytest.fixture(scope="session")
def _require_shared_bind_mount(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Skip the mount-dependent tests unless the daemon can bind-mount the test tmp.

    A VM-backed daemon (colima / Docker Desktop) only shares a configured set of
    host paths into its Linux VM; the pytest temp root (``/var/folders`` on macOS)
    is typically NOT among them, so a bind mount of it resolves to an empty dir.
    Native Linux docker (GitHub Actions) shares everything, so this is a no-op
    there and the tests run for real.

    The probe is a RAW ``docker run -v`` round-trip — independent of
    :class:`ContainerLauncher` — so it isolates "the env can't share this path"
    (→ skip) from "the launcher's mount is broken on an env that CAN share"
    (→ the launcher tests still fail and surface the bug, not skipped).
    """
    probe_dir = tmp_path_factory.mktemp("mount_probe")
    (probe_dir / "sentinel").write_text("mount-ok")
    res = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{probe_dir}:/probe:ro", _E2E_IMAGE,
         "cat", "/probe/sentinel"],
        capture_output=True,
        timeout=180,
        check=False,
    )
    if res.returncode != 0 or res.stdout.strip() != b"mount-ok":
        pytest.skip(
            "docker daemon cannot bind-mount the pytest temp dir (VM file-sharing "
            "excludes it — typical for colima / Docker Desktop with /var/folders). "
            "Runs on native Linux docker (e.g. GitHub Actions), where the mount "
            "round-trips."
        )


@pytest.fixture
def mount_container(_require_shared_bind_mount, tmp_path: Path):
    """Launch a real mount-mode container over a host workspace; tear down after.

    Yields ``(backend, ws_root, container_id)`` where ``ws_root`` is the HOST
    workspace dir bind-mounted at ``/workspace`` and ``backend`` is the real
    :class:`DockerEnvironmentBackend` over it (repo_dir=/workspace).
    """
    ws_root = tmp_path / "ws"
    (ws_root / ".reyn").mkdir(parents=True)

    launcher = ContainerLauncher()
    config = LaunchConfig(workspace_root=str(ws_root), image=_E2E_IMAGE)
    container_id = launcher.launch(config)
    backend = DockerEnvironmentBackend(
        container=container_id, repo_dir=WORKSPACE_DEST_DEFAULT
    )
    try:
        yield backend, ws_root, container_id
    finally:
        launcher.teardown(container_id)


def test_bind_mount_host_to_container_visibility(mount_container) -> None:
    """Tier 2c: a file written on the HOST workspace is read in-container via the mount."""
    backend, ws_root, _ = mount_container

    (ws_root / "probe_host.txt").write_bytes(b"from-host")

    seen = backend.read_bytes(Path(WORKSPACE_DEST_DEFAULT) / "probe_host.txt")
    assert seen == b"from-host"


def test_state_dir_coherence_container_to_host(mount_container) -> None:
    """Tier 2c: an in-container write to /workspace/.reyn lands on host workspace_root/.reyn.

    This is the part2 coherence claim — host ``workspace_root/.reyn`` and
    in-container ``/workspace/.reyn`` are the same physical dir, so OS state
    (events/approvals/index) written by either side is visible to the other.
    """
    backend, ws_root, _ = mount_container

    backend.write_bytes(
        Path(WORKSPACE_DEST_DEFAULT) / ".reyn" / "probe_container.txt",
        b"from-container",
    )

    host_path = ws_root / ".reyn" / "probe_container.txt"
    assert host_path.exists()
    assert host_path.read_bytes() == b"from-container"


def test_teardown_removes_container(tmp_path: Path) -> None:
    """Tier 2c: teardown removes the launched container (no leak)."""
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    launcher = ContainerLauncher()
    container_id = launcher.launch(
        LaunchConfig(workspace_root=str(ws_root), image=_E2E_IMAGE)
    )

    try:
        assert launcher.teardown(container_id) is True
        # The real no-leak contract: after teardown the container is gone. (We do
        # NOT assert a second teardown's return value — `docker rm -f` of a
        # missing id is success (rc 0) on some engines and failure on others;
        # pinning it would be a version-specific format-pin.)
        assert not _container_exists(container_id)
    finally:
        # Bulletproof: if an assert above failed mid-way, force-remove so a
        # launched container never leaks into the CI host.
        launcher.teardown(container_id)


@pytest.mark.asyncio
async def test_security_non_root(mount_container) -> None:
    """Tier 2c: the launched container runs as a non-root user (--user / --cap-drop baseline)."""
    backend, _, _ = mount_container
    res = await backend.run(["id", "-u"], SandboxPolicy(timeout_seconds=30))
    assert res.returncode == 0
    assert res.stdout.strip() != b"0"


@pytest.mark.asyncio
async def test_security_network_off(mount_container) -> None:
    """Tier 2c: outbound network is blocked (--network none)."""
    backend, _, _ = mount_container
    res = await backend.run(
        [
            "python3",
            "-c",
            "import socket; socket.setdefaulttimeout(3); "
            "socket.create_connection(('1.1.1.1', 53))",
        ],
        SandboxPolicy(timeout_seconds=30),
    )
    assert res.returncode != 0


@pytest.mark.asyncio
async def test_security_readonly_rootfs_with_tmpfs_and_mount(mount_container) -> None:
    """Tier 2c: rootfs is read-only; /tmp (tmpfs) and /workspace (rw mount) are writable."""
    backend, _, _ = mount_container
    policy = SandboxPolicy(timeout_seconds=30)

    # Root filesystem is read-only → a write to / fails.
    ro = await backend.run(["sh", "-c", "echo x > /rootfile"], policy)
    assert ro.returncode != 0

    # tmpfs /tmp is writable.
    tmp = await backend.run(["sh", "-c", "echo x > /tmp/probe"], policy)
    assert tmp.returncode == 0

    # The rw workspace bind mount is writable.
    ws = await backend.run(
        ["sh", "-c", f"echo x > {WORKSPACE_DEST_DEFAULT}/probe_rw"], policy
    )
    assert ws.returncode == 0
