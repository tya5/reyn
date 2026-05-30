"""Tier 2: python harness import does NOT load litellm (subprocess).

OS invariant (FP-0008 C4):
  Importing `reyn.kernel._python_harness` — the child-process entry point
  for python preprocessor steps — must NOT trigger a transitive `import
  litellm`. The litellm package takes ~8-14s to initialise; the sandboxed
  preprocessor subprocess has a 5s timeout, so an eager litellm import
  causes unconditional SIGKILL before any user code runs.

  Verified in a fresh subprocess so that litellm already present in the
  test process's sys.modules does not mask the bug.
"""
from __future__ import annotations

import subprocess
import sys
import time


def test_harness_import_does_not_load_litellm():
    """Tier 2: fresh subprocess import of harness leaves litellm out of sys.modules."""
    code = (
        "import reyn.kernel._python_harness; "
        "import sys; "
        "assert 'litellm' not in sys.modules, "
        "'litellm was eagerly loaded by harness import: ' + str(sorted(k for k in sys.modules if k.startswith('litellm')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"harness import loaded litellm (or failed).\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


def test_harness_import_time_is_below_ceiling():
    """Tier 2: harness import completes well under the preprocessor timeout ceiling.

    Ceiling is 3.0s — generous enough to avoid flake on slow CI, but well
    below the pre-fix ~10s and well below the 5s preprocessor timeout.
    A regression back to the eager litellm chain would produce ~8-14s and
    fail this guard.
    """
    code = (
        "import sys, time; "
        "t = time.time(); "
        "import reyn.kernel._python_harness; "
        "elapsed = time.time() - t; "
        "assert elapsed < 3.0, f'harness import took {elapsed:.2f}s (ceiling 3.0s)'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"harness import exceeded ceiling.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
