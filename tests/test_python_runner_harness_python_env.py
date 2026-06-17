"""Tier 2: PythonRunner resolves the harness interpreter (REYN_HARNESS_PYTHON).

The harness subprocess (`reyn.core.kernel._python_harness`) runs under a resolvable
interpreter: an explicit constructor arg, else the ``REYN_HARNESS_PYTHON``
operator override, else ``sys.executable``. The override lets the harness run
under a reyn-capable interpreter that differs from the one reyn was launched with
(e.g. a backend whose default python cannot host reyn).

Real PythonRunner (no mocks); env controlled via monkeypatch.
"""
from __future__ import annotations

import sys

from reyn.python_runner import PythonRunner


def test_env_var_sets_harness_python(monkeypatch) -> None:
    """Tier 2: with REYN_HARNESS_PYTHON set and no explicit arg, the runner uses it."""
    monkeypatch.setenv("REYN_HARNESS_PYTHON", "/opt/reyn-venv/bin/python")
    assert PythonRunner().python_executable == "/opt/reyn-venv/bin/python"


def test_unset_falls_back_to_sys_executable(monkeypatch) -> None:
    """Tier 2: falsification — with REYN_HARNESS_PYTHON unset, the default is
    unchanged (sys.executable); the override is opt-in, not a behavior shift."""
    monkeypatch.delenv("REYN_HARNESS_PYTHON", raising=False)
    assert PythonRunner().python_executable == sys.executable


def test_explicit_arg_takes_precedence_over_env(monkeypatch) -> None:
    """Tier 2: an explicit python_executable arg wins over the env override."""
    monkeypatch.setenv("REYN_HARNESS_PYTHON", "/opt/reyn-venv/bin/python")
    assert PythonRunner("/explicit/python").python_executable == "/explicit/python"
