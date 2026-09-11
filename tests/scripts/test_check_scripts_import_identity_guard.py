"""Tier 1: `check_scripts_import_identity_guard.py`'s own detector logic
(#3024).

Population is derived from real `.py` files written into `tmp_path` (never
from the real `scripts/` tree, so a future script added/removed there
cannot shift what this test asserts) and driven through the real, public
`imports_reyn`/`calls_guard`/`measured` functions -- no mocks, matching
this repo's own testing policy.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT_NAME = "check_scripts_import_identity_guard.py"


def _load():
    from tests._support.paths import REPO_ROOT

    path = REPO_ROOT / "scripts" / _SCRIPT_NAME
    spec = importlib.util.spec_from_file_location("_check_scripts_guard_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git_init_scripts(root: Path) -> None:
    """`measured`'s own population source is `git ls-files`, so the
    synthetic tree needs a real (throwaway) git repo, not just files on
    disk -- an untracked file would silently vanish from the population,
    which would make this test pass for the wrong reason."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"],
        cwd=root, check=True,
    )


_GUARDED = (
    "from verify_env_identity import guard_bare_script_or_exit\n"
    "guard_bare_script_or_exit()\n"
    "import reyn\n"
)
_UNGUARDED = "import reyn\n"
_NO_REYN = "import os\n"
_LAZY_UNGUARDED = "def f():\n    from reyn.config import load_config\n    return load_config\n"
_SUBPROCESS_ONLY = (
    'code = "from reyn.config import load_config"\n'
    'import subprocess\n'
    'subprocess.run(["python3", "-c", code])\n'
)


def test_imports_reyn_true_for_module_level_import(tmp_path: Path) -> None:
    """Tier 1: accept -- a plain module-level `import reyn`."""
    module = _load()
    f = tmp_path / "a.py"
    f.write_text(_UNGUARDED)
    assert module.imports_reyn(f) is True


def test_imports_reyn_true_for_lazy_nested_import(tmp_path: Path) -> None:
    """Tier 1: accept -- a `from reyn...` import several nesting levels
    deep (inside a function body) is still found -- the guard's own
    point is that ONE call covers every such site regardless of depth,
    so this gate's population must see them too."""
    module = _load()
    f = tmp_path / "a.py"
    f.write_text(_LAZY_UNGUARDED)
    assert module.imports_reyn(f) is True


def test_imports_reyn_false_for_a_string_literal_mentioning_reyn(tmp_path: Path) -> None:
    """Tier 1: deny -- a script that builds `from reyn...` as TEXT for a
    separate `python -c` subprocess (this process's own `import reyn`
    never happens) is correctly excluded -- the real s7_driver.py /
    rekey_fixtures.py shape this gate's own module docstring names."""
    module = _load()
    f = tmp_path / "a.py"
    f.write_text(_SUBPROCESS_ONLY)
    assert module.imports_reyn(f) is False


def test_imports_reyn_false_when_reyn_is_never_mentioned(tmp_path: Path) -> None:
    """Tier 1: deny, plain negative control."""
    module = _load()
    f = tmp_path / "a.py"
    f.write_text(_NO_REYN)
    assert module.imports_reyn(f) is False


def test_calls_guard_true_when_the_call_is_present(tmp_path: Path) -> None:
    """Tier 1: accept."""
    module = _load()
    f = tmp_path / "a.py"
    f.write_text(_GUARDED)
    assert module.calls_guard(f) is True


def test_calls_guard_false_when_absent(tmp_path: Path) -> None:
    """Tier 1: deny."""
    module = _load()
    f = tmp_path / "a.py"
    f.write_text(_UNGUARDED)
    assert module.calls_guard(f) is False


def test_measured_flags_an_unguarded_reyn_importing_script(tmp_path: Path) -> None:
    """Tier 2: integration -- a synthetic `scripts/` tree with one
    unguarded reyn-importing file is reported as `missing`, driven
    through the real `measured()` (not the unit pieces in isolation).

    FALSIFY: strip `calls_guard`'s own AST walk down to `return False`
    unconditionally -- `test_calls_guard_true_when_the_call_is_present`
    above goes red first, which is the more precise witness; this test
    additionally proves the wiring from file -> `measured()`'s own
    verdict, not just the unit function."""
    module = _load()
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "verify_env_identity.py").write_text("# stand-in, no reyn import\n")
    (scripts / "unguarded_gate.py").write_text(_UNGUARDED)
    (scripts / "guarded_gate.py").write_text(_GUARDED)
    _git_init_scripts(root)

    missing, stale, ordering, scanned = module.measured(root)

    assert scanned == 3
    assert missing == ["unguarded_gate.py"]
    assert stale == []
    assert ordering == []


def test_measured_respects_the_exempt_scripts_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: an unguarded script NAMED in EXEMPT_SCRIPTS is not flagged
    as missing."""
    module = _load()
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "verify_env_identity.py").write_text("# stand-in\n")
    (scripts / "deliberately_cross_tree.py").write_text(_UNGUARDED)
    _git_init_scripts(root)
    monkeypatch.setattr(
        module, "EXEMPT_SCRIPTS", {"deliberately_cross_tree.py": "reasoned exception, test-only"},
    )

    missing, stale, ordering, scanned = module.measured(root)

    assert missing == []
    assert stale == []
    assert ordering == []


def test_measured_flags_a_stale_exemption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: deny -- an EXEMPT_SCRIPTS entry whose file no longer
    imports `reyn` at all is a genuine finding (the exemption's own
    reason no longer applies), not a silent pass."""
    module = _load()
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "verify_env_identity.py").write_text("# stand-in\n")
    (scripts / "no_longer_reyn.py").write_text(_NO_REYN)
    _git_init_scripts(root)
    monkeypatch.setattr(
        module, "EXEMPT_SCRIPTS", {"no_longer_reyn.py": "used to import reyn, no longer does"},
    )

    missing, stale, ordering, scanned = module.measured(root)

    assert missing == []
    assert stale == ["no_longer_reyn.py"]
    assert ordering == []


_ORDERING_VIOLATION = (
    "import sys\n"
    "from verify_env_identity import guard_bare_script_or_exit\n"
    "guard_bare_script_or_exit()\n"
    "sys.path.insert(0, '/some/src')\n"
    "import reyn\n"
)
_ORDERING_CORRECT = (
    "import sys\n"
    "sys.path.insert(0, '/some/src')\n"
    "from verify_env_identity import guard_bare_script_or_exit\n"
    "guard_bare_script_or_exit()\n"
    "import reyn\n"
)


def test_guard_precedes_own_path_bootstrap_flags_the_false_reject_shape(tmp_path: Path) -> None:
    """Tier 1: accept -- #3024 BLOCKING (lead-coder, PR #6138): a guard
    call positioned BEFORE a same-scope `sys.path.insert` is a real
    accept-side finding (a script that self-bootstraps its own tree
    would wrongly reject a normal, uninstalled-reyn run). This is the
    exact shape `audit_event_firing_condition_gate.py` /
    `mcp_conformance.py` / 6 others had before the fix."""
    module = _load()
    f = tmp_path / "a.py"
    f.write_text(_ORDERING_VIOLATION)
    assert module.guard_precedes_own_path_bootstrap(f) == 4


def test_guard_precedes_own_path_bootstrap_clean_when_guard_is_last(tmp_path: Path) -> None:
    """Tier 1: deny (negative control) -- the FIXED shape: `sys.path.
    insert` first, guard after."""
    module = _load()
    f = tmp_path / "a.py"
    f.write_text(_ORDERING_CORRECT)
    assert module.guard_precedes_own_path_bootstrap(f) is None


def test_measured_flags_an_ordering_violation(tmp_path: Path) -> None:
    """Tier 2: integration -- driven through `measured()`, not just the
    unit function in isolation.

    FALSIFY (performed during review, file Edit only): reverted
    `scripts/spike_preflight.py`'s own guard to BEFORE its
    `sys.path.insert` -- `check_scripts_import_identity_guard.py`
    reported it by name; reverted, confirmed green again."""
    module = _load()
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "verify_env_identity.py").write_text("# stand-in\n")
    (scripts / "misordered_gate.py").write_text(_ORDERING_VIOLATION)
    _git_init_scripts(root)

    missing, stale, ordering, scanned = module.measured(root)

    assert missing == []
    assert ordering == [("misordered_gate.py", 4)]


def test_the_real_scan_against_the_current_tree_has_no_missing_or_stale() -> None:
    """Tier 1: the load-bearing witness -- running the real scan against
    the real repo tree, right now, must find nothing outstanding.
    Mirrors `user_facing_lang_gate.py`'s own identically-shaped test."""
    from tests._support.paths import REPO_ROOT

    module = _load()
    missing, stale, ordering, scanned = module.measured(REPO_ROOT)
    assert scanned > 0, "the scan found 0 files -- a scanner failure, not a clean population"
    assert missing == []
    assert stale == []
    assert ordering == []
