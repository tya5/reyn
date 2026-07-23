"""Tier 1: `check_in_process_tree` contract, and a wiring witness for the root
`conftest.py` guard it feeds (#3233).

Two tests, deliberately different shapes, per the architect's #3233 gap note:
a Tier-1 contract test on the pure function alone stays green even if someone
deletes the `pytest_configure` call that wires it into pytest startup — the
function would still return correct findings, just never asked. The second
test below spawns a REAL `pytest --collect-only` subprocess with a decoy
`PYTHONPATH` so a `reyn` OUTSIDE this checkout resolves first, and asserts on
the subprocess's actual exit code and stderr — the only way to falsify "the
guard is wired, not just present as dead code".

Neither test moves the real `reyn.__file__`: the contract test passes synthetic
paths to the pure function directly, and the wiring test manufactures a decoy
package in a `tmp_path` rather than touching this checkout's own `src/reyn`.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "verify_env_identity.py"


def _load():
    spec = importlib.util.spec_from_file_location("_env_identity_3233_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_reyn_file_outside_root_src_is_flagged_with_both_paths_and_a_remedy(
    tmp_path: Path,
) -> None:
    """Tier 1: a `reyn.__file__` outside `<root>/src` returns a Finding naming both
    paths and a concrete remedy.

    FALSIFY: without this check, a wrong-worktree `import reyn` resolution is
    silently accepted and every collected test measures the wrong checkout's code.
    """
    module = _load()
    root = tmp_path / "this_checkout"
    other = tmp_path / "other_checkout" / "src" / "reyn" / "__init__.py"
    other.parent.mkdir(parents=True)
    other.write_text("")

    finding = module.check_in_process_tree(other, root)

    assert finding is not None
    assert finding.check == "in-process-tree"
    assert str(root) in finding.detail
    assert str(other) in finding.detail
    # The remedy must be concrete and actionable, not a generic "fix your env".
    assert "PYTHONPATH" in finding.remedy
    assert "worktree" in finding.remedy or str(root) in finding.remedy


def test_a_reyn_file_under_root_src_is_clean(tmp_path: Path) -> None:
    """Tier 1: a `reyn.__file__` under `<root>/src` returns None (negative control)."""
    module = _load()
    root = tmp_path / "this_checkout"
    reyn_file = root / "src" / "reyn" / "__init__.py"
    reyn_file.parent.mkdir(parents=True)
    reyn_file.write_text("")

    assert module.check_in_process_tree(reyn_file, root) is None


def test_a_decoy_reyn_cached_before_pytest_starts_makes_startup_exit_nonzero(
    tmp_path: Path,
) -> None:
    """Tier 2: the root `conftest.py`'s `pytest_configure` is actually wired in.

    This is the load-bearing witness the architect flagged as missing from a
    pure-function test alone: it drives a REAL `python -m pytest --collect-only`
    subprocess against this repo and asserts on its actual exit code and
    output. If the `pytest_configure` call were ever deleted from
    `conftest.py`, the pure-function tests above would stay green while this
    one goes red — proving the WIRING, not just the logic, is under test.

    A plain decoy `PYTHONPATH` entry is not sufficient to reproduce the
    divergence: pytest's own `pythonpath = ["src"]` (`pyproject.toml`) inserts
    this checkout's `src` at `sys.path[0]` *after* interpreter startup, which
    wins over anything already on `PYTHONPATH` (verified empirically — a bare
    decoy `PYTHONPATH` entry left the guard silent). The real #3231 incident
    reproduces only when a decoy `reyn` is already cached in `sys.modules`
    BEFORE pytest runs — exactly what an ambient venv's editable `.pth` does at
    interpreter startup, ahead of anything pytest itself controls. A
    `sitecustomize.py` on `PYTHONPATH` reproduces that same "already resolved,
    before pytest's own insertion" timing without touching any real venv.
    """
    decoy_reyn = tmp_path / "decoy_src" / "reyn"
    decoy_reyn.mkdir(parents=True)
    (decoy_reyn / "__init__.py").write_text("")

    site_hook = tmp_path / "site_hook"
    site_hook.mkdir()
    (site_hook / "sitecustomize.py").write_text(
        f"import sys\n"
        f"sys.path.insert(0, {str(tmp_path / 'decoy_src')!r})\n"
        f"import reyn  # noqa: F401  cache the decoy before pytest starts\n"
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(site_hook), str(tmp_path / "decoy_src"), env.get("PYTHONPATH", "")]
    )

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode != 0, (
        f"expected a decoy `reyn` cached before pytest startup to make the "
        f"in-process guard fire; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    combined = proc.stdout + proc.stderr
    assert "env-identity (in-process, #3233)" in combined
    assert "PYTHONPATH" in combined
    assert str(decoy_reyn / "__init__.py") in combined
