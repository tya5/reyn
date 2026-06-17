"""Tier 1: #183 — swe_bench_runner's in-container reyn-venv provisioning helpers.

#1356 routes a python preprocessor step's harness subprocess through the sandbox
backend; for `--env-backend=docker` that is a `docker exec` into the swebench
image, whose testbed conda python is repo-pinned (< reyn's 3.11). So the runner
provisions a python3.11 venv WITH reyn inside the container (OS-change-free,
scripts/-isolated) and points the harness at it via REYN_HARNESS_PYTHON.

These pin the pure provisioning helpers (text-in/text-out). The full container
lifecycle that consumes them now lives in `run_reyn_once_in_container` (the
general-agent path; the swe_bench skill + its `run_reyn_in_container` subprocess
solver were retired in #187) and is exercised end-to-end by the faithful N-run.

Primary-evidence note (not a unit test): the exact `provision_command` was run in
a real swebench astropy-13453 container — the venv built and
`/opt/reyn-venv/bin/python -m reyn.core.kernel._python_harness` imported + executed.
"""
from __future__ import annotations

from scripts.swe_bench_runner import provision_command, reyn_runtime_deps


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
