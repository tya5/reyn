"""Tier 2: #1341 — build-based devcontainer support (build-on-demand).

Pins the launcher-level contract for building a build-based devcontainer image:
- ``_map_devcontainer`` extracts the build spec (dockerFile legacy + build object
  forms) and resolves paths relative to the devcontainer.json dir; compose is
  build_based but NOT buildable (out of scope).
- ``_devcontainer_image_tag`` is content-addressed (deterministic; changes on
  Dockerfile content / build_args / target).
- ``_docker_build`` emits the expected ``docker build`` argv.
- ``launch()`` builds (inspect-then-build) before run for a build config, skips
  the build when the tag is already present, and surfaces build failures.

Uses a real injectable fake runner (no mocks), mirroring test_container_launcher.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.environment.container_launcher import (
    REYN_BASE_IMAGE,
    BuildSpec,
    ContainerLauncher,
    LaunchConfig,
    _devcontainer_image_tag,
    _map_devcontainer,
    load_devcontainer_config,
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


def _err(stderr: bytes = b"boom") -> SandboxResult:
    return SandboxResult(returncode=1, stdout=b"", stderr=stderr)


# ─── _map_devcontainer: build-spec extraction ─────────────────────────────────


def test_map_object_build_extracts_spec(tmp_path) -> None:
    """Tier 2: #1341 — `build` object form extracts dockerfile/context/args/target,
    resolved relative to the devcontainer dir; buildable=True."""
    data = {
        "build": {
            "dockerfile": "Dockerfile",
            "context": "..",
            "args": {"VARIANT": "3.12", "X": 1},
            "target": "dev",
        }
    }
    cfg = _map_devcontainer(data, tmp_path)
    assert cfg.build_based is True
    assert cfg.buildable is True
    assert cfg.dockerfile == str((tmp_path / "Dockerfile").resolve())
    assert cfg.build_context == str((tmp_path / "..").resolve())
    assert cfg.build_args == {"VARIANT": "3.12", "X": "1"}  # values coerced to str
    assert cfg.build_target == "dev"


def test_map_legacy_dockerfile(tmp_path) -> None:
    """Tier 2: #1341 — legacy top-level `dockerFile` (+ optional `context`) form."""
    cfg = _map_devcontainer({"dockerFile": "build/Dockerfile", "context": "."}, tmp_path)
    assert cfg.buildable is True
    assert cfg.dockerfile == str((tmp_path / "build/Dockerfile").resolve())
    assert cfg.build_context == str((tmp_path / ".").resolve())


def test_map_compose_is_build_based_not_buildable(tmp_path) -> None:
    """Tier 2: #1341 — dockerComposeFile is build_based but NOT buildable (out of
    scope for the single-container launcher → caller warns + falls back)."""
    cfg = _map_devcontainer({"dockerComposeFile": "docker-compose.yml"}, tmp_path)
    assert cfg.build_based is True
    assert cfg.buildable is False
    assert cfg.dockerfile is None


def test_map_image_based_not_build_based(tmp_path) -> None:
    """Tier 2: #1341 — a plain image devcontainer stays non-build."""
    cfg = _map_devcontainer({"image": "python:3.12-slim"}, tmp_path)
    assert cfg.build_based is False
    assert cfg.buildable is False
    assert cfg.image == "python:3.12-slim"


def test_load_resolves_relative_to_devcontainer_dir(tmp_path) -> None:
    """Tier 2: #1341 — paths resolve relative to the .devcontainer/ location."""
    dc_dir = tmp_path / ".devcontainer"
    dc_dir.mkdir()
    (dc_dir / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (dc_dir / "devcontainer.json").write_text('{"build": {"dockerfile": "Dockerfile"}}')
    cfg = load_devcontainer_config(str(tmp_path))
    assert cfg is not None and cfg.buildable
    assert cfg.dockerfile == str((dc_dir / "Dockerfile").resolve())


# ─── content-addressed tag ────────────────────────────────────────────────────


def test_image_tag_deterministic_and_content_addressed(tmp_path) -> None:
    """Tier 2: #1341 — tag is deterministic for the same inputs and changes when
    the Dockerfile content / build_args / target change (F2)."""
    df = tmp_path / "Dockerfile"
    df.write_text("FROM python:3.12-slim\n")
    spec = BuildSpec(dockerfile=str(df), context=str(tmp_path))
    t0 = _devcontainer_image_tag(spec)
    assert t0.startswith("reyn-dc-") and t0.endswith(":local")
    assert _devcontainer_image_tag(spec) == t0  # deterministic
    # content change → new tag
    df.write_text("FROM python:3.12-slim\nRUN echo hi\n")
    assert _devcontainer_image_tag(spec) != t0
    # args / target change → new tag
    base = BuildSpec(dockerfile=str(df), context=str(tmp_path))
    assert _devcontainer_image_tag(
        BuildSpec(dockerfile=str(df), context=str(tmp_path), build_args={"A": "1"})
    ) != _devcontainer_image_tag(base)
    assert _devcontainer_image_tag(
        BuildSpec(dockerfile=str(df), context=str(tmp_path), target="dev")
    ) != _devcontainer_image_tag(base)


# ─── _docker_build argv + launch build-order ──────────────────────────────────


def test_docker_build_argv(tmp_path) -> None:
    """Tier 2: #1341 — _docker_build emits `docker build -t tag -f df
    --build-arg k=v --target t context`."""
    fake = _FakeRunner([_ok()])
    launcher = ContainerLauncher(runner=fake)
    launcher._docker_build(
        "reyn-dc-abc:local", "/ws/.devcontainer/Dockerfile", "/ws/.devcontainer",
        build_args={"VARIANT": "3.12"}, target="dev",
    )
    (argv,) = fake.calls
    assert argv[:5] == ["docker", "build", "-t", "reyn-dc-abc:local", "-f"]
    assert argv[5] == "/ws/.devcontainer/Dockerfile"
    assert "--build-arg" in argv and "VARIANT=3.12" in argv
    assert "--target" in argv and "dev" in argv
    assert argv[-1] == "/ws/.devcontainer"  # context last


def test_launch_builds_before_run(tmp_path) -> None:
    """Tier 2: #1341 — a build config drives inspect(miss)→build→run, in order;
    image is the content-addressed tag."""
    df = tmp_path / "Dockerfile"
    df.write_text("FROM python:3.12-slim\n")
    spec = BuildSpec(dockerfile=str(df), context=str(tmp_path))
    tag = _devcontainer_image_tag(spec)
    cfg = LaunchConfig(workspace_root=str(tmp_path), image=tag, build=spec)
    # inspect(miss=rc1) → build(ok) → run(ok, cid)
    fake = _FakeRunner([_err(), _ok(), _ok(b"cid123\n")])
    launcher = ContainerLauncher(runner=fake)
    cid = launcher.launch(cfg)
    assert cid == "cid123"
    verbs = [c[1] for c in fake.calls]
    assert verbs[:3] == ["image", "build", "run"]  # inspect → build → run
    assert "-t" in fake.calls[1] and tag in fake.calls[1]


def test_launch_skips_build_when_image_present(tmp_path) -> None:
    """Tier 2: #1341 — inspect-hit → reuse (no build), straight to run (F2 cache)."""
    spec = BuildSpec(dockerfile=str(tmp_path / "Dockerfile"), context=str(tmp_path))
    cfg = LaunchConfig(workspace_root=str(tmp_path), image="reyn-dc-x:local", build=spec)
    fake = _FakeRunner([_ok(), _ok(b"cid\n")])  # inspect(hit) → run
    ContainerLauncher(runner=fake).launch(cfg)
    verbs = [c[1] for c in fake.calls]
    assert verbs == ["image", "run"]  # no build
    assert all(c[1] != "build" for c in fake.calls)


def test_launch_build_failure_raises(tmp_path) -> None:
    """Tier 2: #1341 — a failed build surfaces as RuntimeError (no silent run)."""
    spec = BuildSpec(dockerfile=str(tmp_path / "Dockerfile"), context=str(tmp_path))
    cfg = LaunchConfig(workspace_root=str(tmp_path), image="reyn-dc-x:local", build=spec)
    fake = _FakeRunner([_err(), _err(b"no such file")])  # inspect(miss) → build(fail)
    with pytest.raises(RuntimeError, match="devcontainer image build failed"):
        ContainerLauncher(runner=fake).launch(cfg)


def test_ensure_image_reyn_base_still_builds(tmp_path) -> None:
    """Tier 2: #1341 DRY-refactor regression — ensure_image(REYN_BASE_IMAGE) still
    inspect-then-builds via the shared _docker_build."""
    fake = _FakeRunner([_err(), _ok()])  # inspect(miss) → build(ok)
    ContainerLauncher(runner=fake).ensure_image(REYN_BASE_IMAGE)
    verbs = [c[1] for c in fake.calls]
    assert verbs == ["image", "build"]
    assert "-t" in fake.calls[1] and REYN_BASE_IMAGE in fake.calls[1]


def test_ensure_image_noop_for_other_image(tmp_path) -> None:
    """Tier 2: a non-base image is left to docker run to pull (ensure_image no-op)."""
    fake = _FakeRunner([])
    ContainerLauncher(runner=fake).ensure_image("python:3.12-slim")
    assert fake.calls == []  # no inspect, no build
