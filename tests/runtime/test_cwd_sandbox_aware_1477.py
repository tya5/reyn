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
    adapter = _make_adapter(universal_wrappers_enabled=False,  # #4159: not exercised by this test
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    assert adapter.get_cwd() == container_path


def test_get_cwd_with_host_backend_returns_os_getcwd(tmp_path: Path) -> None:
    """Tier 2: #1477 — when environment_backend has no repo_dir (HostBackend),
    get_cwd() falls back to os.getcwd() — existing behaviour preserved."""
    backend = _FakeHostBackend()
    adapter = _make_adapter(universal_wrappers_enabled=False,  # #4159: not exercised by this test
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    assert adapter.get_cwd() == os.getcwd()


def test_get_cwd_with_no_backend_returns_os_getcwd(tmp_path: Path) -> None:
    """Tier 2: #1477 — when no environment_backend is set (None), get_cwd()
    returns os.getcwd() — backward-compat for host-only sessions."""
    adapter = _make_adapter(universal_wrappers_enabled=False,  # #4159: not exercised by this test
        agent_workspace_dir=tmp_path / "agents" / "test",
    )
    assert adapter.get_cwd() == os.getcwd()


def test_get_cwd_container_differs_from_host(tmp_path: Path) -> None:
    """Tier 2: #1477 — falsification pair: container path != host cwd.
    Confirms the fix actually changes the value (not a no-op)."""
    container_path = "/testbed"
    backend = _FakeContainerBackend(repo_dir=container_path)
    adapter = _make_adapter(universal_wrappers_enabled=False,  # #4159: not exercised by this test
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    assert adapter.get_cwd() == container_path
    assert adapter.get_cwd() != os.getcwd()


def test_get_cwd_with_host_backend_uses_workspace_base_dir_over_process_cwd(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: #4204 bucket D — when a HostBackend is present AND the
    session's real workspace_base_dir differs from the process's raw cwd
    (the subdirectory-launch case this issue tracks), get_cwd() reports
    the base_dir — matching what sandboxed_exec.py's real op actually
    anchors its subprocess cwd on (``ctx.workspace.base_dir``) — not the
    process cwd the SP would otherwise show.

    STRIP-FALSIFY: reverting get_cwd()'s workspace_base_dir_fn branch
    (the pre-#4204 form) makes this go RED — it would return the
    subdirectory (process cwd) instead of the real project root."""
    project_root = tmp_path
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    monkeypatch.chdir(subdir)  # the operator launched reyn from here

    backend = _FakeHostBackend()
    adapter = _make_adapter(
        universal_wrappers_enabled=False,
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
        workspace_base_dir=project_root,
    )
    assert adapter.get_cwd() == str(project_root)
    assert adapter.get_cwd() != os.getcwd()  # os.getcwd() is the subdir


def test_get_cwd_falls_back_to_os_getcwd_when_base_dir_unresolvable(
    tmp_path: Path,
) -> None:
    """Tier 2: #4204 bucket D — when no workspace_base_dir supplier is wired
    at all (test hosts / adapters built without one), get_cwd() falls back
    to os.getcwd() — the pre-#4204 behavior is preserved as the defensive
    floor, not silently broken for callers that don't supply one."""
    backend = _FakeHostBackend()
    adapter = _make_adapter(
        universal_wrappers_enabled=False,
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
        # workspace_base_dir intentionally omitted (None) — no supplier wired.
    )
    assert adapter.get_cwd() == os.getcwd()
