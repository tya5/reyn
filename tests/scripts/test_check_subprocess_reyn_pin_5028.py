"""Tier 1: scripts/check_subprocess_reyn_pin.py's population/ratchet contract.

Same skeleton as `tests/scripts/test_check_tests_path_literal_reference_4065.py`
(itself following `test_3726_mypy_ratchet.py`) — a committed baseline set
only ever shrinks; a measured file not in it is new and must be surfaced, a
file that silently leaves the measured set (migrated to the fixture, or
deleted) is not itself reported. Here the measured set is `tests/**/*.py`
files spawning `sys.executable` without declaring `out_of_process_reyn`/
`reyn_console_scripts`, rather than path literals or mypy errors, but
`new_files` is the same `measured - baseline` shape.

Real filesystem + real `git` fixtures (a real `tmp_path` tree, `git init` +
`git add`) for the population/scan tests — no mocks, the whole point is
these are pure functions over real text/files and the real tracked-file
population.
"""
from __future__ import annotations

import json
import subprocess

from scripts.check_subprocess_reyn_pin import (
    _BASELINE_PATH,
    _ROOT,
    gap_files,
    load_baseline,
    new_files,
)

# `tests/scripts/test_check_subprocess_reyn_pin_5028.py`'s own source
# mentions `sys.executable` and the two fixture names in prose above — split
# so this file's own text never contains a run the scanner under test would
# match against ITSELF (same defensive split `test_check_tests_path_literal_
# reference_4065.py` uses for its own `tests/`-shaped fixture literals,
# #4068).
_SPAWN = "sys" + ".executable"
_DECLARED = "out_of_process_reyn"


def _init_repo(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)


# ── gap_files — the population scan ─────────────────────────────────────────


def test_a_spawn_without_the_fixture_is_in_the_gap(tmp_path) -> None:
    """Tier 1: the gate's whole reason to exist — a test file spawning
    `sys.executable` with no declaration of either fixture is a gap file."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text(
        f"proc = subprocess.run([{_SPAWN}, '-c', 'import reyn'])\n", encoding="utf-8",
    )
    _init_repo(tmp_path)
    assert gap_files(tmp_path) == {"tests/test_a.py"}


def test_a_spawn_declaring_out_of_process_reyn_is_not_in_the_gap(tmp_path) -> None:
    """Tier 1: a file requesting `out_of_process_reyn` anywhere in its own
    text is not a gap file — the fixture request is the declaration this
    gate exists to require, not any particular usage shape of it."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text(
        f"def test_x({_DECLARED}):\n"
        f"    proc = subprocess.run([{_SPAWN}, '-c', 'import reyn'])\n",
        encoding="utf-8",
    )
    _init_repo(tmp_path)
    assert gap_files(tmp_path) == set()


def test_a_spawn_declaring_reyn_console_scripts_is_not_in_the_gap(tmp_path) -> None:
    """Tier 1: the second declaration form — a test running a
    `[project.scripts]` entry by name requests `reyn_console_scripts`
    instead, and that also satisfies the gate."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text(
        f"def test_x(reyn_console_scripts):\n"
        f"    proc = subprocess.run([{_SPAWN}, 'reyn'])\n",
        encoding="utf-8",
    )
    _init_repo(tmp_path)
    assert gap_files(tmp_path) == set()


def test_a_file_with_no_spawn_at_all_is_not_in_the_gap(tmp_path) -> None:
    """Tier 1: a test file that never spawns `sys.executable` at all is
    outside the population entirely, regardless of fixture declarations."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("assert 1 + 1 == 2\n", encoding="utf-8")
    _init_repo(tmp_path)
    assert gap_files(tmp_path) == set()


def test_an_untracked_file_is_not_scanned(tmp_path) -> None:
    """Tier 1: the population is `git ls-files tests`, not a directory
    walk — an untracked file must not contribute to the gap."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text(
        f"proc = subprocess.run([{_SPAWN}, '-c', 'import reyn'])\n", encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # test_a.py is never `git add`-ed — untracked.
    assert gap_files(tmp_path) == set()


def test_a_non_python_tracked_file_is_not_scanned(tmp_path) -> None:
    """Tier 1: scope is `.py` files under `tests/` — a tracked non-Python
    file (a fixture data file, a README) mentioning `sys.executable` in
    prose must not contribute."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "README.md").write_text(f"uses {_SPAWN} internally\n", encoding="utf-8")
    _init_repo(tmp_path)
    assert gap_files(tmp_path) == set()


# ── new_files — the ratchet check itself ────────────────────────────────────


def test_a_file_in_the_baseline_is_not_new() -> None:
    """Tier 1: grandfathered debt does not fail the gate."""
    baseline = {"tests/core/test_x.py"}
    measured = {"tests/core/test_x.py"}
    assert new_files(measured, baseline) == set()


def test_a_file_absent_from_the_baseline_is_new() -> None:
    """Tier 1: the load-bearing case — a NEW file entering the gap after
    the baseline was written must be caught."""
    baseline = {"tests/core/test_x.py"}
    measured = {"tests/core/test_x.py", "tests/core/test_new_gap.py"}
    assert new_files(measured, baseline) == {"tests/core/test_new_gap.py"}


def test_a_file_leaving_the_measured_set_is_not_reported() -> None:
    """Tier 1: a fix (migrating to the fixture, or deleting the file)
    silently drops out — nothing has to be edited in the baseline to let a
    fix "count", same discipline as check_tests_path_literal_reference.py's
    own ratchet."""
    baseline = {"tests/core/test_x.py", "tests/core/test_fixed.py"}
    measured = {"tests/core/test_x.py"}
    assert new_files(measured, baseline) == set()


# ── the real committed baseline ─────────────────────────────────────────────


def test_the_real_baseline_has_no_new_files_against_the_current_tree() -> None:
    """Tier 1: the load-bearing witness — running the real scan against the
    real repo tree, right now, must find nothing beyond what's baselined.
    This is the gate itself, run as a test."""
    baseline = load_baseline()
    measured = gap_files(_ROOT)
    assert new_files(measured, baseline) == set()


def test_baseline_is_a_flat_list_of_existing_tracked_files() -> None:
    """Tier 1: schema check — every baseline entry is a string naming a
    file that still exists in the tree (a stale entry silently narrows
    what the ratchet protects, with nothing surfacing that it happened)."""
    data = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    assert data, "baseline must not be empty — a real, measured population"
    for entry in data:
        assert isinstance(entry, str)
        assert (_ROOT / entry).is_file(), entry
