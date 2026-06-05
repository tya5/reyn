"""Tier 2: `reyn run` container-backend flags (FP-0008 #1115 Stage 2 + #1324 mount).

`reyn run --env-backend=docker` either:
  - ATTACHES to a running container (`--container` + `--repo-dir`) — the #1115
    Stage 2 behavior; or
  - LAUNCHES a mount-mode container (`--container` omitted) — the #1324 path:
    reyn starts a security-hardened container with the workspace bind-mounted at
    /workspace and returns a teardown cleanup (unless --keep-container).

Both build a single DockerEnvironmentBackend that run.py injects at BOTH the FS
seam and the exec seam (agent-level uniform). host (default) keeps identity
behavior. Flags are generic (no skill-specific knowledge, P7).

No mocks; real argparse.Namespace + real DockerEnvironmentBackend (no docker
daemon touched — attach is construction-only; launch uses an injected fake
launcher). Docstrings open "Tier 2:".
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from reyn.cli.commands.run import _build_environment_backend


def _args(**kw) -> argparse.Namespace:
    base = {
        "env_backend": "host",
        "container": None,
        "repo_dir": None,
        "state_dir": None,
        "image": None,
        "mounts": None,
        "keep_container": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


class _FakeLauncher:
    """Records launch configs + teardown calls; never touches Docker (no mocks)."""

    def __init__(self, cid: str = "fakecid") -> None:
        self.cid = cid
        self.launched: list = []
        self.torn_down: list[str] = []

    def launch(self, config, *, timeout: int = 120) -> str:
        self.launched.append(config)
        return self.cid

    def teardown(self, container_id: str, *, timeout: int = 60) -> bool:
        self.torn_down.append(container_id)
        return True


# ─── host (default) ────────────────────────────────────────────────────────────


def test_host_backend_returns_none_quad() -> None:
    """Tier 2: --env-backend=host yields no backend + default dirs + no cleanup."""
    backend, base_dir, state_dir, cleanup = _build_environment_backend(
        _args(env_backend="host")
    )
    assert (backend, base_dir, state_dir, cleanup) == (None, None, None, None)


def test_default_is_host() -> None:
    """Tier 2: a Namespace without env_backend defaults to host (getattr fallback)."""
    backend, base_dir, state_dir, cleanup = _build_environment_backend(
        argparse.Namespace()
    )
    assert (backend, base_dir, state_dir, cleanup) == (None, None, None, None)


# ─── docker ATTACH (--container given) ─────────────────────────────────────────


def test_docker_attach_builds_backend_with_dirs() -> None:
    """Tier 2: --container + --repo-dir attaches; maps dirs; no teardown (operator-owned)."""
    backend, base_dir, state_dir, cleanup = _build_environment_backend(
        _args(
            env_backend="docker",
            container="reyn_inst_1",
            repo_dir="/testbed",
            state_dir="/host/state",
        )
    )
    assert backend is not None
    assert backend.container == "reyn_inst_1"
    assert backend.repo_dir == "/testbed"
    assert base_dir == Path("/testbed")
    assert state_dir == Path("/host/state")
    assert cleanup is None


def test_docker_attach_satisfies_both_protocols() -> None:
    """Tier 2: the single docker instance satisfies BOTH the FS and exec Protocols."""
    from reyn.environment import EnvironmentBackend
    from reyn.sandbox import SandboxBackend

    backend, _b, _s, _c = _build_environment_backend(
        _args(env_backend="docker", container="c", repo_dir="/testbed", state_dir="/h")
    )
    assert isinstance(backend, EnvironmentBackend), "must satisfy the FS seam Protocol"
    assert isinstance(backend, SandboxBackend), "must satisfy the exec seam Protocol"


def test_docker_attach_without_state_dir_keeps_base_dir(capsys) -> None:
    """Tier 2: --state-dir omitted (attach/baked-repo) → state_dir None + a warning
    that OS state lands on the in-container repo FS."""
    backend, base_dir, state_dir, _c = _build_environment_backend(
        _args(env_backend="docker", container="c", repo_dir="/testbed")
    )
    assert backend is not None
    assert base_dir == Path("/testbed")
    assert state_dir is None
    assert "without --state-dir" in capsys.readouterr().err


def test_docker_attach_missing_repo_dir_exits() -> None:
    """Tier 2: --container without --repo-dir is a clean exit."""
    with pytest.raises(SystemExit):
        _build_environment_backend(_args(env_backend="docker", container="c"))


# ─── docker LAUNCH (--container omitted, #1324) ────────────────────────────────


def test_docker_launch_builds_backend_and_cleanup() -> None:
    """Tier 2: omitting --container launches a mount-mode container at /workspace
    and wires a teardown cleanup (non-persistent default)."""
    fake = _FakeLauncher()
    backend, base_dir, state_dir, cleanup = _build_environment_backend(
        _args(env_backend="docker"), launcher=fake
    )
    assert backend is not None
    assert backend.container == "fakecid"
    assert backend.repo_dir == "/workspace"
    assert base_dir == Path("/workspace")
    (_launched_cfg,) = fake.launched  # exactly one launch
    assert callable(cleanup)
    cleanup()
    assert fake.torn_down == ["fakecid"]


def test_docker_launch_defaults_state_dir_to_workspace_root_reyn(tmp_path, monkeypatch) -> None:
    """Tier 2: part2 — launch without --state-dir defaults state_dir to the HOST
    workspace_root/.reyn (the bind-mounted dir → coherent with /workspace/.reyn).

    Deterministic: chdir into a tmp project root carrying a reyn.yaml so
    _find_project_root resolves to tmp_path (not the ambient cwd)."""
    (tmp_path / "reyn.yaml").write_text("")
    monkeypatch.chdir(tmp_path)
    fake = _FakeLauncher()
    _b, _bd, state_dir, _c = _build_environment_backend(
        _args(env_backend="docker"), launcher=fake
    )
    assert state_dir == tmp_path.resolve() / ".reyn"


def test_docker_launch_state_dir_falls_back_to_cwd_without_reyn_yaml(
    tmp_path, monkeypatch
) -> None:
    """Tier 2: part2/bug1 — with no reyn.yaml up the tree _find_project_root
    returns None; state_dir must fall back to cwd/.reyn (a real host path), NOT
    the bogus 'None/.reyn' that str(None) produced. Reproduce-first for the
    #1328-origin latent bug part2 exposed."""
    monkeypatch.chdir(tmp_path)  # tmp_path has no reyn.yaml here or above
    fake = _FakeLauncher()
    _b, _bd, state_dir, _c = _build_environment_backend(
        _args(env_backend="docker"), launcher=fake
    )
    assert state_dir == tmp_path.resolve() / ".reyn"
    assert "None" not in str(state_dir)


def test_docker_launch_explicit_state_dir_wins() -> None:
    """Tier 2: part2 — an explicit --state-dir overrides the mount-coherent default."""
    fake = _FakeLauncher()
    _b, _bd, state_dir, _c = _build_environment_backend(
        _args(env_backend="docker", state_dir="/host/s"), launcher=fake
    )
    assert state_dir == Path("/host/s")


def test_docker_launch_uses_image_and_mounts() -> None:
    """Tier 2: --image + --mount thread into the LaunchConfig."""
    fake = _FakeLauncher()
    _build_environment_backend(
        _args(env_backend="docker", image="myimg:1", mounts=["/d:/m:ro"]),
        launcher=fake,
    )
    (cfg,) = fake.launched
    assert cfg.image == "myimg:1"
    (mount,) = cfg.mounts
    assert (mount.host, mount.container, mount.mode) == ("/d", "/m", "ro")


def test_docker_launch_keep_container_skips_teardown() -> None:
    """Tier 2: --keep-container → persistent config + no teardown cleanup."""
    fake = _FakeLauncher()
    _b, _bd, _sd, cleanup = _build_environment_backend(
        _args(env_backend="docker", keep_container=True), launcher=fake
    )
    assert cleanup is None
    (cfg,) = fake.launched
    assert cfg.persistent is True


def test_docker_launch_bad_mount_exits() -> None:
    """Tier 2: a malformed --mount spec is a clean exit (before any launch)."""
    fake = _FakeLauncher()
    with pytest.raises(SystemExit):
        _build_environment_backend(
            _args(env_backend="docker", mounts=["bogus"]), launcher=fake
        )
    assert fake.launched == []  # rejected before launching


# ─── docker LAUNCH devcontainer awareness (#1324 b) ────────────────────────────


def _write_devcontainer(tmp_path, body: str) -> None:
    (tmp_path / ".devcontainer").mkdir(exist_ok=True)
    (tmp_path / ".devcontainer" / "devcontainer.json").write_text(body, encoding="utf-8")
    (tmp_path / "reyn.yaml").write_text("")  # so _find_project_root resolves here


def test_docker_launch_reads_devcontainer_image(tmp_path, monkeypatch) -> None:
    """Tier 2: #1324b — a workspace devcontainer.json image seeds the LaunchConfig
    when no --image is given."""
    _write_devcontainer(tmp_path, '{"image": "dcimg:1", "postCreateCommand": "make"}')
    monkeypatch.chdir(tmp_path)
    fake = _FakeLauncher()
    _build_environment_backend(_args(env_backend="docker"), launcher=fake)
    (cfg,) = fake.launched
    assert cfg.image == "dcimg:1"
    assert cfg.setup_command == "make"


def test_docker_launch_cli_image_overrides_devcontainer(tmp_path, monkeypatch) -> None:
    """Tier 2: #1324b — an explicit --image overrides the devcontainer image."""
    _write_devcontainer(tmp_path, '{"image": "dcimg:1"}')
    monkeypatch.chdir(tmp_path)
    fake = _FakeLauncher()
    _build_environment_backend(
        _args(env_backend="docker", image="cli:override"), launcher=fake
    )
    (cfg,) = fake.launched
    assert cfg.image == "cli:override"


def test_docker_launch_build_based_devcontainer_warns(tmp_path, monkeypatch, capsys) -> None:
    """Tier 2: #1324b — a build-based devcontainer warns + falls back to the default
    image (build-based support is a tracked follow-up)."""
    from reyn.environment.container_launcher import DEFAULT_IMAGE

    _write_devcontainer(tmp_path, '{"build": {"dockerfile": "Dockerfile"}}')
    monkeypatch.chdir(tmp_path)
    fake = _FakeLauncher()
    _build_environment_backend(_args(env_backend="docker"), launcher=fake)
    (cfg,) = fake.launched
    assert cfg.image == DEFAULT_IMAGE
    assert "build-based devcontainer" in capsys.readouterr().err
