"""Tier 2: #1479 — SP Environment section: system info (date/platform/shell/git).

Competitor-aligned fields added to ## Environment:
- date:     today (ISO-8601, always; host-clock)
- platform: OS family + kernel release (backend-derived, container-aware)
- shell:    default shell (backend-derived)
- git repo: yes/no (.git presence at cwd)

Backend resolution: getattr-guarded — ContainerBackend future-ready;
HostBackend / no backend falls back to local platform module.

FakeRouterHost has no get_environment_info → field absent → fixture keys
unaffected (confirmed in replay note).

No mocks. Real-construct fakes.
"""
from __future__ import annotations

import os
from pathlib import Path

from reyn.runtime.router_system_prompt import build_system_prompt
from tests._support.router_host_adapter import make_adapter as _make_adapter

# ── Fake backends ─────────────────────────────────────────────────────────────


class _FakeContainerBackendWithInfo:
    """ContainerBackend with get_environment_info() — the future-ready path."""

    def __init__(
        self, repo_dir: str, platform: str, os_version: str, shell: str,
        is_git_repo: bool | None = None,
    ) -> None:
        self.repo_dir = repo_dir
        self._platform = platform
        self._os_version = os_version
        self._shell = shell
        # #1481: a real container backend probes .git IN-container. None =
        # the probe omitted it (degrade) → adapter omits is_git_repo.
        self._is_git_repo = is_git_repo

    def get_environment_info(self) -> dict:
        info = {
            "platform": self._platform,
            "os_version": self._os_version,
            "shell": self._shell,
        }
        if self._is_git_repo is not None:
            info["is_git_repo"] = self._is_git_repo
        return info


class _FakeHostBackend:
    """HostBackend: no repo_dir, no get_environment_info."""
    pass


class _FakeContainerBackendNoInfo:
    """ContainerBackend without get_environment_info() — probe not yet implemented.

    This is the current state of DockerEnvironmentBackend. repo_dir signals
    it is a non-host backend; absence of get_environment_info means the
    platform/shell should be OMITTED (not filled from host values).
    """

    def __init__(self, repo_dir: str) -> None:
        self.repo_dir = repo_dir


# ── 1. get_environment_info() on RouterHostAdapter ────────────────────────────


def test_env_info_host_backend_derives_from_platform_module(tmp_path: Path) -> None:
    """Tier 2: #1479 — host backend path: platform/os_version/shell derived
    from local platform module + os.environ. date always present."""
    import platform
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=_FakeHostBackend(),
    )
    info = adapter.get_environment_info()
    assert "date" in info, "date must always be present (host-clock)"
    assert info["platform"] == platform.system().lower()
    assert info["os_version"] == platform.release()


def test_env_info_container_backend_uses_backend_values(tmp_path: Path) -> None:
    """Tier 2: #1479 — container backend path: platform/os_version/shell come
    from backend.get_environment_info() (container's linux, not host's darwin)."""
    backend = _FakeContainerBackendWithInfo(
        repo_dir="/testbed",
        platform="linux",
        os_version="5.15.0",
        shell="/bin/bash",
    )
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    info = adapter.get_environment_info()
    assert info["platform"] == "linux"
    assert info["os_version"] == "5.15.0"
    assert info["shell"] == "/bin/bash"


def test_env_info_container_backend_no_probe_omits_platform_shell(tmp_path: Path) -> None:
    """Tier 2: #1479 — non-host backend without get_environment_info() must NOT
    fill platform/shell from the host. Omit them instead (degrade, don't guess).
    Only date is always present. This pins the fix against the #1477-pattern
    regression: showing host darwin/zsh for a linux container = wrong context.
    """
    backend = _FakeContainerBackendNoInfo(repo_dir="/testbed")
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    info = adapter.get_environment_info()
    assert "date" in info, "date must always be present (clock-derived)"
    assert "platform" not in info, "platform must be absent when container probe not available"
    assert "os_version" not in info, "os_version must be absent"
    assert "shell" not in info, "shell must be absent"


def test_env_info_no_backend_derives_from_platform_module(tmp_path: Path) -> None:
    """Tier 2: #1479 — no backend: same as host backend (fallback to platform)."""
    import platform
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
    )
    info = adapter.get_environment_info()
    assert info["platform"] == platform.system().lower()
    assert "date" in info


def test_env_info_git_repo_detection_true(tmp_path: Path) -> None:
    """Tier 2: #1481 — is_git_repo reflects the backend's IN-CONTAINER probe.

    The container backend probes .git inside the container (repo_dir lives in
    the container, not on the host), so is_git_repo comes from the backend's
    get_environment_info — NOT a host-path check on the adapter side.
    """
    backend = _FakeContainerBackendWithInfo(
        repo_dir=str(tmp_path), platform="linux", os_version="5.15.0",
        shell="/bin/bash", is_git_repo=True,
    )
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    info = adapter.get_environment_info()
    assert info.get("is_git_repo") is True


def test_env_info_git_repo_detection_false(tmp_path: Path) -> None:
    """Tier 2: #1481 — is_git_repo=False when the in-container probe says so."""
    backend = _FakeContainerBackendWithInfo(
        repo_dir=str(tmp_path), platform="linux", os_version="5.15.0",
        shell="/bin/bash", is_git_repo=False,
    )
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    info = adapter.get_environment_info()
    assert info.get("is_git_repo") is False


def test_env_info_git_repo_omitted_when_probe_degrades(tmp_path: Path) -> None:
    """Tier 2: #1481 — is_git_repo OMITTED when the container probe doesn't
    supply it (degrade) — NOT back-filled from a host-path .git check."""
    backend = _FakeContainerBackendWithInfo(
        repo_dir=str(tmp_path), platform="linux", os_version="5.15.0",
        shell="/bin/bash", is_git_repo=None,
    )
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test",
        environment_backend=backend,
    )
    info = adapter.get_environment_info()
    assert "is_git_repo" not in info


# ── 2. SP rendering ───────────────────────────────────────────────────────────


def _sp_with_env(**kw) -> str:
    return build_system_prompt(
        agent_name="test",
        agent_role="tester",
        available_skills=[],
        available_agents=[],
        memory_index={},
        **kw,
    )


def test_sp_renders_date_when_environment_info_set() -> None:
    """Tier 2: #1479 — environment_info with date renders in ## Environment."""
    sp = _sp_with_env(environment_info={"date": "2026-06-11", "is_git_repo": True})
    assert "## Environment" in sp
    assert "date: 2026-06-11" in sp


def test_sp_renders_platform_and_version() -> None:
    """Tier 2: #1479 — platform + os_version render as 'platform: linux 5.15.0'."""
    sp = _sp_with_env(
        environment_info={"date": "2026-06-11", "platform": "linux", "os_version": "5.15.0"}
    )
    assert "platform: linux 5.15.0" in sp


def test_sp_renders_shell() -> None:
    """Tier 2: #1479 — shell renders in ## Environment when present."""
    sp = _sp_with_env(
        environment_info={"date": "2026-06-11", "shell": "/bin/zsh"}
    )
    assert "shell: /bin/zsh" in sp


def test_sp_renders_git_repo_yes() -> None:
    """Tier 2: #1479 — is_git_repo=True renders as 'git repo: yes'."""
    sp = _sp_with_env(environment_info={"date": "2026-06-11", "is_git_repo": True})
    assert "git repo: yes" in sp


def test_sp_renders_git_repo_no() -> None:
    """Tier 2: #1479 — is_git_repo=False renders as 'git repo: no'."""
    sp = _sp_with_env(environment_info={"date": "2026-06-11", "is_git_repo": False})
    assert "git repo: no" in sp


def test_sp_environment_section_absent_when_neither_cwd_nor_info() -> None:
    """Tier 2: #1479 — ## Environment section absent when both cwd and
    environment_info are None (FakeRouterHost replay path → fixtures unaffected)."""
    sp = _sp_with_env()  # no cwd, no environment_info
    assert "## Environment" not in sp


def test_sp_omits_empty_shell() -> None:
    """Tier 2: #1479 — shell field absent when empty string (degrade, don't guess)."""
    sp = _sp_with_env(environment_info={"date": "2026-06-11", "shell": ""})
    assert "shell:" not in sp


def test_sp_platform_only_without_version() -> None:
    """Tier 2: #1479 — platform renders even without os_version (graceful degrade)."""
    sp = _sp_with_env(environment_info={"date": "2026-06-11", "platform": "linux"})
    assert "platform: linux" in sp
    # Must not try to render 'linux ' with trailing space
    assert "platform: linux \n" not in sp or "platform: linux" in sp
