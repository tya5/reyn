"""Tier 1/2: #183 — swe_bench_runner provisions an in-container reyn venv.

#1356 routes a python preprocessor step's harness subprocess through the sandbox
backend; for `--env-backend=docker` that is a `docker exec` into the swebench
image, whose testbed conda python is repo-pinned (< reyn's 3.11). So the runner
provisions a python3.11 venv WITH reyn inside the container (OS-change-free,
scripts/-isolated) and points the harness at it via REYN_HARNESS_PYTHON.

Pure helpers are text-in/text-out (Tier 1). The container integration uses the
existing injected docker_runner + a real reyn shim (Tier 2, no mocks).

Primary-evidence note (not a unit test): the exact `provision_command` was run in
a real swebench astropy-13453 container — the venv built and
`/opt/reyn-venv/bin/python -m reyn.kernel._python_harness` imported + executed.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

from scripts.swe_bench_runner import (
    provision_command,
    reyn_runtime_deps,
    run_reyn_in_container,
)

_INSTANCE = {
    "instance_id": "astropy__astropy-13453",
    "repo": "astropy/astropy",
    "base_commit": "abc123",
    "problem_statement": "Fix a bug.",
}


# ── Tier 1: pure helpers ─────────────────────────────────────────────────────


def test_reyn_runtime_deps_drops_dev_tools_keeps_runtime_with_pins() -> None:
    """Tier 1: runtime deps keep their pyproject version pins; test/lint tooling
    (pytest / ruff / mypy / pre-commit) is dropped (the harness doesn't need it)."""
    pyproject = (
        "[project]\n"
        'dependencies = [\n'
        '  "litellm>=1.0",\n'
        '  "pydantic>=2.0",\n'
        '  "pytest>=8.0",\n'
        '  "ruff",\n'
        '  "mypy",\n'
        '  "pytest-asyncio>=0.23",\n'
        '  "numpy>=1.24",\n'
        ']\n'
    )
    deps = reyn_runtime_deps(pyproject)
    assert "litellm>=1.0" in deps and "pydantic>=2.0" in deps and "numpy>=1.24" in deps
    assert not any(
        d.split(">=")[0].split("[")[0].strip() in {"pytest", "ruff", "mypy", "pytest-asyncio"}
        for d in deps
    ), f"dev tools must be dropped: {deps}"


def test_provision_command_builds_venv_pip_and_pth() -> None:
    """Tier 1: the provision body creates a 3.11 venv, pip-installs the deps
    (each shell-quoted — version specs contain `>`), and puts reyn on the path via
    a .pth to the bind-mounted source (no PYTHONPATH threading)."""
    cmd = provision_command(["litellm>=1.0", "pydantic>=2.0"], "/host/repo/src")
    assert "python3.11 -m venv /opt/reyn-venv" in cmd
    assert "/opt/reyn-venv/bin/pip install" in cmd
    # version specs are shell-quoted so bash does not treat `>` as a redirect
    assert "'litellm>=1.0'" in cmd and "'pydantic>=2.0'" in cmd
    # the .pth points at the reyn source at its host-absolute path (same-path mount)
    assert "reyn.pth" in cmd and "/host/repo/src" in cmd


# ── Tier 2: container integration (injected docker_runner + reyn shim) ────────


class _RecordingDocker:
    """Injectable docker runner — records argv, returns a configurable result."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout=180):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=0, stdout="cid123\n", stderr="")


def _patch_emitting_reyn(tmp_path: Path, env_out: Path):
    """A reyn shim that records REYN_HARNESS_PYTHON from its env + prints a patch."""
    script = tmp_path / "rec_reyn.py"
    script.write_text(
        "import sys, os, json\n"
        f"open({str(env_out)!r}, 'w').write(os.environ.get('REYN_HARNESS_PYTHON', ''))\n"
        "print('=== Final Output ===')\n"
        "print(json.dumps({'patch': 'diff --git a/f b/f\\n'}))\n",
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def test_container_mounts_reyn_and_provisions_venv(tmp_path: Path) -> None:
    """Tier 2: docker run bind-mounts the reyn repo :ro, and a `docker exec`
    provisions the venv (the harness's python substrate) before reyn runs."""
    docker = _RecordingDocker()
    reyn_base = _patch_emitting_reyn(tmp_path, tmp_path / "hp.txt")

    run_reyn_in_container(
        _INSTANCE,
        image="swebench/sweb.eval.x86_64.astropy:latest",
        repo_dir="/testbed",
        state_dir=str(tmp_path / "state"),
        reyn_base=reyn_base,
        timeout=30,
        docker_runner=docker,
    )

    run_call = next(c for c in docker.calls if c[:2] == ["docker", "run"])
    # the reyn repo is bind-mounted read-only at its OWN host path (same-path
    # mount) so host-absolute paths (module + .pth) resolve inside the container
    mount = run_call[run_call.index("-v") + 1]
    src, dst, mode = mount.rsplit(":", 2)
    assert src == dst, f"reyn must be same-path mounted (host==container): {mount}"
    assert mode == "ro", f"reyn mount must be read-only: {mount}"

    exec_call = next((c for c in docker.calls if c[:2] == ["docker", "exec"]), None)
    assert exec_call is not None, "a docker exec must provision the venv"
    body = exec_call[-1]
    assert "python3.11 -m venv /opt/reyn-venv" in body and "reyn.pth" in body
    # provisioning precedes the teardown
    exec_idx = docker.calls.index(exec_call)
    rm_idx = next(i for i, c in enumerate(docker.calls) if c[:3] == ["docker", "rm", "-f"])
    assert exec_idx < rm_idx


def test_container_points_harness_at_venv_python(tmp_path: Path) -> None:
    """Tier 2: reyn is invoked with REYN_HARNESS_PYTHON = the in-container venv
    python, so the #1356 harness routes to the 3.11 venv (the only OS-side hook)."""
    env_out = tmp_path / "hp.txt"
    docker = _RecordingDocker()
    reyn_base = _patch_emitting_reyn(tmp_path, env_out)

    run_reyn_in_container(
        _INSTANCE,
        image="img:latest",
        repo_dir="/testbed",
        state_dir=str(tmp_path / "state"),
        reyn_base=reyn_base,
        timeout=30,
        docker_runner=docker,
    )

    assert env_out.read_text().strip() == "/opt/reyn-venv/bin/python"
