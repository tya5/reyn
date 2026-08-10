"""Tier 1: scripts/check_tests_path_literal_reference.py's regex/ratchet contract.

Same skeleton as `tests/scripts/test_3726_mypy_ratchet.py`'s ratchet tests —
a committed baseline set only ever shrinks; a measured entry not in it is
new and must be surfaced, an entry that silently disappears from the
measured set (a fix, or the referencing file moving/being deleted) is not
itself reported. Here the measured set is `(referencing_file, tests/-path-
literal)` pairs rather than mypy `(file, error-code)` pairs, and the
population is `git ls-files` rather than a mypy run, but the ratchet logic
(`new_pairs`) is the same shape: `measured - baseline`, nothing more.

Real filesystem fixtures for the regex/resolution tests (a real `tmp_path`
tree) — no mocks, the whole point is these are pure functions over real
text/files.
"""
from __future__ import annotations

import json
import subprocess

from scripts.check_tests_path_literal_reference import (
    _BASELINE_PATH,
    _PATH_LITERAL_RE,
    _ROOT,
    classify,
    load_baseline,
    new_pairs,
    offending_references,
)

# ── the regex itself — #4006's own lesson (mid-sentence, not quote-anchored) ─


def test_a_mid_sentence_reference_is_matched() -> None:
    """Tier 1: #4006 measured 17x undercounting from anchoring to "right
    after a quote character" — the regex must match `tests/...py` embedded
    anywhere in a larger prose run, not only a standalone string literal."""
    line = 'See the gate in tests/scripts/test_foo.py for the -O witness.'
    matches = [m.group(0) for m in _PATH_LITERAL_RE.finditer(line)]
    assert matches == ["tests/scripts/test_foo.py"]


def test_a_bare_substring_without_a_word_boundary_is_not_matched() -> None:
    """Tier 1: "xtests/foo.py" must not match — the left word-boundary
    exists specifically to reject a `tests/` occurring mid-identifier."""
    line = "xtests/foo.py"
    assert list(_PATH_LITERAL_RE.finditer(line)) == []


# ── offending_references — resolution against a real tmp tree ──────────────


def test_a_literal_resolving_to_a_real_file_is_not_offending(tmp_path) -> None:
    """Tier 1: `tests/services/test_x.py` existing on disk is not an
    offender, even though the scan matched it."""
    (tmp_path / "tests" / "services").mkdir(parents=True)
    (tmp_path / "tests" / "services" / "test_x.py").write_text("", encoding="utf-8")
    (tmp_path / "note.md").write_text(
        "see tests/services/test_x.py for details\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    offenders = offending_references(tmp_path)
    assert offenders == []


def test_a_literal_not_resolving_is_offending(tmp_path) -> None:
    """Tier 1: `tests/services/test_gone.py` referenced but absent on disk
    IS an offender — the gate's whole reason to exist."""
    (tmp_path / "note.md").write_text(
        "see tests/services/test_gone.py for details\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    offenders = offending_references(tmp_path)
    assert offenders == [(tmp_path / "note.md", "tests/services/test_gone.py", 1)]


def test_an_untracked_file_is_not_scanned(tmp_path) -> None:
    """Tier 1: the population is `git ls-files` (tracked content), not a
    directory walk — an untracked file (e.g. a gitignored build artifact
    like mkdocs' `site/`) must not contribute offenders, with zero
    exclusion rule naming it."""
    (tmp_path / "note.md").write_text(
        "see tests/services/test_gone.py for details\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    # note.md is never `git add`-ed — untracked.
    offenders = offending_references(tmp_path)
    assert offenders == []


def test_changelog_md_is_excluded_even_though_tracked(tmp_path) -> None:
    """Tier 1: CHANGELOG.md IS tracked (so `git ls-files` alone would
    include it) but is explicitly excluded — a historical record naming a
    path that was real when written is not a defect."""
    (tmp_path / "CHANGELOG.md").write_text(
        "- fixed tests/services/test_long_gone.py\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    offenders = offending_references(tmp_path)
    assert offenders == []


def test_flat_tests_disposition_json_is_excluded_even_though_tracked(tmp_path) -> None:
    """Tier 1: scripts/flat_tests_disposition.json (#3879 S2) is the same
    shape as CHANGELOG.md, not the gate's own baseline shape — its `moved`
    entries are keyed on the file's OLD flat path BY DESIGN, so the old
    path correctly never resolving is the record, not a defect. Without
    this exclusion, every file #3879 records as moved would cost one
    baseline entry here forever (lead-coder review, #4065 follow-up)."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "flat_tests_disposition.json").write_text(
        '{"tests/test_moved_away.py": {"disposition": "moved", "to": "tests/core/test_moved_away.py"}}\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    offenders = offending_references(tmp_path)
    assert offenders == []


# ── classify — the never-existed vs. stale discriminator ───────────────────


def test_a_literal_never_tracked_classifies_never_existed() -> None:
    """Tier 1: a literal absent from the full-history tracked-file set is
    `never-existed` — an illustrative example or an unbuilt promise, never
    a real file that moved."""
    assert classify("tests/test_a.py", ever_tracked=set()) == "never-existed"


def test_a_literal_once_tracked_classifies_stale() -> None:
    """Tier 1: a literal present in the full-history tracked-file set is
    `stale` — it WAS a real file and the reference wasn't updated when it
    moved or was deleted."""
    ever = {"tests/test_moved_away.py"}
    assert classify("tests/test_moved_away.py", ever_tracked=ever) == "stale"


# ── new_pairs — the ratchet check itself ────────────────────────────────────


def test_a_pair_in_the_baseline_is_not_new() -> None:
    """Tier 1: grandfathered debt does not fail the gate."""
    baseline = {("docs/foo.md", "tests/test_gone.py")}
    measured = {("docs/foo.md", "tests/test_gone.py")}
    assert new_pairs(measured, baseline) == set()


def test_a_pair_absent_from_the_baseline_is_new() -> None:
    """Tier 1: the load-bearing case — a reference that goes stale AFTER
    the baseline was written must be caught."""
    baseline = {("docs/foo.md", "tests/test_gone.py")}
    measured = {("docs/foo.md", "tests/test_gone.py"), ("docs/bar.md", "tests/test_new_gone.py")}
    assert new_pairs(measured, baseline) == {("docs/bar.md", "tests/test_new_gone.py")}


def test_a_pair_leaving_the_measured_set_is_not_reported() -> None:
    """Tier 1: a fix (or the referencing file itself moving away) silently
    drops out — nothing has to be edited in the baseline to let a fix
    "count", the same discipline `mypy_ratchet.py`'s own docstring names."""
    baseline = {("docs/foo.md", "tests/test_gone.py"), ("docs/bar.md", "tests/test_also_gone.py")}
    measured = {("docs/foo.md", "tests/test_gone.py")}
    assert new_pairs(measured, baseline) == set()


# ── the real committed baseline ─────────────────────────────────────────────


def test_the_real_baseline_has_no_new_pairs_against_the_current_tree() -> None:
    """Tier 1: the load-bearing witness — running the real scan against the
    real repo tree, right now, must find nothing beyond what's baselined.
    This is the gate itself, run as a test."""
    baseline = load_baseline()
    measured = {
        (str(path.relative_to(_ROOT)), literal)
        for path, literal, _lineno in offending_references(_ROOT)
    }
    assert new_pairs(measured, baseline) == set()


def test_every_baseline_entry_has_a_valid_class() -> None:
    """Tier 1: schema check — every entry declares `class` as one of the
    two values `classify()` can return, not silently omitted or free text."""
    data = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    assert data, "baseline must not be empty — a real, measured population"
    for entry in data:
        assert entry["class"] in ("stale", "never-existed"), entry
