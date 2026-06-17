"""Tier 2: #1663 — the CodeAct harness interpreter is the HOST python, NOT
REYN_HARNESS_PYTHON.

CodeActRunner's harness is a host-local orchestrator: its AF_UNIX control socket
is handed to the child via ``pass_fds`` (an inherited fd cannot cross a
``docker exec`` boundary), so the harness must run on the reyn host under the
reyn-process interpreter. It deliberately does NOT honor ``REYN_HARNESS_PYTHON``
— that operator override targets the in-container #1356 *preprocessor* harness
(``PythonRunner``), which is routed through ``backend.run`` (= ``docker exec``)
and so needs the container's python. Picking it up here pointed codeact's host
``Popen`` at a container-only path (``/opt/reyn-venv/bin/python``) under
``--env-backend=docker``, and the seatbelt-wrapped exec failed with execvp rc=71.

This is the inverse of ``test_python_runner_harness_python_env`` (PythonRunner
MUST honor the env var); the two pin the deliberate divergence.

Real CodeActRunner (no mocks); env controlled via monkeypatch.
"""
from __future__ import annotations

import sys

from reyn.core.kernel.codeact_runner import CodeActRunner


def test_codeact_ignores_harness_python_env(monkeypatch) -> None:
    """Tier 2: #1663 — even with REYN_HARNESS_PYTHON set to a container path,
    CodeActRunner uses the host sys.executable (the rc=71 regression guard). A
    non-default sentinel pins it: were the env fallback still present, the runner
    would pick up the container path and this would fail."""
    monkeypatch.setenv("REYN_HARNESS_PYTHON", "/opt/reyn-venv/bin/python")
    runner = CodeActRunner()
    assert runner.python_executable == sys.executable
    assert runner.python_executable != "/opt/reyn-venv/bin/python"


def test_codeact_unset_env_is_sys_executable(monkeypatch) -> None:
    """Tier 2: #1663 — with the env var unset, the default is the host
    sys.executable (unchanged for the normal non-docker path)."""
    monkeypatch.delenv("REYN_HARNESS_PYTHON", raising=False)
    assert CodeActRunner().python_executable == sys.executable


def test_codeact_explicit_arg_wins(monkeypatch) -> None:
    """Tier 2: #1663 — an explicit python_executable arg still wins (test/caller
    override), independent of the env var."""
    monkeypatch.setenv("REYN_HARNESS_PYTHON", "/opt/reyn-venv/bin/python")
    assert CodeActRunner("/explicit/python").python_executable == "/explicit/python"
