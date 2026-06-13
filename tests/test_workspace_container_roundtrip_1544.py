"""Tier 2c: real-docker container shadow-git round-trip (#1544 increment-2).

The REAL-backend verification of the container path: a live container (git+bash
image) with the workspace bind-mounted at /workspace, exercising a real
``_ContainerGitRunner`` capture→restore via ``docker exec``. This is the only
real test of the container path — the (B) tmux gate is HOST-only (host git) and
never touches ``_ContainerGitRunner``; Fake-backend unit tests don't run real
docker-exec-git ([[feedback_fake_backend_unit_misses_real_integration]]).

Skipped without a reachable Docker daemon. Also skipped when the daemon can't
bind-mount the pytest temp (colima / Docker Desktop don't share /var/folders) —
runs for real on native-Linux docker (CI). Locally verified once on 2026-06-13
(macOS, repo-relative mount): capture→restore reverted the workspace file,
removed a later-added file, and preserved .reyn — ROUND-TRIP PASS.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# import reyn.workspace first to pre-resolve the pre-existing environment/workspace
# import cycle, so this module collects in isolation (not just in full-suite order).
import reyn.workspace  # noqa: F401
from reyn.environment.container_backend import DockerEnvironmentBackend
from reyn.environment.container_launcher import (
    WORKSPACE_DEST_DEFAULT,
    ContainerLauncher,
    LaunchConfig,
)
from reyn.events.workspace_version_store import WorkspaceVersionStore, _ContainerGitRunner

# Production uses reyn-base (python:3.12-slim + git); python:3.12 (full) ships
# git+bash too and needs no build, so the path is exercised image-agnostically.
_GIT_IMAGE = "python:3.12"

_DOCKER_AVAILABLE = DockerEnvironmentBackend(container="_probe", repo_dir="/").available()

pytestmark = pytest.mark.skipif(
    not _DOCKER_AVAILABLE, reason="no reachable Docker daemon",
)


@pytest.fixture
def _require_shared_bind_mount(tmp_path: Path) -> None:
    """Skip unless the daemon can bind-mount the pytest temp (raw docker -v probe).

    VM-backed daemons (colima / Docker Desktop) only share configured host paths;
    the pytest temp (/var/folders on macOS) is typically excluded → a bind mount
    resolves empty. Native-Linux docker (CI) shares everything → runs for real.
    """
    (tmp_path / "sentinel").write_text("mount-ok")
    res = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{tmp_path}:/probe:ro", _GIT_IMAGE,
         "cat", "/probe/sentinel"],
        capture_output=True, timeout=300, check=False,
    )
    if res.returncode != 0 or res.stdout.strip() != b"mount-ok":
        pytest.skip(
            "docker daemon cannot bind-mount the pytest temp (VM file-sharing "
            "excludes /var/folders — colima / Docker Desktop). Runs on native "
            "Linux docker (CI). Locally verified once via a repo-relative mount."
        )


@pytest.mark.asyncio
async def test_container_capture_restore_round_trip(_require_shared_bind_mount, tmp_path):
    """Tier 2c: real in-container git capture→restore reverts the workspace as-of-N.

    Mount-mode: ws_root bind-mounted at /workspace; git runs in-container via
    _ContainerGitRunner (docker exec) against the container path context, while
    the store's small FS surface (info/exclude) is on the host git-dir (the
    bind-mount source, visible in-container).
    """
    ws_root = tmp_path / "ws"
    (ws_root / ".reyn").mkdir(parents=True)
    (ws_root / ".reyn" / "wal.jsonl").write_text("os-state", encoding="utf-8")
    (ws_root / "file.txt").write_text("v1", encoding="utf-8")

    launcher = ContainerLauncher()
    container_id = launcher.launch(LaunchConfig(workspace_root=str(ws_root), image=_GIT_IMAGE))
    backend = DockerEnvironmentBackend(container=container_id, repo_dir=WORKSPACE_DEST_DEFAULT)
    try:
        store = WorkspaceVersionStore(
            ws_root, ws_root / ".reyn" / "workspace-shadow.git",
            git_runner=_ContainerGitRunner(
                backend,
                git_dir=f"{WORKSPACE_DEST_DEFAULT}/.reyn/workspace-shadow.git",
                work_tree=WORKSPACE_DEST_DEFAULT,
            ),
        )
        assert await store.capture(10) is not None        # real in-container commit
        (ws_root / "file.txt").write_text("v2", encoding="utf-8")
        (ws_root / "added.txt").write_text("new", encoding="utf-8")
        await store.capture(20)
        assert await store.seqs() == [10, 20]

        await store.restore_to_seq(10)

        assert (ws_root / "file.txt").read_text() == "v1"         # reverted in-container
        assert not (ws_root / "added.txt").exists()                # later-added removed
        assert (ws_root / ".reyn" / "wal.jsonl").read_text() == "os-state"  # OS state survived
    finally:
        launcher.teardown(container_id)
