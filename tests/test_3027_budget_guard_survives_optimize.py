"""Tier 2: assert_static_bounds's budget self-consistency guards survive
``python -O`` (#3027).

``assert budgets.effective_trigger > 0`` (and the sibling ``B_M > 0`` check)
used to be plain ``assert`` statements in
``reyn.services.compaction.engine.assert_static_bounds``. CPython strips
every ``assert`` statement when the interpreter runs with ``-O`` /
``PYTHONOPTIMIZE=1`` — so under an optimized production run the guard would
silently vanish, letting a negative ``effective_trigger`` flow downstream
(where ``elide``'s ``total <= effective_trigger`` compares against a
negative number and is always true = compaction fires on every turn).

#3027's fix replaces the two self-consistency checks with an explicit
``raise CompactionBudgetSelfConsistencyError(...)`` (same class of fix as
#2352: "raise, don't assert, so -O cannot strip the guard"). These tests
run the guard in a real subprocess under ``-O`` — a mock/patch of
``__debug__`` would NOT prove anything, because CPython's ``assert``
stripping happens at compile time, not via a runtime flag a test can fake.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# The repo may be `pip install -e`'d from a different checkout (e.g. another
# worktree) than the one pytest is running from — pytest itself picks up the
# right `src/` via `pythonpath = ["src"]` in pyproject.toml, but a bare
# subprocess `python -c` does not. Point PYTHONPATH at *this* checkout's
# `src/` explicitly so the subprocess imports the code under test, not
# whatever `reyn` happens to be installed in site-packages.
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")

_SCRIPT_TEMPLATE = """
import sys
from reyn.config.chat import CompactionConfig
from reyn.services.compaction.engine import ComputedBudgets, assert_static_bounds

cfg = CompactionConfig()
budgets = ComputedBudgets(
    main_pool=10_000, head_budget=1000, body_budget=500,
    tail_budget=1000, new_msg_budget=500,
    B_M={b_m}, main_M_room=5000, effective_trigger={effective_trigger},
)
assert_static_bounds(cfg, budgets, "test-model")
print("NO_RAISE", file=sys.stdout)
"""


def _run(script: str, *, optimize: bool) -> subprocess.CompletedProcess:
    args = [sys.executable]
    if optimize:
        args.append("-O")
    args += ["-c", script]
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)


def test_effective_trigger_guard_fires_under_dash_o() -> None:
    """Tier 2: the effective_trigger<=0 guard raises even under `python -O`.

    This is the load-bearing witness for #3027: before the fix, this exact
    scenario used a plain `assert` and would print NO_RAISE (exit 0) under
    -O instead of raising.
    """
    script = _SCRIPT_TEMPLATE.format(b_m=5000, effective_trigger=-7)
    result = _run(script, optimize=True)
    assert result.returncode != 0, (
        f"guard did NOT fire under -O (stdout={result.stdout!r}): "
        f"a negative effective_trigger silently flowed through"
    )
    assert "CompactionBudgetSelfConsistencyError" in result.stderr, result.stderr
    assert "effective_trigger = -7" in result.stderr, result.stderr
    assert "NO_RAISE" not in result.stdout


def test_effective_trigger_guard_fires_in_normal_mode() -> None:
    """Tier 2: regression — the guard still fires in normal (non -O) mode."""
    script = _SCRIPT_TEMPLATE.format(b_m=5000, effective_trigger=-7)
    result = _run(script, optimize=False)
    assert result.returncode != 0
    assert "CompactionBudgetSelfConsistencyError" in result.stderr, result.stderr
    assert "effective_trigger = -7" in result.stderr, result.stderr


def test_valid_budgets_do_not_fire_under_dash_o() -> None:
    """Tier 2: control — a self-consistent budget config does NOT raise
    under -O (the guard is not over-firing)."""
    script = _SCRIPT_TEMPLATE.format(b_m=5000, effective_trigger=5000)
    result = _run(script, optimize=True)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "NO_RAISE" in result.stdout


def test_valid_budgets_do_not_fire_in_normal_mode() -> None:
    """Tier 2: control — same as above in normal mode (no over-firing)."""
    script = _SCRIPT_TEMPLATE.format(b_m=5000, effective_trigger=5000)
    result = _run(script, optimize=False)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "NO_RAISE" in result.stdout
