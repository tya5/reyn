"""Tier 2: #3879 — the flat_tests_disposition.json schema check + unprocessed derivation.

Real filesystem fixtures throughout (real JSON files in `tmp_path`) — the
functions under test read real file content, so faking the filesystem
would test nothing real.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.flat_tests_disposition_check import (
    load_arc_population,
    unprocessed,
    validation_errors,
)
from tests._support.paths import REPO_ROOT


def _write_arc_population(path: Path, keys: "list[str]") -> None:
    path.write_text(json.dumps(keys), encoding="utf-8")


def _write_disposition(path: Path, entries: dict) -> None:
    path.write_text(json.dumps(entries), encoding="utf-8")


def test_unprocessed_is_arc_population_minus_disposition_keys(tmp_path: Path) -> None:
    """Tier 2: THE derivation — an arc-population entry with a disposition
    entry is accounted for; one without is unprocessed."""
    arc_population = tmp_path / "arc_population.json"
    disposition = tmp_path / "disposition.json"
    _write_arc_population(arc_population, ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"])
    _write_disposition(disposition, {
        "tests/test_a.py": {"disposition": "moved", "to": "tests/x/test_a.py"},
        "tests/test_b.py": {"disposition": "flat-by-decision", "reason": "single-file module, no bucket exists"},
    })
    result = unprocessed(arc_population, disposition)
    assert result == {"tests/test_c.py"}


def test_unprocessed_is_empty_set_when_fully_accounted(tmp_path: Path) -> None:
    """Tier 2: accept-side — every arc-population file has an entry ->
    empty set, the #3879 stopping condition."""
    arc_population = tmp_path / "arc_population.json"
    disposition = tmp_path / "disposition.json"
    _write_arc_population(arc_population, ["tests/test_a.py"])
    _write_disposition(disposition, {
        "tests/test_a.py": {"disposition": "deleted", "reason": "Q3: nobody would miss it, no consumer"},
    })
    assert unprocessed(arc_population, disposition) == set()


def test_missing_disposition_file_means_everything_unprocessed(tmp_path: Path) -> None:
    """Tier 2: a disposition.json that doesn't exist yet is the same as an
    empty one — every arc-population file is unprocessed, not an error."""
    arc_population = tmp_path / "arc_population.json"
    _write_arc_population(arc_population, ["tests/test_a.py", "tests/test_b.py"])
    missing = tmp_path / "does_not_exist.json"
    assert unprocessed(arc_population, missing) == {"tests/test_a.py", "tests/test_b.py"}


def test_a_move_without_a_disposition_entry_does_not_shrink_unprocessed(tmp_path: Path) -> None:
    """Tier 2: THE regression this snapshot exists to fix (lead-coder,
    #4068 follow-up) — a file moving OUT of the live, ratchet-shrinking
    baseline must NOT make it disappear from `unprocessed` if nobody wrote
    a disposition entry for it. `arc_population` is frozen at a
    point-in-time snapshot, so it does not react to a baseline shrinking
    the way the OLD (baseline-based) derivation did.

    Simulates the exact failure: an arc-population file "moves" (its
    disposition is never recorded) — `unprocessed` must still name it,
    proving the frozen snapshot doesn't shrink out from under the
    population the way the live baseline does."""
    arc_population = tmp_path / "arc_population.json"
    disposition = tmp_path / "disposition.json"
    _write_arc_population(arc_population, ["tests/test_a.py", "tests/test_b.py"])
    # test_a.py "moves" — nobody records a disposition entry for it, the
    # exact #4066 shape (a migration PR merges, the live baseline shrinks,
    # nothing gets transcribed).
    _write_disposition(disposition, {})
    result = unprocessed(arc_population, disposition)
    assert result == {"tests/test_a.py", "tests/test_b.py"}, (
        "a file moving without a disposition entry must stay unprocessed — "
        "the frozen snapshot must not shrink the way the live baseline does"
    )


def test_moved_without_to_is_a_schema_error() -> None:
    """Tier 2: disposition=moved requires a non-empty 'to' — THE case the
    schema check exists to catch."""
    entries = {"tests/test_a.py": {"disposition": "moved"}}
    errors = validation_errors(entries)
    assert any("requires a non-empty 'to'" in e for e in errors)


def test_flat_by_decision_without_reason_is_a_schema_error() -> None:
    """Tier 2: disposition=flat-by-decision / deleted require a non-empty
    'reason'."""
    entries = {"tests/test_a.py": {"disposition": "flat-by-decision", "reason": ""}}
    errors = validation_errors(entries)
    assert any("requires a non-empty 'reason'" in e for e in errors)


def test_a_classification_word_as_reason_is_rejected() -> None:
    """Tier 2: THE discipline lead-coder named explicitly — 'Tier 4' or 'no
    axis' as a reason is a classification, not a WHY, and must be rejected."""
    entries = {
        "tests/test_a.py": {"disposition": "deleted", "reason": "Tier 4"},
        "tests/test_b.py": {"disposition": "flat-by-decision", "reason": "no axis"},
    }
    errors = validation_errors(entries)
    assert any("test_a.py" in e and "names a CLASSIFICATION" in e for e in errors)
    assert any("test_b.py" in e and "names a CLASSIFICATION" in e for e in errors)


def test_a_real_one_sentence_reason_is_accepted() -> None:
    """Tier 2: accept-side — a genuine WHY (not a classification word)
    passes."""
    entries = {
        "tests/test_a.py": {
            "disposition": "deleted",
            "reason": "the mechanism it guards was removed by PR #2435 over a month ago",
        },
    }
    assert validation_errors(entries) == []


def test_an_invalid_disposition_value_is_a_schema_error() -> None:
    """Tier 2: non-vacuity — a typo'd or invented disposition value (not one
    of the 3 allowed) is caught, not silently accepted."""
    entries = {"tests/test_a.py": {"disposition": "archived"}}
    errors = validation_errors(entries)
    assert any("not one of" in e for e in errors)


def test_the_real_disposition_file_has_no_schema_errors() -> None:
    """Tier 2: the actual, committed scripts/flat_tests_disposition.json
    passes its own schema check — not assumed, verified against the real
    file."""
    from scripts.flat_tests_disposition_check import load_disposition

    real = load_disposition()
    assert validation_errors(real) == []


def test_the_frozen_snapshot_has_no_internal_duplicate_keys() -> None:
    """Tier 2: the real, committed flat_tests_arc_population.json is
    Stage 0's own flat_tests_baseline.json (commit accdfd226, 1,129 files)
    — a plain list, not derived by combining two sources, but still worth
    a non-vacuity check that nothing produced a duplicate key along the
    way (e.g. a `tests/{n}` normalization collision)."""
    keys = load_arc_population()
    raw = json.loads(
        (REPO_ROOT / "scripts" / "flat_tests_arc_population.json").read_text(encoding="utf-8")
    )
    assert len(raw) == len(keys), "duplicate keys in the frozen snapshot"


def test_the_real_disposition_keys_are_a_subset_of_the_frozen_population() -> None:
    """Tier 2: every currently-recorded disposition entry must trace back
    to the frozen population it was recorded against — a disposition key
    absent from `arc_population` would mean the artifact drifted from what
    it's supposed to be tracking."""
    from scripts.flat_tests_disposition_check import load_disposition

    population = load_arc_population()
    disposition = load_disposition()
    assert set(disposition.keys()) <= population


def test_the_frozen_snapshot_equals_stage_0s_own_committed_baseline() -> None:
    """Tier 2: the load-bearing witness for the #4072 fix — the committed
    `flat_tests_arc_population.json` must be EXACTLY the 1,129 filenames
    Stage 0 (commit accdfd226, #3883) committed to `flat_tests_baseline.
    json`, normalized the same way `load_baseline()` normalizes the live
    one. Not the union of two live artifacts (the first, wrong snapshot's
    own shape) — read directly from git history, not re-derived."""
    import subprocess

    raw = subprocess.run(
        ["git", "show", "accdfd226:scripts/flat_tests_baseline.json"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    stage0 = {f"tests/{n}" for n in json.loads(raw)}
    assert load_arc_population() == stage0
