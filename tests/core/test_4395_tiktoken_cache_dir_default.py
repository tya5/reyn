"""Tier 2: importing `reyn` sets TIKTOKEN_CACHE_DIR (and
CUSTOM_TIKTOKEN_CACHE_DIR) to a reyn-owned, persistent directory, and that
choice SURVIVES a subsequent `import litellm` (#4395).

OS invariant: every tiktoken-touching code path in this codebase — whether
it goes through litellm.token_counter or (as `litellm_provider.py` does)
calls tiktoken directly — must see the SAME persistent, reyn-owned cache
directory, regardless of import order, so tiktoken never falls back to its
own default (the OS temp dir, periodically cleared, so a network fetch to
openaipublic.blob.core.windows.net can recur even after having worked
once) and never gets pointed at litellm's own package-internal directory
(where a tokenizer-version mismatch would make tiktoken delete an
installed package's data file and re-fetch on every run).

TWO env vars are required, not one — an earlier draft of this fix set only
TIKTOKEN_CACHE_DIR and claimed litellm's own later import "force-assigns
... so this is only a bridge", which was WRONG for the case that matters
most: litellm's own `default_encoding.py` (loaded transitively by `import
litellm` itself, the owner's actual crash site) does an UNCONDITIONAL
`os.environ["TIKTOKEN_CACHE_DIR"] = ...` (confirmed by reading its source
directly, not assumed) — NOT a `setdefault`. The one input that module
actually reads before deciding that value is `CUSTOM_TIKTOKEN_CACHE_DIR`;
setting only `TIKTOKEN_CACHE_DIR` gets silently overwritten the moment
litellm is imported. `test_survives_a_subsequent_litellm_import` below is
the test that would have caught this — the other tests only checked state
immediately after `import reyn`, never after the `import litellm` that
actually exercises the bug.

Redirecting WHERE the cache lives is still not enough on its own: it
leaves the directory EMPTY on a first run, and tiktoken's own fetch call
(`requests.get(blobpath)` in `tiktoken/load.py`) has NO timeout — under a
proxy/firewall that accepts a connection but never answers, that first
run hangs forever (owner-observed: "Loading..." never resolves, no
exception, no log growth — the hang is inside `import litellm` itself, on
the startup path). `test_seeds_the_cache_from_litellms_own_bundled_blobs`
covers the fix: importing `reyn` also seeds the redirected directory from
litellm's own bundled tiktoken blobs (a same-machine file copy, zero
network) so a version-compatible combo never needs the network even cold.

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
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )


def test_importing_reyn_sets_both_cache_dir_vars(out_of_process_reyn):
    """Tier 2: a fresh process, no env vars pre-set, sees BOTH
    TIKTOKEN_CACHE_DIR and CUSTOM_TIKTOKEN_CACHE_DIR populated the instant
    `reyn` is importable — before any litellm or tiktoken import of its
    own. Both are required (see module docstring); this only checks they
    exist, `test_survives_a_subsequent_litellm_import` checks the actual
    defect (surviving litellm's own overwrite)."""
    code = (
        "import os; "
        "assert 'TIKTOKEN_CACHE_DIR' not in os.environ, 'test env leaked the var in'; "
        "import reyn; "
        "v1 = os.environ.get('TIKTOKEN_CACHE_DIR'); "
        "v2 = os.environ.get('CUSTOM_TIKTOKEN_CACHE_DIR'); "
        "assert v1, 'TIKTOKEN_CACHE_DIR was not set by importing reyn'; "
        "assert v2, 'CUSTOM_TIKTOKEN_CACHE_DIR was not set by importing reyn'; "
        "assert v1 == v2, 'the two vars disagree: ' + repr((v1, v2))"
    )
    result = _run(
        out_of_process_reyn, code,
        unset=("TIKTOKEN_CACHE_DIR", "CUSTOM_TIKTOKEN_CACHE_DIR"),
    )
    assert result.returncode == 0, (
        f"importing reyn failed to set both cache-dir vars.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_survives_a_subsequent_litellm_import(out_of_process_reyn):
    """Tier 2: THE regression test. litellm's own `default_encoding.py`
    (loaded transitively the moment `import litellm` runs) does an
    UNCONDITIONAL assignment to TIKTOKEN_CACHE_DIR — reading only
    CUSTOM_TIKTOKEN_CACHE_DIR as its one overridable input. Without setting
    that second var, reyn's own TIKTOKEN_CACHE_DIR default is silently
    discarded the moment litellm is imported anywhere — exactly the owner's
    actual crash site (`import litellm` itself, not a later call)."""
    code = (
        "import os; "
        "import reyn; "
        "before = os.environ['TIKTOKEN_CACHE_DIR']; "
        "import litellm; "
        "after = os.environ['TIKTOKEN_CACHE_DIR']; "
        "assert after == before, "
        "'litellm import overwrote reyn TIKTOKEN_CACHE_DIR: ' + repr((before, after))"
    )
    result = _run(
        out_of_process_reyn, code,
        unset=("TIKTOKEN_CACHE_DIR", "CUSTOM_TIKTOKEN_CACHE_DIR"),
    )
    assert result.returncode == 0, (
        f"reyn's TIKTOKEN_CACHE_DIR did not survive `import litellm`.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_seeds_the_cache_from_litellms_own_bundled_blobs(out_of_process_reyn, tmp_path):
    """Tier 2: THE hang-on-first-run defect. Redirecting WHERE the cache
    lives (the other tests) still leaves it EMPTY on a first run —
    tiktoken's own fetch call has no timeout, so under a proxy that
    accepts a connection but never answers, that first run hangs forever
    (owner-observed: "Loading..." never resolves). Importing `reyn` must
    seed the redirected directory from litellm's own bundled blobs so a
    version-compatible combo never needs the network at all, even cold.
    A fresh, isolated $HOME (not `out_of_process_reyn`'s inherited one) so
    this doesn't depend on — or pollute — any real prior cache state."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    code = (
        "import os, re, socket\n"
        "socket.socket.connect = lambda self, *a, **kw: (_ for _ in ()).throw(AssertionError('NETWORK CALL: ' + repr(a)))\n"
        "import reyn\n"
        "cache_dir = os.path.join(os.path.expanduser('~'), '.reyn', 'cache', 'tiktoken')\n"
        "names = os.listdir(cache_dir)\n"
        "assert names, 'the cache directory was not seeded with anything: ' + cache_dir\n"
        "sha1_pattern = re.compile(r'^[0-9a-f]{40}$')\n"
        "hex_named = [n for n in names if sha1_pattern.match(n)]\n"
        "assert hex_named, 'no sha1-named cache blob was seeded: ' + repr(names)\n"
        "import litellm\n"
        "print('OK: seeded and import litellm needed zero network calls')\n"
    )
    result = _run(
        out_of_process_reyn, code,
        unset=("TIKTOKEN_CACHE_DIR", "CUSTOM_TIKTOKEN_CACHE_DIR"),
        HOME=str(fake_home),
    )
    assert result.returncode == 0, (
        f"import reyn did not seed the cache dir, or a network call was attempted.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_does_not_seed_when_an_operator_chose_a_different_directory(out_of_process_reyn, tmp_path):
    """Tier 2: seeding writes into reyn's OWN computed directory — if an
    operator's own TIKTOKEN_CACHE_DIR won the setdefault (a different
    directory), reyn's directory is never consulted by anything, so
    seeding it would be pure waste. Confirms that directory stays empty
    in that case (the operator's own chosen directory is untouched by
    this seed step entirely — it's their directory to manage)."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    operator_dir = tmp_path / "operator_chosen_cache"
    operator_dir.mkdir()
    code = (
        "import os; "
        "import reyn; "
        "reyn_dir = os.path.join(os.path.expanduser('~'), '.reyn', 'cache', 'tiktoken'); "
        "assert os.listdir(reyn_dir) == [], "
        "'reyn seeded its own directory even though the operator chose a different one: ' + repr(os.listdir(reyn_dir))"
    )
    result = _run(
        out_of_process_reyn, code,
        HOME=str(fake_home),
        TIKTOKEN_CACHE_DIR=str(operator_dir),
    )
    assert result.returncode == 0, (
        f"unexpected seeding into reyn's own dir despite an operator override.\n"
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
    result = _run(
        out_of_process_reyn, code,
        unset=("TIKTOKEN_CACHE_DIR", "CUSTOM_TIKTOKEN_CACHE_DIR"),
    )
    assert result.returncode == 0, (
        f"TIKTOKEN_CACHE_DIR points at a fragile location.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_an_operator_set_value_is_respected(out_of_process_reyn):
    """Tier 2: reyn's defaults must not clobber an operator's own explicit
    choice for EITHER var — `setdefault`, not a forced assignment (same
    convention the existing LITELLM_LOCAL_* defaults in this module already
    use)."""
    code = (
        "import os; "
        "import reyn; "
        "assert os.environ['TIKTOKEN_CACHE_DIR'] == '/operator/chosen/path', "
        "'reyn overrode an operator-set TIKTOKEN_CACHE_DIR: ' + os.environ['TIKTOKEN_CACHE_DIR']; "
        "assert os.environ['CUSTOM_TIKTOKEN_CACHE_DIR'] == '/operator/other/path', "
        "'reyn overrode an operator-set CUSTOM_TIKTOKEN_CACHE_DIR: ' + os.environ['CUSTOM_TIKTOKEN_CACHE_DIR']"
    )
    result = _run(
        out_of_process_reyn, code,
        TIKTOKEN_CACHE_DIR="/operator/chosen/path",
        CUSTOM_TIKTOKEN_CACHE_DIR="/operator/other/path",
    )
    assert result.returncode == 0, (
        f"reyn did not respect an operator-set cache-dir var.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
