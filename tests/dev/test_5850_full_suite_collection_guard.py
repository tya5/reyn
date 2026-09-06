"""Tier 1: Contract — reyn.dev.testing.full_suite_guard, the #5850 local
full-suite collection-count abort.

Same style as tests/dev/test_stall_dump_4986.py: the guard's own
``pytest_collection_modifyitems`` calls ``pytest.exit()``, which ends the
session it fires in before a single test runs. Exercising that from
pytester's in-process ``runpytest()`` would abort THIS file's own outer
collection along with it, so every scenario below spawns a real
``sys.executable -m pytest`` SUBPROCESS instead (never the in-process
runner) against a throwaway inner test tree of N trivial items, loading
ONLY ``reyn.dev.testing.full_suite_guard`` as a pytest plugin — not this
repo's own tests/conftest.py, whose other fixtures assume this repo's real
directory layout and would break inside pytester's throwaway rootdir.

``out_of_process_reyn`` pins the spawned subprocess's ``PYTHONPATH`` to the
SAME reyn checkout this test itself imports (#5028) — required whenever a
test spawns a `sys.executable` subprocess importing `reyn` (the guard
module lives under `reyn.dev.testing`, so every inner run needs it).

The outer CI/GITHUB_ACTIONS env (genuinely set when THIS suite itself runs
in CI) and any inherited override are stripped from every spawn's env
below — each scenario needs to control those two inputs precisely, not
inherit whatever the outer process happens to be running under.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from reyn.dev.testing.full_suite_guard import (
    FULL_SUITE_COLLECTION_CEILING,
    OVERRIDE_ENV_VAR,
)

pytest_plugins = ["pytester"]

_INNER_CONFTEST = 'pytest_plugins = ["reyn.dev.testing.full_suite_guard"]\n'

# The guard's own abort message (full_suite_guard.py) — the identifying
# substring this test's "did it actually fire" checks key on.
_GUARD_MESSAGE_MARKER = "BLOCKED by tests/conftest.py's #5850 collection guard"

_STRIPPED_ENV_VARS = ("CI", "GITHUB_ACTIONS", OVERRIDE_ENV_VAR)


def _make_test_items(n: int) -> str:
    return "\n".join(f"def test_item_{i}():\n    assert True\n" for i in range(n))


def _spawn_inner_pytest(
    pytester: pytest.Pytester,
    src_root: str,
    n_items: int,
    extra_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    pytester.makeconftest(_INNER_CONFTEST)
    pytester.makepyfile(test_inner=_make_test_items(n_items))
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV_VARS}
    env["PYTHONPATH"] = src_root + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_inner.py"],
        cwd=pytester.path, env=env, capture_output=True, text=True,
    )


def test_over_ceiling_collection_aborts_the_session(
    pytester: pytest.Pytester, out_of_process_reyn: str,
) -> None:
    """Tier 1: N+1 collected items (one over FULL_SUITE_COLLECTION_CEILING),
    no override, no CI env -> pytest.exit() aborts the session with a
    nonzero returncode and the guard's own message, before any test runs."""
    result = _spawn_inner_pytest(
        pytester, out_of_process_reyn, FULL_SUITE_COLLECTION_CEILING + 1, {},
    )
    assert result.returncode == 1
    assert _GUARD_MESSAGE_MARKER in result.stdout


def test_override_env_var_lets_an_over_ceiling_run_pass_through(
    pytester: pytest.Pytester, out_of_process_reyn: str,
) -> None:
    """Tier 1: the SAME over-ceiling collection, with REYN_FULL_SUITE_OK=1
    set -> the guard steps aside and the session completes normally."""
    result = _spawn_inner_pytest(
        pytester, out_of_process_reyn, FULL_SUITE_COLLECTION_CEILING + 1,
        {OVERRIDE_ENV_VAR: "1"},
    )
    assert result.returncode == 0
    assert _GUARD_MESSAGE_MARKER not in result.stdout


def test_ci_env_lets_an_over_ceiling_run_pass_through(
    pytester: pytest.Pytester, out_of_process_reyn: str,
) -> None:
    """Tier 1: the SAME over-ceiling collection, with CI=1 set (as GitHub
    Actions itself sets it, never a local shell) -> unconditionally exempt,
    the session completes normally without needing the override too."""
    result = _spawn_inner_pytest(
        pytester, out_of_process_reyn, FULL_SUITE_COLLECTION_CEILING + 1,
        {"CI": "1"},
    )
    assert result.returncode == 0
    assert _GUARD_MESSAGE_MARKER not in result.stdout


def test_exactly_at_ceiling_passes_through_unmodified(
    pytester: pytest.Pytester, out_of_process_reyn: str,
) -> None:
    """Tier 1: the boundary itself -- exactly FULL_SUITE_COLLECTION_CEILING
    items, no override, no CI env -> the <= comparison lets it through.
    Guards the off-by-one directly, not just "some number under it"."""
    result = _spawn_inner_pytest(
        pytester, out_of_process_reyn, FULL_SUITE_COLLECTION_CEILING, {},
    )
    assert result.returncode == 0
    assert _GUARD_MESSAGE_MARKER not in result.stdout
