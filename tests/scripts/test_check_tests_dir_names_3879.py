"""Tier 2: #3879 shadow+vocabulary gate (scripts/check_tests_dir_names.py).

Placed in tests/scripts/ (not flat) — this file is itself new, obeying the
ratchet it sits alongside.

Real filesystem + real subprocess/git throughout (no mocks) — the functions
under test are thin wrappers over the filesystem, AST parsing, and git, so
faking any of them would test nothing real.

★ Falsify-verified directly against the REAL repo (not only in these
isolated fixtures) before this file was written: creating a real
``tests/scripts/__init__.py`` made ``check_tests_dir_names.py`` genuinely
RED for the shadow reason, and separately made two already-existing tests
(``tests/scripts/test_swe_bench_runner_venv_183.py``,
``tests/scripts/test_verify_package_move_root_config.py``) fail collection with
``ModuleNotFoundError: No module named 'scripts.swe_bench_runner'`` — the
exact real-world breakage the shadow check exists to predict, not a guessed
one. The working tree was restored and confirmed clean afterward.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import scripts.check_tests_dir_names as check_tests_dir_names
from scripts.check_tests_dir_names import (
    baseline_at_ref,
    current_tests_dir_names,
    imported_top_level_names,
    is_allowed_new_name,
    load_baseline,
    main,
    new_dir_names,
    real_src_packages,
    repo_root_dir_names,
    shadowed_names,
    write_baseline,
)


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── current_tests_dir_names / real_src_packages ─────────────────────────────


def test_current_tests_dir_names_excludes_pycache(tmp_path: Path) -> None:
    """Tier 2: __pycache__ is not a candidate directory name."""
    tests_dir = tmp_path / "tests"
    _write(tests_dir / "core" / "test_a.py", "# t\n")
    (tests_dir / "__pycache__").mkdir(parents=True)
    assert current_tests_dir_names(tests_dir) == {"core"}


def test_real_src_packages_requires_init_py(tmp_path: Path) -> None:
    """Tier 2: a directory without __init__.py is not a real package — a
    plain data directory under src/reyn/ (if one existed) would not count."""
    src_reyn = tmp_path / "src" / "reyn"
    _write(src_reyn / "core" / "__init__.py")
    (src_reyn / "not_a_package").mkdir(parents=True)
    assert real_src_packages(src_reyn) == {"core"}


# ── imported_top_level_names / repo_root_dir_names ──────────────────────────


def test_imported_top_level_names_reads_both_import_forms(tmp_path: Path) -> None:
    """Tier 2: both `import X` and `from X.y import z` contribute X's root
    name — matches how real code in this repo imports `scripts.foo`."""
    _write(
        tmp_path / "tests" / "test_uses_scripts.py",
        "import scripts.verify_package_move\nfrom scripts.other import thing\n",
    )
    assert imported_top_level_names(tmp_path) == {"scripts"}


def test_imported_top_level_names_ignores_relative_imports(tmp_path: Path) -> None:
    """Tier 2: a relative import (`from . import x`) has no top-level
    module name to extract — must not crash or contribute a bogus entry."""
    _write(tmp_path / "src" / "pkg" / "mod.py", "from . import sibling\n")
    assert imported_top_level_names(tmp_path) == set()


def test_repo_root_dir_names_excludes_machinery(tmp_path: Path) -> None:
    """Tier 2: .git/.venv/tests/src etc. are never candidate collision
    names — only real top-level code/data directories are."""
    for name in (".git", ".venv", "tests", "src", "__pycache__"):
        (tmp_path / name).mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "dogfood").mkdir()
    assert repo_root_dir_names(tmp_path) == {"scripts", "dogfood"}


# ── shadowed_names — the shadow mechanism itself ────────────────────────────


def test_shadowed_names_is_empty_when_no_init_py(tmp_path: Path) -> None:
    """Tier 2: a tests/<name>/ dir with the SAME name as an imported
    repo-root package, but with NO __init__.py, is a namespace-package
    portion — it merges, it doesn't shadow. This is `tests/scripts/`'s
    actual current state in the real repo (0 collisions)."""
    _write(tmp_path / "scripts" / "real_module.py", "x = 1\n")
    _write(tmp_path / "tests" / "test_uses_scripts.py", "import scripts.real_module\n")
    (tmp_path / "tests" / "scripts").mkdir(parents=True, exist_ok=True)
    # No __init__.py written under tests/scripts/.
    assert shadowed_names(tmp_path / "tests", tmp_path) == set()


def test_shadowed_names_catches_a_real_collision(tmp_path: Path) -> None:
    """Tier 2: the gate's own reason to exist — tests/scripts/__init__.py
    existing, WITH scripts actually imported elsewhere, must be caught.
    Mirrors the real repo's `scripts` collision exactly."""
    _write(tmp_path / "scripts" / "real_module.py", "x = 1\n")
    _write(tmp_path / "tests" / "test_uses_scripts.py", "import scripts.real_module\n")
    _write(tmp_path / "tests" / "scripts" / "__init__.py")
    assert shadowed_names(tmp_path / "tests", tmp_path) == {"scripts"}


def test_shadowed_names_ignores_an_unimported_name_collision(tmp_path: Path) -> None:
    """Tier 2: a tests/<name>/__init__.py whose name matches a repo-root
    directory that NOTHING actually imports is not a real collision — this
    is what keeps dogfood/pipelines/docs/etc. from being false positives
    (verified directly against the real repo: every non-hidden root
    directory resolves via importlib.util.find_spec as a namespace package
    regardless of content, so bare resolvability is not the right signal)."""
    (tmp_path / "dogfood").mkdir()  # exists, nothing imports it
    _write(tmp_path / "tests" / "dogfood" / "__init__.py")
    assert shadowed_names(tmp_path / "tests", tmp_path) == set()


# ── is_allowed_new_name — vocabulary rule ①② ────────────────────────────────


def test_is_allowed_new_name_accepts_a_real_src_package() -> None:
    """Tier 2: rule ① — a name mirroring a real src/reyn/<name>/ package."""
    assert is_allowed_new_name("runtime", {"runtime", "core"}) is True


def test_is_allowed_new_name_accepts_the_repo_special_case() -> None:
    """Tier 2: rule ① — the `repo` special case needs no src/reyn mirror."""
    assert is_allowed_new_name("repo", set()) is True


def test_is_allowed_new_name_rejects_a_non_mirroring_name() -> None:
    """Tier 2: a name with no matching src/reyn/<name>/ package and not
    `repo` is rejected."""
    assert is_allowed_new_name("tui", {"runtime", "core"}) is False


def test_is_allowed_new_name_rejects_a_reserved_name_even_if_a_real_package_exists() -> None:
    """Tier 2: `web`/`cli`/`chat` are excluded regardless of rule ① — the
    real repo has src/reyn/cli/ and src/reyn/web/ as REAL packages, but the
    existing tests/cli/ and tests/web/ directories test a DIFFERENT package
    entirely (confirmed by grep: tests/cli/test_auth_login_ux.py imports
    reyn.interfaces.cli, not reyn.cli) — a coincidental name match, not a
    real mirror, so a NEW directory must not reuse the name either."""
    assert is_allowed_new_name("cli", {"cli", "runtime"}) is False
    assert is_allowed_new_name("web", {"web", "runtime"}) is False
    assert is_allowed_new_name("chat", {"chat", "runtime"}) is False
    assert is_allowed_new_name("scaffold", {"runtime"}) is False
    assert is_allowed_new_name("_support", {"runtime"}) is False


# ── new_dir_names — ratchet arithmetic ───────────────────────────────────────


def test_new_dir_names_is_measured_minus_baseline() -> None:
    """Tier 2: a name absent from the baseline is new; a baseline name
    absent from measured (a directory retired) is not reported at all —
    same silent-shrink contract as flat_tests_ratchet.new_flat_files."""
    measured = {"core", "runtime", "tui"}
    baseline = {"core", "runtime", "gone_via_rename"}
    assert new_dir_names(measured, baseline) == {"tui"}


def test_write_baseline_then_load_baseline_round_trips(tmp_path: Path) -> None:
    """Tier 2: the baseline file format round-trips through write/load, and
    is written sorted (stable diffs)."""
    path = tmp_path / "baseline.json"
    write_baseline({"core", "_support"}, path)
    assert load_baseline(path) == {"core", "_support"}
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == sorted(on_disk)


# ── baseline_at_ref — same shape as flat_tests_ratchet's own tests ──────────


@pytest.fixture
def _real_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "tests_dir_names_baseline.json").write_text(
        json.dumps(["core", "runtime"], indent=2) + "\n", encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=repo, check=True)
    return repo


def test_baseline_at_ref_reads_the_committed_content(_real_git_repo: Path) -> None:
    """Tier 2: reads the baseline as committed at a ref via a real `git
    show`, not the working tree's current (possibly dirtied) copy."""
    baseline_path = _real_git_repo / "scripts" / "tests_dir_names_baseline.json"
    baseline_path.write_text(json.dumps(["core"]), encoding="utf-8")
    committed = baseline_at_ref("HEAD", baseline_path, root=_real_git_repo)
    assert committed == {"core", "runtime"}


def test_baseline_at_ref_returns_none_when_the_ref_lacks_the_file(
    _real_git_repo: Path,
) -> None:
    """Tier 2: a ref with no baseline file at all (predates the gate) is
    not growth — nothing to grow FROM — and must not crash."""
    result = baseline_at_ref(
        "HEAD", _real_git_repo / "scripts" / "nonexistent.json", root=_real_git_repo,
    )
    assert result is None


# ── main() end-to-end — the gate's actual entry point ───────────────────────


@pytest.fixture
def _repo_tree(tmp_path: Path) -> Path:
    """A full fake repo tree: real src/reyn/ packages, a tests/ directory
    matching the baseline exactly, and a scripts/ package that IS imported
    elsewhere — mirrors the real repo's shape closely enough to drive
    main() honestly."""
    root = tmp_path / "repo"
    _write(root / "src" / "reyn" / "core" / "__init__.py")
    _write(root / "src" / "reyn" / "runtime" / "__init__.py")
    _write(root / "scripts" / "real_module.py", "x = 1\n")
    _write(root / "tests" / "test_uses_scripts.py", "import scripts.real_module\n")
    _write(root / "tests" / "core" / "__init__.py")
    write_baseline({"core"}, root / "scripts" / "tests_dir_names_baseline.json")
    return root


def _patch(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(check_tests_dir_names, "_ROOT", root)
    monkeypatch.setattr(check_tests_dir_names, "_TESTS_DIR", root / "tests")
    monkeypatch.setattr(check_tests_dir_names, "_SRC_REYN", root / "src" / "reyn")
    monkeypatch.setattr(
        check_tests_dir_names, "_BASELINE_PATH",
        root / "scripts" / "tests_dir_names_baseline.json",
    )


def test_main_is_green_on_a_baseline_matching_tree(
    monkeypatch: pytest.MonkeyPatch, _repo_tree: Path,
) -> None:
    """Tier 2: requirement ① from #3879's task — current state (tests/
    matches the baseline exactly, no shadow) must be green."""
    _patch(monkeypatch, _repo_tree)
    assert main([]) == 0


def test_main_is_red_when_a_baselined_dir_gains_an_init_py_shadowing_scripts(
    monkeypatch: pytest.MonkeyPatch, _repo_tree: Path,
) -> None:
    """Tier 2: requirement ② from #3879's task, executed for real — adding
    tests/scripts/__init__.py (the exact real-repo scenario, falsify-verified
    directly against the actual repo before this test was written; see
    module docstring) makes main() reject it.

    `scripts` is put in the baseline FIRST (grandfathered — mirrors the real
    repo's `tests/scripts/` already existing) so the vocabulary check has
    nothing to say about it; only the shadow check can fail this. Without
    this isolation, this test stayed GREEN even with `shadowed_names`
    stripped to always return an empty set — the vocabulary check (`scripts`
    being both new AND not a real src/reyn package) caught it independently,
    masking a dead shadow mechanism (caught by this file's own
    falsify-verification pass, not assumed correct)."""
    _patch(monkeypatch, _repo_tree)
    write_baseline({"core", "scripts"}, _repo_tree / "scripts" / "tests_dir_names_baseline.json")
    _write(_repo_tree / "tests" / "scripts" / "__init__.py")
    assert main([]) != 0


def test_main_is_red_for_a_new_directory_that_does_not_mirror_src(
    monkeypatch: pytest.MonkeyPatch, _repo_tree: Path,
) -> None:
    """Tier 2: a NEW tests/<name>/ with no matching src/reyn/<name>/ package
    is rejected by the vocabulary check."""
    _patch(monkeypatch, _repo_tree)
    (_repo_tree / "tests" / "tui").mkdir()
    assert main([]) != 0


def test_main_is_green_for_a_new_directory_that_mirrors_src(
    monkeypatch: pytest.MonkeyPatch, _repo_tree: Path,
) -> None:
    """Tier 2: a NEW tests/<name>/ that DOES mirror a real src/reyn/<name>/
    package is accepted — the vocabulary check does not block legitimate
    Stage-1 additions."""
    _patch(monkeypatch, _repo_tree)
    (_repo_tree / "tests" / "runtime").mkdir()
    assert main([]) == 0


def test_main_check_growth_rejects_a_hand_edited_baseline(
    monkeypatch: pytest.MonkeyPatch, _repo_tree: Path,
) -> None:
    """Tier 2: same hand-edit-the-baseline loophole flat_tests_ratchet.py
    closes, applied here — pre-authorizing a name via the baseline file
    alone (no corresponding real directory needed for this check) must be
    rejected against a base ref."""
    _patch(monkeypatch, _repo_tree)
    subprocess.run(["git", "init", "-q"], cwd=_repo_tree, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=_repo_tree, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=_repo_tree, check=True)
    subprocess.run(["git", "add", "."], cwd=_repo_tree, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=_repo_tree, check=True)

    write_baseline({"core", "hand_added"}, _repo_tree / "scripts" / "tests_dir_names_baseline.json")
    monkeypatch.chdir(_repo_tree)

    assert main(["--check-growth", "HEAD"]) != 0


def test_main_check_growth_allows_a_shrunk_baseline(
    monkeypatch: pytest.MonkeyPatch, _repo_tree: Path,
) -> None:
    """Tier 2: the other side — a baseline that only shrank (a directory
    genuinely retired) must not be rejected by --check-growth."""
    _patch(monkeypatch, _repo_tree)
    subprocess.run(["git", "init", "-q"], cwd=_repo_tree, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=_repo_tree, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=_repo_tree, check=True)
    write_baseline({"core", "runtime"}, _repo_tree / "scripts" / "tests_dir_names_baseline.json")
    subprocess.run(["git", "add", "."], cwd=_repo_tree, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=_repo_tree, check=True)

    write_baseline({"core"}, _repo_tree / "scripts" / "tests_dir_names_baseline.json")
    monkeypatch.chdir(_repo_tree)

    assert main(["--check-growth", "HEAD"]) == 0
