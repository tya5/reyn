"""Tier 2: #1477 — RouterHostAdapter.get_cwd() is sandbox-aware.

When an environment backend is configured (e.g. DockerEnvironmentBackend),
get_cwd() returns the in-container path (backend.repo_dir) rather than the
host's os.getcwd(). Without this fix, the SP Environment section shows the
host path while FS/exec ops run against the container repo_dir — a frame
mismatch that leaks host paths into the agent's context.

No mocks. Real-construct fakes (pure subclass, no MagicMock).
"""
from __future__ import annotations

import os
from pathlib import Path

from tests._support.router_host_adapter import make_adapter as _make_adapter

# ── Real fake backends ───────────────────────────────────────────────────────


class _FakeContainerBackend:
    """Real fake for DockerEnvironmentBackend: exposes repo_dir only."""

    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = repo_dir


class _FakeHostBackend:
    """Real fake for HostBackend: no repo_dir attribute."""
    pass


# ── Tests ────────────────────────────────────────────────────────────────────


def test_get_cwd_with_container_backend_returns_repo_dir(tmp_path: Path) -> None:
    """Tier 2: #1477 — when environment_backend has repo_dir (ContainerBackend),
    get_cwd() returns the container path, not the host cwd."""
    container_path = "/testbed"
    backend = _FakeContainerBackend(repo_dir=container_path)
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    assert adapter.get_cwd() == container_path


def test_get_cwd_with_host_backend_returns_os_getcwd(tmp_path: Path) -> None:
    """Tier 2: #1477 — when environment_backend has no repo_dir (HostBackend),
    get_cwd() falls back to os.getcwd() — existing behaviour preserved."""
    backend = _FakeHostBackend()
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    assert adapter.get_cwd() == os.getcwd()


def test_get_cwd_with_no_backend_returns_os_getcwd(tmp_path: Path) -> None:
    """Tier 2: #1477 — when no environment_backend is set (None), get_cwd()
    returns os.getcwd() — backward-compat for host-only sessions."""
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
    )
    assert adapter.get_cwd() == os.getcwd()


def test_get_cwd_container_differs_from_host(tmp_path: Path) -> None:
    """Tier 2: #1477 — falsification pair: container path != host cwd.
    Confirms the fix actually changes the value (not a no-op)."""
    container_path = "/testbed"
    backend = _FakeContainerBackend(repo_dir=container_path)
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    assert adapter.get_cwd() == container_path
    assert adapter.get_cwd() != os.getcwd()
