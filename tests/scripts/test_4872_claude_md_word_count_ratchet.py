"""Tier 2: #4872 — the CLAUDE.md word-count ratchet
(scripts/claude_md_word_count_ratchet.py).

Placed in tests/scripts/ (not flat) deliberately: this file is itself NEW,
so it must obey the flat-tests gate on its own introducing PR — the same
reason test_flat_tests_ratchet_3879.py names for its own placement.

Real filesystem throughout (a real tmp_path tree with real CLAUDE.md files)
— no mocks; the functions under test are themselves thin wrappers over the
filesystem, so faking it would test nothing real.
"""
from __future__ import annotations

import json
from pathlib import Path

import scripts.claude_md_word_count_ratchet as ratchet
from scripts.claude_md_word_count_ratchet import (
    load_baseline,
    main,
    measured_word_counts,
    over_baseline,
    write_baseline,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_measured_word_counts_matches_wc_dash_w(tmp_path: Path) -> None:
    """Tier 2: word count is `str.split()`'s whitespace-run split — the
    module docstring's own claim that this matches `wc -w` byte-for-byte,
    verified here for a real file (not just asserted in prose)."""
    _write(tmp_path / "CLAUDE.md", "one two  three\nfour\tfive\n")
    assert measured_word_counts(tmp_path) == {"CLAUDE.md": 5}


def test_measured_word_counts_finds_nested_files(tmp_path: Path) -> None:
    """Tier 2: a CLAUDE.md under a subdirectory is measured too — the
    ratchet's whole point (#4872's own background: 6 nested CLAUDE.md files
    already exist, each its own load-bearing per-directory cost)."""
    _write(tmp_path / "CLAUDE.md", "root file")
    _write(tmp_path / "src" / "pkg" / "CLAUDE.md", "a b c")
    assert measured_word_counts(tmp_path) == {
        "CLAUDE.md": 2,
        "src/pkg/CLAUDE.md": 3,
    }


def test_measured_word_counts_excludes_git_and_venv_dirs(tmp_path: Path) -> None:
    """Tier 2: a CLAUDE.md-named file inside .git or .venv (never a real
    rules file a session would load) must not be counted — false growth
    from tooling internals would make this gate untrustworthy."""
    _write(tmp_path / "CLAUDE.md", "root")
    _write(tmp_path / ".git" / "CLAUDE.md", "not a real rules file")
    _write(tmp_path / ".venv" / "lib" / "CLAUDE.md", "also not real")
    assert measured_word_counts(tmp_path) == {"CLAUDE.md": 1}


def test_over_baseline_flags_growth_only() -> None:
    """Tier 2: the ratchet's core arithmetic — only a file whose CURRENT
    count exceeds its baseline is flagged; a match or a shrink is not."""
    measured = {"CLAUDE.md": 10, "sub/CLAUDE.md": 5}
    baseline = {"CLAUDE.md": 8, "sub/CLAUDE.md": 5}
    assert over_baseline(measured, baseline) == {"CLAUDE.md": (8, 10)}


def test_over_baseline_flags_a_shrink_as_not_over() -> None:
    """Tier 2: the other side — a file that got SHORTER than its baseline
    (a real, welcome cut) must not be reported. Without this, a fix that
    flagged any DIFFERENCE (not just growth) would still pass the test
    above."""
    measured = {"CLAUDE.md": 5}
    baseline = {"CLAUDE.md": 10}
    assert over_baseline(measured, baseline) == {}


def test_over_baseline_flags_a_brand_new_file_with_no_entry() -> None:
    """Tier 2: a CLAUDE.md with no baseline entry at all (a brand-new nested
    file) is treated as baseline 0 — ANY word count on it is growth, never
    a free pass. Mirrors flat_tests_ratchet.py's identical "new = must be
    declared" posture for a new flat test file."""
    measured = {"CLAUDE.md": 100, "new/CLAUDE.md": 12}
    baseline = {"CLAUDE.md": 100}
    assert over_baseline(measured, baseline) == {"new/CLAUDE.md": (0, 12)}


def test_over_baseline_a_removed_file_is_silently_absent() -> None:
    """Tier 2: a baseline entry whose file no longer exists at all (deleted
    or moved) is not reported — nothing to grow FROM once it's gone,
    mirroring flat_tests_ratchet.py's identical silent-shrink treatment."""
    measured = {"CLAUDE.md": 10}
    baseline = {"CLAUDE.md": 10, "gone/CLAUDE.md": 999}
    assert over_baseline(measured, baseline) == {}


def test_write_baseline_then_load_baseline_round_trips(tmp_path: Path) -> None:
    """Tier 2: the baseline file format round-trips through write/load —
    catches a JSON-shape mismatch between the writer and the reader."""
    path = tmp_path / "baseline.json"
    write_baseline({"b/CLAUDE.md": 3, "CLAUDE.md": 10}, path)
    assert load_baseline(path) == {"CLAUDE.md": 10, "b/CLAUDE.md": 3}
    # Sorted on disk — a stable diff is why write_baseline sorts by key
    # rather than dumping dict insertion order.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert list(on_disk.keys()) == sorted(on_disk.keys())


# ── main() end-to-end — the gate's own CLI entry point ──────────────────
# lead-coder's own #3879 review finding (test_flat_tests_ratchet_3879.py's
# comment): helper-level tests alone can leave main()'s actual wiring
# unverified. These drive `main()` directly against a real tmp_path tree.


def test_main_fails_when_a_file_exceeds_its_baseline(
    monkeypatch, tmp_path: Path,
) -> None:
    """Tier 2: the gate's own reason to exist — `main([])` must exit
    nonzero when a real file's word count exceeds its committed baseline.

    Strip-falsifier (verified locally, not committed): reverting
    `over_baseline`'s `count > was` check to `count != was` still catches
    this case (a shrink would also fail then) — the SEPARATE shrink test
    below is what actually isolates the `>` behavior; together they pin
    the exact comparison."""
    _write(tmp_path / "CLAUDE.md", "one two three four five")
    baseline_path = tmp_path / "baseline.json"
    write_baseline({"CLAUDE.md": 3}, baseline_path)
    monkeypatch.setattr(ratchet, "_ROOT", tmp_path)
    monkeypatch.setattr(ratchet, "_BASELINE_PATH", baseline_path)

    assert main([]) != 0


def test_main_passes_when_every_file_is_at_or_under_baseline(
    monkeypatch, tmp_path: Path,
) -> None:
    """Tier 2: the non-vacuity twin of the test above — a real tree that
    genuinely satisfies its baseline (including a real reduction) must
    exit 0. Without this, a `main()` that always returns nonzero would
    still pass the failing-case test."""
    _write(tmp_path / "CLAUDE.md", "one two three")
    baseline_path = tmp_path / "baseline.json"
    write_baseline({"CLAUDE.md": 10}, baseline_path)  # shrank since baseline
    monkeypatch.setattr(ratchet, "_ROOT", tmp_path)
    monkeypatch.setattr(ratchet, "_BASELINE_PATH", baseline_path)

    assert main([]) == 0


def test_main_write_baseline_locks_in_the_current_counts(
    monkeypatch, tmp_path: Path,
) -> None:
    """Tier 2: `main(["--write-baseline"])` regenerates the baseline file
    from the CURRENT tree — the real adoption/deliberate-raise path #4872's
    own dispatch requires ("commit the updated baseline in the SAME PR")."""
    _write(tmp_path / "CLAUDE.md", "one two three four")
    baseline_path = tmp_path / "baseline.json"
    write_baseline({"CLAUDE.md": 1}, baseline_path)
    monkeypatch.setattr(ratchet, "_ROOT", tmp_path)
    monkeypatch.setattr(ratchet, "_BASELINE_PATH", baseline_path)

    assert main([]) != 0, "sanity: the pre-write-baseline tree must be red first"

    exit_code = main(["--write-baseline"])

    assert exit_code == 0
    assert load_baseline(baseline_path) == {"CLAUDE.md": 4}
    assert main([]) == 0, "after --write-baseline, the same tree must be green"
