"""Tier 2: #4008 — the bare-import-reference gate (the sys.path-dependent
sibling of #3995/#4002/#4019's `__file__`-depth class).

Real filesystem fixtures throughout (a real `tmp_path` tree of `.py`
files, real directories) — the function under test reads real file
content and compares real module names, so faking the filesystem would
test nothing real.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_bare_tests_import_reference import (
    flat_module_basenames,
    offending_files,
)


def test_a_nested_bare_import_of_an_existing_flat_module_is_flagged(
    tmp_path: Path,
) -> None:
    """Tier 2: THE real-world instance — a nested consumer bare-imports a
    name that matches an existing flat tests/*.py module. Resolves today
    only because pytest's prepend import mode puts the CONSUMER's own
    directory on sys.path; breaks the moment the consumer moves again."""
    (tmp_path / "_async_wait.py").write_text("def wait_until(): ...\n", encoding="utf-8")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "test_a.py").write_text(
        "from _async_wait import wait_until\n", encoding="utf-8"
    )
    offenders = offending_files(tmp_path)
    assert offenders == [(hooks / "test_a.py", ["_async_wait"])]


def test_a_flat_files_own_bare_import_is_never_flagged(tmp_path: Path) -> None:
    """Tier 2: a FLAT file's own directory already IS tests_dir, so its own
    bare import of a flat sibling is correct today and stays correct
    regardless of what else moves — only NESTED consumers are in scope."""
    (tmp_path / "_async_wait.py").write_text("def wait_until(): ...\n", encoding="utf-8")
    (tmp_path / "test_a.py").write_text(
        "from _async_wait import wait_until\n", encoding="utf-8"
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_a_dotted_import_of_a_real_package_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: `from reyn.core.x import y` (a real installed package, not a
    flat tests/ sibling) must not be flagged — the check only fires when
    the FIRST dotted component matches an EXISTING flat tests/*.py
    basename, not any bare-looking import."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "test_a.py").write_text(
        "from reyn.core.kernel import module_is_allowed\n", encoding="utf-8"
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_an_explicit_tests_prefixed_import_is_not_flagged(tmp_path: Path) -> None:
    """Tier 2: the FIX this gate steers toward — `from tests._support.x
    import y` (an explicit, depth-independent import, level==0 but the
    top-level component is `tests`, never a flat module basename) is not
    flagged, even though the target module (`async_wait.py`) also happens
    to exist flat somewhere unrelated in this fixture."""
    (tmp_path / "async_wait.py").write_text("def wait_until(): ...\n", encoding="utf-8")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "test_a.py").write_text(
        "from tests._support.async_wait import wait_until\n", encoding="utf-8"
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_a_relative_import_is_never_flagged(tmp_path: Path) -> None:
    """Tier 2: a relative import (`from . import x`, `level > 0`) is a
    different, already-depth-safe mechanism (resolves relative to the
    importing package, not sys.path) — out of this gate's scope by
    construction (`node.level == 0` in the predicate)."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "__init__.py").write_text("", encoding="utf-8")
    (hooks / "_local.py").write_text("X = 1\n", encoding="utf-8")
    (hooks / "test_a.py").write_text("from ._local import X\n", encoding="utf-8")
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_conftest_is_structurally_exempt_at_any_depth(tmp_path: Path) -> None:
    """Tier 2: conftest.py never moves (pytest resolves the nearest one to
    the file being collected) — the same structural exemption
    check_file_depth_reference.py grants it, for the identical reason."""
    (tmp_path / "_async_wait.py").write_text("def wait_until(): ...\n", encoding="utf-8")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "conftest.py").write_text(
        "from _async_wait import wait_until\n", encoding="utf-8"
    )
    offenders = offending_files(tmp_path)
    assert offenders == []


def test_the_check_needs_no_curated_module_name(tmp_path: Path) -> None:
    """Tier 2: non-vacuity — a flat module name NEVER SEEN before (not
    `_async_wait`, not anything hardcoded anywhere) is still caught,
    because the check is a structural comparison against the real,
    current flat population, not a name-membership test against a list."""
    (tmp_path / "some_brand_new_flat_module_xyz.py").write_text(
        "X = 1\n", encoding="utf-8"
    )
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "test_a.py").write_text(
        "from some_brand_new_flat_module_xyz import X\n", encoding="utf-8"
    )
    offenders = offending_files(tmp_path)
    assert offenders == [(hooks / "test_a.py", ["some_brand_new_flat_module_xyz"])]


def test_flat_module_basenames_excludes_init(tmp_path: Path) -> None:
    """Tier 2: __init__.py is a package marker, not an importable module
    name a bare import would ever name — excluded from the flat set."""
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "test_a.py").write_text("X = 1\n", encoding="utf-8")
    assert flat_module_basenames(tmp_path) == {"test_a"}


def test_the_real_repo_tree_is_currently_clean() -> None:
    """Tier 2: the gate's own starting population — verified against the
    real, current tree (not assumed), matching check_file_depth_reference.py's
    own "run it before shipping it" discipline. 19 real instances were
    fixed the same night this gate was designed (#4008); this asserts they
    stayed fixed."""
    from scripts.check_bare_tests_import_reference import _ROOT, _TESTS_DIR

    assert _TESTS_DIR == _ROOT / "tests"
    offenders = offending_files(_TESTS_DIR)
    assert offenders == [], (
        f"real regression(s) found: {offenders} — this gate's baseline is "
        "zero, so any hit here is new, not inherited debt"
    )
