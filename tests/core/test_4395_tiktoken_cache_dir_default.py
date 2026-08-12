"""Tier 2: importing `reyn` sets TIKTOKEN_CACHE_DIR to a reyn-owned,
persistent directory before anything else runs (#4395).

OS invariant: every tiktoken-touching code path in this codebase — whether
it goes through litellm.token_counter or (as `litellm_provider.py` does)
calls tiktoken directly — must see the SAME persistent, reyn-owned cache
directory, regardless of import order, so tiktoken never falls back to its
own default (the OS temp dir, periodically cleared, so a network fetch to
openaipublic.blob.core.windows.net can recur even after having worked
once) and never gets pointed at litellm's own package-internal directory
(where a tokenizer-version mismatch would make tiktoken delete an
installed package's data file and re-fetch on every run).

Verified in a fresh subprocess — this is an `os.environ` side effect of
`reyn`'s package `__init__`, which only runs once per process; testing it
in-process would either see a stale value from whatever earlier import in
the test process's own history ran first, or (worse) mask the very
import-order bug #4395 is about.
"""
from __future__ import annotations

import os
import subprocess
import sys


def _run(
    src_root: str, code: str, *, unset: "tuple[str, ...]" = (), **env_overrides: str,
) -> subprocess.CompletedProcess:
    """Run *code* in a fresh subprocess with `src_root` on PYTHONPATH.
    `unset` removes keys from the inherited environment entirely (not the
    same as setting them to "") — `env_overrides` sets/replaces others."""
    env = {**os.environ, "PYTHONPATH": src_root, **env_overrides}
    for key in unset:
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_importing_reyn_sets_tiktoken_cache_dir(out_of_process_reyn):
    """Tier 2: a fresh process, no env var pre-set, sees TIKTOKEN_CACHE_DIR
    populated the instant `reyn` is importable — before any litellm or
    tiktoken import of its own."""
    code = (
        "import os; "
        "assert 'TIKTOKEN_CACHE_DIR' not in os.environ, 'test env leaked the var in'; "
        "import reyn; "
        "v = os.environ.get('TIKTOKEN_CACHE_DIR'); "
        "assert v, 'TIKTOKEN_CACHE_DIR was not set by importing reyn'"
    )
    result = _run(out_of_process_reyn, code, unset=("TIKTOKEN_CACHE_DIR",))
    assert result.returncode == 0, (
        f"importing reyn failed to set TIKTOKEN_CACHE_DIR.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_tiktoken_cache_dir_points_outside_the_os_temp_dir_and_litellm_package(out_of_process_reyn):
    """Tier 2: the value is neither tiktoken's own OS-temp-dir default (gets
    cleared, so a fix pointing there would silently regress) nor inside the
    litellm package's own install directory (tiktoken can delete a file
    there on a version mismatch — see this module's own docstring)."""
    code = (
        "import os, tempfile, importlib.util; "
        "import reyn; "
        "v = os.environ['TIKTOKEN_CACHE_DIR']; "
        "assert not v.startswith(tempfile.gettempdir()), "
        "'points at the OS temp dir — gets cleared, defeats persistence: ' + v; "
        "spec = importlib.util.find_spec('litellm'); "
        "litellm_dir = next(iter(spec.submodule_search_locations)) if spec and spec.submodule_search_locations else None; "
        "assert litellm_dir is None or not v.startswith(litellm_dir), "
        "'points inside the litellm package install — tiktoken can mutate it: ' + v; "
        "assert os.path.isdir(v), 'the directory was not actually created: ' + v"
    )
    result = _run(out_of_process_reyn, code, unset=("TIKTOKEN_CACHE_DIR",))
    assert result.returncode == 0, (
        f"TIKTOKEN_CACHE_DIR points at a fragile location.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_an_operator_set_value_is_respected(out_of_process_reyn):
    """Tier 2: reyn's default must not clobber an operator's own explicit
    choice — `setdefault`, not a forced assignment (same convention the
    existing LITELLM_LOCAL_* defaults in this module already use)."""
    code = (
        "import os; "
        "import reyn; "
        "assert os.environ['TIKTOKEN_CACHE_DIR'] == '/operator/chosen/path', "
        "'reyn overrode an operator-set TIKTOKEN_CACHE_DIR: ' + os.environ['TIKTOKEN_CACHE_DIR']"
    )
    result = _run(
        out_of_process_reyn, code, TIKTOKEN_CACHE_DIR="/operator/chosen/path",
    )
    assert result.returncode == 0, (
        f"reyn did not respect an operator-set TIKTOKEN_CACHE_DIR.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
