"""Tier 2: python harness import does NOT load litellm (subprocess).

OS invariant (FP-0008 C4):
  Importing `reyn.core.kernel._python_harness` — the child-process entry point
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
        "import reyn.core.kernel._python_harness; "
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


def test_harness_import_does_not_load_agent_llm_chain():
    """Tier 2: fresh subprocess import of harness leaves the agent/llm/httpx chain out.

    #1367 — the C4 lazy-litellm fix removed litellm from the harness path, but
    ``reyn/__init__`` and ``reyn/kernel/__init__`` still eager-imported
    ``Agent -> llm -> httpx`` (~0.5s), which under the in-container venv path on
    an emulated host inflated past the ~5s step timeout. The package __init__s
    are now PEP 562-lazy, so importing the harness must NOT transitively load
    ``reyn.skill_runtime`` / ``reyn.llm`` / ``httpx``. This is the structural invariant
    (robust to host speed); the timing guard below is a coarse backstop.
    """
    code = (
        "import reyn.core.kernel._python_harness; import sys; "
        "leaked = [m for m in ('reyn.skill_runtime', 'reyn.llm', 'reyn.llm.llm', 'httpx') "
        "          if m in sys.modules]; "
        "assert not leaked, 'harness import eagerly loaded heavy chain: ' + str(leaked)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"harness import loaded the agent/llm/httpx chain (or failed).\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


def test_lazy_public_api_still_resolves():
    """Tier 2: PEP 562-lazy __init__ still exposes the public names on access.

    Falsification guard for the lazy refactor: ``from reyn import SkillRuntime`` and
    ``from reyn.core.kernel import OSRuntime`` must still resolve (now triggering the
    lazy load), and an unknown attribute must raise AttributeError — proving the
    laziness did not silently drop the public surface.
    """
    code = "\n".join(
        [
            "import reyn",
            "from reyn import SkillRuntime, RunResult, Phase, Skill, SkillGraph",
            "from reyn.core.kernel import OSRuntime, validate_output, normalize",
            "names = (SkillRuntime, RunResult, Phase, Skill, SkillGraph,",
            "         OSRuntime, validate_output, normalize)",
            "assert all(x is not None for x in names), 'a public name resolved to None'",
            "raised = False",
            "try:",
            "    reyn.DoesNotExist",
            "except AttributeError:",
            "    raised = True",
            "assert raised, 'unknown attr did not raise AttributeError'",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"lazy public API did not resolve correctly.\n"
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
        "import reyn.core.kernel._python_harness; "
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
