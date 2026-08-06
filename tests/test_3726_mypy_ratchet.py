"""Tier 1: scripts/mypy_ratchet.py's parse/diff contract.

Same skeleton as `tests/test_3595_s4_slash_handler_seam.py`'s `_SESSION_RESIDUE`
ratchet — a committed baseline set only ever shrinks; a measured entry not in
it is new and must be surfaced, an entry that silently disappears from the
measured set (a fix) is not itself reported. Here the measured set is
`(file, mypy-error-code)` pairs rather than private-member accesses, and the
"walk" is running mypy itself rather than an AST pass, but the ratchet logic
(`new_findings`) is the same shape: `measured - baseline`, nothing more.

Public surface only: `parse_mypy_output` / `new_findings` / `load_baseline` /
`write_baseline` are called directly against real strings/files (no mocks —
there is nothing to fake here, the whole point is these are pure functions
over text/sets).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mypy_ratchet.py"


def _load():
    spec = importlib.util.spec_from_file_location("_mypy_ratchet_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── parse_mypy_output ───────────────────────────────────────────────────────


def test_parses_error_lines_into_file_code_pairs() -> None:
    """Tier 1: an ordinary mypy error line becomes one (file, code) pair."""
    module = _load()
    text = (
        'src/reyn/foo.py:12: error: Name "X" is not defined  [name-defined]\n'
        'src/reyn/bar.py:34: error: Argument 1 has incompatible type  [arg-type]\n'
    )
    assert module.parse_mypy_output(text) == {
        ("src/reyn/foo.py", "name-defined"),
        ("src/reyn/bar.py", "arg-type"),
    }


def test_repeated_lines_in_the_same_file_and_code_collapse_to_one_pair() -> None:
    """Tier 1: the ratchet is keyed on (file, code), not exact line/occurrence
    count — a line shifting from an unrelated edit above it must never flip
    the gate by itself."""
    module = _load()
    text = (
        'src/reyn/foo.py:12: error: msg one  [attr-defined]\n'
        'src/reyn/foo.py:99: error: msg two  [attr-defined]\n'
    )
    assert module.parse_mypy_output(text) == {("src/reyn/foo.py", "attr-defined")}


def test_note_lines_and_the_summary_line_are_not_parsed_as_findings() -> None:
    """Tier 1: FALSIFY — a `note:` line or the trailing `Found N errors...`
    summary carry no `[code]` a future run can diff against; parsing them
    would corrupt the baseline with entries that can never be matched again."""
    module = _load()
    text = (
        'src/reyn/foo.py:12: error: msg  [assignment]\n'
        'src/reyn/foo.py:12: note: Error code "arg-type" not covered by "type: ignore[assignment]" comment\n'
        "Found 1 error in 1 file (checked 5 source files)\n"
    )
    assert module.parse_mypy_output(text) == {("src/reyn/foo.py", "assignment")}


# ── new_findings (the ratchet itself) ───────────────────────────────────────


def test_a_measured_pair_absent_from_baseline_is_new() -> None:
    """Tier 1: FALSIFY — a pair the baseline never declared must surface as new."""
    module = _load()
    measured = {("src/reyn/foo.py", "attr-defined"), ("src/reyn/bar.py", "arg-type")}
    baseline = {("src/reyn/foo.py", "attr-defined")}
    assert module.new_findings(measured, baseline) == {("src/reyn/bar.py", "arg-type")}


def test_a_baselined_pair_that_disappears_from_measured_is_not_reported() -> None:
    """Tier 1: a fix (a baselined pair no longer measured) never appears in
    `new_findings` — nothing has to be edited to let a fix 'count', per the
    module's own design (mirrors #3595's ratchet: only growth is gated)."""
    module = _load()
    measured: set[tuple[str, str]] = set()
    baseline = {("src/reyn/foo.py", "attr-defined")}
    assert module.new_findings(measured, baseline) == set()


def test_measured_set_equal_to_baseline_is_clean() -> None:
    """Tier 1: nothing new against an identical measured/baseline set."""
    module = _load()
    pairs = {("src/reyn/foo.py", "attr-defined"), ("src/reyn/bar.py", "arg-type")}
    assert module.new_findings(pairs, pairs) == set()


# ── load_baseline / write_baseline round-trip ───────────────────────────────


def test_write_then_load_baseline_round_trips(tmp_path: Path) -> None:
    """Tier 1: what write_baseline puts on disk, load_baseline reads back
    unchanged — the maintenance path (`--write-baseline`) round-trips."""
    module = _load()
    path = tmp_path / "baseline.json"
    pairs = {("src/reyn/foo.py", "attr-defined"), ("src/reyn/bar.py", "arg-type")}

    module.write_baseline(pairs, path)
    loaded = module.load_baseline(path)

    assert loaded == pairs


def test_written_baseline_is_sorted_for_stable_diffs(tmp_path: Path) -> None:
    """Tier 1: sorted output keeps the committed file's diffs reviewable —
    an unsorted dump would make every regeneration a full-file reorder."""
    module = _load()
    path = tmp_path / "baseline.json"
    pairs = {("src/reyn/zzz.py", "attr-defined"), ("src/reyn/aaa.py", "arg-type")}

    module.write_baseline(pairs, path)
    data = json.loads(path.read_text(encoding="utf-8"))

    files = [entry["file"] for entry in data]
    assert files == sorted(files)


# ── the real repo, end to end ────────────────────────────────────────────────


def test_the_committed_baseline_is_reachable_through_the_registered_target() -> None:
    """Tier 1: `load_baseline()`'s own default (no path argument, the same
    default `main()` uses) resolves to the real committed baseline — a path
    typo here would silently no-op the CI job (0 findings, 0 baseline,
    trivially "clean") rather than raising."""
    module = _load()
    baseline = module.load_baseline()
    assert len(baseline) > 0
