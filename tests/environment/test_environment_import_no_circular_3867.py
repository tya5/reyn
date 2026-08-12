"""Tier 2: `import reyn.environment` resolves standalone, no circular import (#3867).

OS invariant: importing ``reyn.environment`` as the FIRST reyn import in a
fresh interpreter must not raise ``ImportError``.

Before this fix, the import graph closed a cycle:
  environment/__init__.py    -> host_backend
  host_backend.py            -> data.workspace.text_codec (a submodule import,
                                 which always runs data/workspace/__init__.py
                                 first)
  data/workspace/__init__.py -> .workspace (Workspace)
  workspace.py                -> environment.host_backend  <- closes the loop

The fix relocates ``text_codec`` (a leaf module with no dependency on
``Workspace`` — see its own module docstring) out of the ``data.workspace``
package into ``reyn.data.text_codec``, so importing it no longer runs
``data/workspace/__init__.py`` and never re-enters ``environment.host_backend``.

Why a normal in-process pytest test cannot witness this: this repo's full
suite collects 10,388+ tests with zero collection errors even with the bug
present — some OTHER test, imported earlier in collection order, always
happens to have already fully initialized ``reyn.environment`` or
``reyn.data.workspace`` (breaking the partial-init race) before any test
reaches the code path that would trip it. Only a FRESH interpreter that
imports ``reyn.environment`` as its first reyn import reproduces the failure
(exactly the shape a lone `pytest tests/test_x.py` -k, or a real `python -c
"import reyn.environment"`, hits) — hence the subprocess witness below rather
than an in-process import assertion.
"""
from __future__ import annotations

import os
import subprocess
import sys


def test_environment_is_importable_as_the_first_reyn_import(out_of_process_reyn):
    """Tier 2: #3867 — `reyn.environment` must not depend on anything that,
    transitively, depends back on `reyn.environment` before it finishes
    initializing. Reproduces via a fresh interpreter where `reyn.environment`
    is the FIRST reyn import (in-process, other already-imported reyn modules
    would mask the partial-init race)."""
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    result = subprocess.run(
        [sys.executable, "-c", "import reyn.environment; print('OK')"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": out_of_process_reyn},
    )
    assert result.returncode == 0, (
        "`import reyn.environment` failed as the first reyn import in a fresh "
        f"interpreter (#3867 circular-import regression).\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_data_workspace_is_importable_as_the_first_reyn_import(out_of_process_reyn):
    """Tier 2: #3867 — the reverse direction: `reyn.data.workspace` (which
    constructs a `HostBackend` from `reyn.environment.host_backend`) must also
    be importable standalone, first, in a fresh interpreter."""
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    result = subprocess.run(
        [sys.executable, "-c", "import reyn.data.workspace; print('OK')"],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": out_of_process_reyn},
    )
    assert result.returncode == 0, (
        "`import reyn.data.workspace` failed as the first reyn import in a fresh "
        f"interpreter.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout
