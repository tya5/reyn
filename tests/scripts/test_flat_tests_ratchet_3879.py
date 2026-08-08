"""Tier 2: #3879 Stage 0 — the flat-tests ratchet (scripts/flat_tests_ratchet.py).

Placed in tests/scripts/ (not flat) deliberately: this file is itself NEW, so
it must obey the gate it tests — landing it flat would be exactly the
regression this gate exists to catch, on its own introducing PR.

Real filesystem + real subprocess/git throughout (a real tmp_path tree with
real files, a real git repo for the --check-growth tests) — no mocks; the
functions under test are themselves thin wrappers over the filesystem and
git, so faking either would test nothing real.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import scripts.flat_tests_ratchet as flat_tests_ratchet
from scripts.flat_tests_ratchet import (
    baseline_at_ref,
    load_baseline,
    main,
    measured_flat_files,
    new_flat_files,
    write_baseline,
)


def _make_tests_dir(
    tmp_path: Path, flat_names: "list[str]", nested: "list[str] | None" = None,
) -> Path:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for name in flat_names:
        (tests_dir / name).write_text("# test\n", encoding="utf-8")
    for rel in nested or []:
        p = tests_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# test\n", encoding="utf-8")
    return tests_dir


def test_measured_flat_files_sees_only_direct_children(tmp_path: Path) -> None:
    """Tier 2: a file in a subdirectory is never counted as flat — the whole
    point of the ratchet is to distinguish the two."""
    tests_dir = _make_tests_dir(
        tmp_path,
        flat_names=["test_a.py", "test_b.py"],
        nested=["sub/test_c.py"],
    )
    assert measured_flat_files(tests_dir) == {"test_a.py", "test_b.py"}


def test_measured_flat_files_ignores_non_python(tmp_path: Path) -> None:
    """Tier 2: a non-.py file dropped flat (README, fixture data) is not a
    ratchet concern — this gate is about NEW TEST FILES, not arbitrary files."""
    tests_dir = _make_tests_dir(tmp_path, flat_names=["test_a.py"])
    (tests_dir / "README.md").write_text("x", encoding="utf-8")
    assert measured_flat_files(tests_dir) == {"test_a.py"}


def test_new_flat_files_is_measured_minus_baseline() -> None:
    """Tier 2: the ratchet's core arithmetic — a name absent from the
    baseline is new; a baseline name absent from measured (moved away) is
    NOT reported by this function at all (the silent-shrink contract)."""
    measured = {"test_a.py", "test_b.py", "test_new.py"}
    baseline = {"test_a.py", "test_b.py", "test_gone_via_git_mv.py"}
    assert new_flat_files(measured, baseline) == {"test_new.py"}


def test_write_baseline_then_load_baseline_round_trips(tmp_path: Path) -> None:
    """Tier 2: the baseline file format round-trips through write/load —
    catches a JSON-shape mismatch between the writer and the reader."""
    path = tmp_path / "baseline.json"
    write_baseline({"test_b.py", "test_a.py"}, path)
    assert load_baseline(path) == {"test_a.py", "test_b.py"}
    # Sorted on disk — a real property of the written file, not incidental:
    # a stable diff is why write_baseline sorts rather than dumping set order.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == sorted(on_disk)


@pytest.fixture
def _real_git_repo(tmp_path: Path) -> Path:
    """A REAL git repository with one commit carrying a baseline file — the
    fixture :func:`baseline_at_ref` reads via a real ``git show`` subprocess
    call, not a stand-in for git."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "flat_tests_baseline.json").write_text(
        json.dumps(["test_a.py", "test_b.py"], indent=2) + "\n", encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=repo, check=True)
    return repo


def test_baseline_at_ref_reads_the_committed_content(_real_git_repo: Path) -> None:
    """Tier 2: reads the baseline as committed at a ref, via a real `git
    show` — not the working tree's current (possibly dirty) copy."""
    baseline_path = _real_git_repo / "scripts" / "flat_tests_baseline.json"
    # Dirty the working tree copy — the committed content must still be what
    # comes back, proving this reads git history, not the live file.
    baseline_path.write_text(json.dumps(["test_a.py"]), encoding="utf-8")

    committed = baseline_at_ref("HEAD", baseline_path, root=_real_git_repo)
    assert committed == {"test_a.py", "test_b.py"}, (
        "must read the COMMITTED content, not the dirtied working-tree copy"
    )


def test_baseline_at_ref_returns_none_when_the_ref_lacks_the_file(
    _real_git_repo: Path,
) -> None:
    """Tier 2: a ref that predates this gate's own introduction (no baseline
    file at all) is not growth — there is nothing to grow FROM, and the
    growth check must not crash or false-fire on that ref."""
    subprocess.run(
        ["git", "rm", "-q", "scripts/flat_tests_baseline.json"],
        cwd=_real_git_repo, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "remove baseline"],
        cwd=_real_git_repo, check=True,
    )
    result = baseline_at_ref(
        "HEAD~1", _real_git_repo / "scripts" / "flat_tests_baseline.json",
        root=_real_git_repo,
    )
    assert result == {"test_a.py", "test_b.py"}

    # And a ref before ANY commit existed simply has no such content to show.
    result_missing = baseline_at_ref(
        "HEAD", _real_git_repo / "scripts" / "nonexistent_baseline.json",
        root=_real_git_repo,
    )
    assert result_missing is None


# ── main()'s --check-growth branch — the gate's OWN reason to exist ─────────
# lead-coder's blocking review finding: 12 tests covered every HELPER, none
# covered whether `main(["--check-growth", ref])` actually rejects growth —
# stripping the growth-rejection branch entirely left every prior test green.
# These drive the real CLI entry point end-to-end against a real git repo,
# both directions (grow rejects / shrink allows), matching the two-sided
# requirement lead-coder's second blocking comment added.


@pytest.fixture
def _repo_with_tests_dir(_real_git_repo: Path) -> Path:
    """Extends :func:`_real_git_repo` with a real ``tests/`` directory whose
    flat files match the committed baseline exactly — so the growth-check
    tests below isolate `--check-growth`'s OWN pass/fail, uncontaminated by
    the separate "new file not in baseline" check `main()` also runs."""
    tests_dir = _real_git_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_a.py").write_text("# test\n", encoding="utf-8")
    (tests_dir / "test_b.py").write_text("# test\n", encoding="utf-8")
    return _real_git_repo


def test_main_check_growth_rejects_a_baseline_that_grew(
    monkeypatch: pytest.MonkeyPatch, _repo_with_tests_dir: Path,
) -> None:
    """Tier 2: the blocking gap itself — `main(["--check-growth", "HEAD"])`
    must exit nonzero when the WORKING-TREE baseline carries a name HEAD's
    committed baseline does not, independent of `measured`. Hand-editing the
    baseline to pre-authorize a name with no corresponding real file is
    exactly the loophole `--check-growth` exists to close.

    NON-VACUITY (falsification, verified locally): reverting the growth-check
    branch in `main()` — commenting out the `if args.check_growth: ...` block
    entirely — makes this test FAIL (main returns 0), confirming the test
    actually depends on that branch running, not merely on `new` being
    nonempty (`measured` here still equals the OLD baseline, so the
    new-file check alone stays green)."""
    baseline_path = _repo_with_tests_dir / "scripts" / "flat_tests_baseline.json"
    baseline_path.write_text(
        json.dumps(["test_a.py", "test_b.py", "test_hand_added.py"], indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(flat_tests_ratchet, "_BASELINE_PATH", baseline_path)
    monkeypatch.setattr(flat_tests_ratchet, "_TESTS_DIR", _repo_with_tests_dir / "tests")
    monkeypatch.setattr(flat_tests_ratchet, "_ROOT", _repo_with_tests_dir)
    monkeypatch.chdir(_repo_with_tests_dir)

    exit_code = main(["--check-growth", "HEAD"])

    assert exit_code != 0, (
        "main() did not reject a baseline that grew vs HEAD with no "
        "corresponding new file on disk"
    )


def test_main_check_growth_allows_a_baseline_that_shrank(
    monkeypatch: pytest.MonkeyPatch, _repo_with_tests_dir: Path,
) -> None:
    """Tier 2: the other side of the same requirement (lead-coder's second
    blocking comment) — a baseline that SHRANK (Stage 1's `git mv` removing
    a name once the file moved into a subdirectory) must NOT be rejected by
    `--check-growth`. Without this half, a fix that made the growth check
    fire on ANY change (not just growth) would still pass the test above."""
    baseline_path = _repo_with_tests_dir / "scripts" / "flat_tests_baseline.json"
    tests_dir = _repo_with_tests_dir / "tests"
    # Simulate Stage 1: test_b.py moved out of tests/ into a subdirectory,
    # and its name dropped from the baseline — both sides shrink together,
    # as a real `git mv` + ratchet re-run would produce.
    (tests_dir / "test_b.py").unlink()
    baseline_path.write_text(
        json.dumps(["test_a.py"], indent=2) + "\n", encoding="utf-8",
    )
    monkeypatch.setattr(flat_tests_ratchet, "_BASELINE_PATH", baseline_path)
    monkeypatch.setattr(flat_tests_ratchet, "_TESTS_DIR", tests_dir)
    monkeypatch.setattr(flat_tests_ratchet, "_ROOT", _repo_with_tests_dir)
    monkeypatch.chdir(_repo_with_tests_dir)

    exit_code = main(["--check-growth", "HEAD"])

    assert exit_code == 0, (
        "main() rejected a baseline that only SHRANK — a real Stage-1 git-mv "
        "migration would be blocked by its own ratchet"
    )
