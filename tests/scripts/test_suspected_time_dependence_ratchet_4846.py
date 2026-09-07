"""Tier 2: #4846 — the suspected-time-dependence ratchet
(scripts/suspected_time_dependence_ratchet.py).

Real filesystem + real git repos throughout (`_iter_scan_files` runs a real
`git ls-files` subprocess) — no mocks; the module under test is itself a
thin AST walk over real file text plus a real git population source, so
faking either would test nothing real.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import scripts.suspected_time_dependence_ratchet as ratchet
from scripts.suspected_time_dependence_ratchet import (
    grown_files,
    load_baseline,
    main,
    suspected_counts,
    write_baseline,
)

# Every illustrative baseline-key literal below is spelled `_T + "..."`
# rather than one contiguous string — same split
# `check_tests_path_literal_reference.py`'s own paired test file uses for
# the identical reason (see that test file's own `_T`): this ratchet's real
# keys genuinely carry a `tests/` prefix (`measured()`'s `rel =
# str(path.relative_to(root))`, unlike `flat_tests_ratchet`'s bare
# basenames), so a fake-but-realistic example key here would otherwise BE
# a `tests/....py`-shaped literal and register as a new hit against that
# other gate's own whole-repo scan.
_T = "tests/"

# ── suspected_counts() — the three AST detectors, one shape each ───────────


def test_sleep_zero_is_not_counted(tmp_path: Path) -> None:
    """Tier 2: `time.sleep(0)`/`asyncio.sleep(0)` is a cooperative yield, not
    a wait — the one shape this ratchet positively clears rather than merely
    failing to flag."""
    p = tmp_path / "test_a.py"
    p.write_text(
        "import time\n"
        "import asyncio\n"
        "def test_x():\n"
        "    time.sleep(0)\n"
        "async def test_y():\n"
        "    await asyncio.sleep(0.0)\n",
        encoding="utf-8",
    )
    assert suspected_counts(p) == (0, 0)


def test_sleep_nonzero_literal_counts_as_floor(tmp_path: Path) -> None:
    """Tier 2: a `sleep()` call whose argument is NOT provably zero (a
    nonzero literal here) is suspected floor — the AST cannot know whether
    an assert depends on it, so it counts rather than assumes innocence."""
    p = tmp_path / "test_a.py"
    p.write_text("import time\ndef test_x():\n    time.sleep(2)\n", encoding="utf-8")
    assert suspected_counts(p) == (0, 1)


def test_sleep_with_a_variable_argument_still_counts_as_floor(tmp_path: Path) -> None:
    """Tier 2: `sleep(x)` where `x` is a name, not a literal, is NOT
    provably zero either — `_is_provably_zero` only ever clears the literal
    `0`/`0.0` shape, never a name or expression, so this must still count."""
    p = tmp_path / "test_a.py"
    p.write_text(
        "import time\ndef test_x(delay):\n    time.sleep(delay)\n", encoding="utf-8",
    )
    assert suspected_counts(p) == (0, 1)


def test_pytest_mark_timeout_counts_as_ceiling(tmp_path: Path) -> None:
    """Tier 2: `@pytest.mark.timeout(N)` is pure syntax — the shape itself
    is the ceiling violation, no semantic question needed."""
    p = tmp_path / "test_a.py"
    p.write_text(
        "import pytest\n@pytest.mark.timeout(5)\ndef test_x():\n    pass\n",
        encoding="utf-8",
    )
    assert suspected_counts(p) == (1, 0)


def test_range_loop_with_await_in_body_counts_as_ceiling(tmp_path: Path) -> None:
    """Tier 2: `for _ in range(N): await ...` is the "bounded polling retry"
    shape from the design brief's own population — a wait wrapped in a
    bounded iteration count, syntactically certain."""
    p = tmp_path / "test_a.py"
    p.write_text(
        "async def test_x():\n"
        "    for _ in range(200):\n"
        "        await something()\n",
        encoding="utf-8",
    )
    assert suspected_counts(p) == (1, 0)


def test_range_loop_with_sleep_in_body_counts_as_ceiling(tmp_path: Path) -> None:
    """Tier 2: the same shape via a `sleep()`/`pause()` call inside the loop
    body rather than an `await` — a synchronous polling retry."""
    p = tmp_path / "test_a.py"
    p.write_text(
        "import time\n"
        "def test_x():\n"
        "    for _ in range(50):\n"
        "        time.sleep(0.1)\n",
        encoding="utf-8",
    )
    ceiling, floor = suspected_counts(p)
    assert ceiling == 1
    # The inner sleep(0.1) is ALSO a real floor call in its own right —
    # the two detectors are independent, both may fire on the same line.
    assert floor == 1


def test_range_loop_over_plain_data_is_not_counted(tmp_path: Path) -> None:
    """Tier 2: `for _ in range(N): <no wait>` — plain data iteration, the
    census's own "harmless" bucket — must not be flagged; this is the
    discriminator between "iterates N times" and "waits, bounded by N"."""
    p = tmp_path / "test_a.py"
    p.write_text(
        "def test_x():\n"
        "    total = 0\n"
        "    for i in range(10):\n"
        "        total += i\n",
        encoding="utf-8",
    )
    assert suspected_counts(p) == (0, 0)


def test_unparseable_file_counts_as_nothing(tmp_path: Path) -> None:
    """Tier 2: a file this ratchet cannot even parse contributes zero to
    either count — silently, not as a crash — a parse failure here is a
    DIFFERENT gate's problem (ruff/CI), not this one's to raise on."""
    p = tmp_path / "test_a.py"
    p.write_text("def test_x(:\n    pass\n", encoding="utf-8")
    assert suspected_counts(p) == (0, 0)


# ── grown_files() — the ratchet's core arithmetic ──────────────────────────


def test_grown_files_flags_an_existing_file_whose_count_increased() -> None:
    """Tier 2: a file already in the baseline whose measured count exceeds
    its baseline count is new debt."""
    assert grown_files({_T + "a.py": 3}, {_T + "a.py": 2}) == {_T + "a.py": (2, 3)}


def test_grown_files_ignores_an_existing_file_whose_count_is_unchanged() -> None:
    """Tier 2: no growth, no report — the common, healthy case."""
    assert grown_files({_T + "a.py": 2}, {_T + "a.py": 2}) == {}


def test_grown_files_treats_a_file_absent_from_baseline_as_grown_from_zero() -> None:
    """Tier 2: a file carrying a nonzero suspected count with NO baseline
    entry at all is compared against 0 — a brand-new file introducing a
    suspected site is new debt exactly like an existing file's count rising."""
    assert grown_files({_T + "new.py": 1}, {}) == {_T + "new.py": (0, 1)}


def test_grown_files_silently_allows_a_shrink() -> None:
    """Tier 2: a file whose count DROPPED (or vanished from `measured`
    entirely, a fix or a deletion) is never reported — the silent-shrink
    contract every ratchet in this repo shares."""
    assert grown_files({_T + "a.py": 1}, {_T + "a.py": 3}) == {}
    assert grown_files({}, {_T + "a.py": 3}) == {}


# ── baseline round-trip ─────────────────────────────────────────────────────


def test_write_baseline_then_load_baseline_round_trips(tmp_path: Path) -> None:
    """Tier 2: the two-section baseline format round-trips through
    write/load — catches a JSON-shape mismatch between the writer and the
    reader."""
    path = tmp_path / "baseline.json"
    write_baseline({_T + "b.py": 2, _T + "a.py": 1}, {_T + "c.py": 3}, path)
    ceiling, floor = load_baseline(path)
    assert ceiling == {_T + "a.py": 1, _T + "b.py": 2}
    assert floor == {_T + "c.py": 3}


# ── main() end-to-end, against a REAL git repo ──────────────────────────────
# Mirrors flat_tests_ratchet's own established pattern: `_iter_scan_files`
# runs a real `git ls-files`, so exercising `main()` needs a real repo with
# real tracked files, not a monkeypatched population.


@pytest.fixture
def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo


def _commit_tests_dir(repo: Path, files: "dict[str, str]") -> None:
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    for name, content in files.items():
        (tests_dir / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "tests"], cwd=repo, check=True)


def _write_baseline_file(repo: Path, ceiling: dict, floor: dict) -> Path:
    path = repo / "scripts"
    path.mkdir(exist_ok=True)
    baseline_path = path / "suspected_time_dependence_ratchet_baseline.json"
    baseline_path.write_text(
        json.dumps({"ceiling": ceiling, "floor": floor}, indent=2) + "\n", encoding="utf-8",
    )
    return baseline_path


def test_main_fails_when_the_scan_finds_zero_files(
    monkeypatch: pytest.MonkeyPatch, _git_repo: Path,
) -> None:
    """Tier 2: the vacuity guard — a repo with NO tracked `tests/*.py` files
    at all must fail loud, distinctly from "0 suspects, all baselined"
    (architect's own acceptance criterion: a baseline assert alone cannot
    tell "clean" from "the scanner broke").

    NON-VACUITY (falsification, verified locally): commenting out the
    `if scanned == 0: ... return 1` branch in `main()` makes this test FAIL
    (main proceeds to the normal baseline comparison, which reads an empty
    scan against an empty baseline as OK — exit 0), confirming the test
    depends on that branch specifically, not on any other failure path."""
    (_git_repo / "README.md").write_text("no tests here", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=_git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "no tests"], cwd=_git_repo, check=True)
    baseline_path = _write_baseline_file(_git_repo, {}, {})
    monkeypatch.setattr(ratchet, "_ROOT", _git_repo)
    monkeypatch.setattr(ratchet, "_BASELINE_PATH", baseline_path)
    monkeypatch.chdir(_git_repo)

    assert main([]) != 0


def test_main_passes_when_measured_matches_baseline(
    monkeypatch: pytest.MonkeyPatch, _git_repo: Path,
) -> None:
    """Tier 2: the healthy case — measured counts equal the committed
    baseline exactly, nothing grew."""
    _commit_tests_dir(
        _git_repo,
        {"test_a.py": "import time\ndef test_x():\n    time.sleep(2)\n"},
    )
    baseline_path = _write_baseline_file(_git_repo, {}, {_T + "test_a.py": 1})
    monkeypatch.setattr(ratchet, "_ROOT", _git_repo)
    monkeypatch.setattr(ratchet, "_BASELINE_PATH", baseline_path)
    monkeypatch.chdir(_git_repo)

    assert main([]) == 0


def test_main_fails_on_a_new_suspected_ceiling_site_in_a_new_file(
    monkeypatch: pytest.MonkeyPatch, _git_repo: Path,
) -> None:
    """Tier 2: architect's own acceptance criterion — adding
    `@pytest.mark.timeout` in a file the baseline has never seen goes red.

    NON-VACUITY (strip-falsify): removing the `@pytest.mark.timeout`
    decorator from the committed file below (so it becomes a plain
    function) makes this test's own premise false — `main()` returns 0
    instead — confirmed by running this exact scenario both ways locally
    before landing it."""
    _commit_tests_dir(
        _git_repo,
        {
            "test_new.py": (
                "import pytest\n@pytest.mark.timeout(5)\ndef test_x():\n    pass\n"
            ),
        },
    )
    baseline_path = _write_baseline_file(_git_repo, {}, {})
    monkeypatch.setattr(ratchet, "_ROOT", _git_repo)
    monkeypatch.setattr(ratchet, "_BASELINE_PATH", baseline_path)
    monkeypatch.chdir(_git_repo)

    assert main([]) != 0


def test_main_fails_when_an_existing_files_floor_count_rises(
    monkeypatch: pytest.MonkeyPatch, _git_repo: Path,
) -> None:
    """Tier 2: a file already in the baseline whose suspected floor count
    goes UP (a second `sleep()` added) fails, even though the file itself
    is not new."""
    _commit_tests_dir(
        _git_repo,
        {
            "test_a.py": (
                "import time\n"
                "def test_x():\n"
                "    time.sleep(1)\n"
                "    time.sleep(2)\n"
            ),
        },
    )
    baseline_path = _write_baseline_file(_git_repo, {}, {_T + "test_a.py": 1})
    monkeypatch.setattr(ratchet, "_ROOT", _git_repo)
    monkeypatch.setattr(ratchet, "_BASELINE_PATH", baseline_path)
    monkeypatch.chdir(_git_repo)

    assert main([]) != 0


def test_main_write_baseline_writes_current_measured_counts(
    monkeypatch: pytest.MonkeyPatch, _git_repo: Path,
) -> None:
    """Tier 2: `--write-baseline` regenerates the file from the current
    scan — the adoption/legitimate-growth path."""
    _commit_tests_dir(
        _git_repo,
        {"test_a.py": "import time\ndef test_x():\n    time.sleep(3)\n"},
    )
    baseline_path = _git_repo / "scripts" / "suspected_time_dependence_ratchet_baseline.json"
    baseline_path.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(ratchet, "_ROOT", _git_repo)
    monkeypatch.setattr(ratchet, "_BASELINE_PATH", baseline_path)
    monkeypatch.chdir(_git_repo)

    exit_code = main(["--write-baseline"])

    assert exit_code == 0
    ceiling, floor = load_baseline(baseline_path)
    assert ceiling == {}
    assert floor == {_T + "test_a.py": 1}
