"""Tier 2: #3879 — the flat_tests_disposition.json schema check + unprocessed derivation.

Real filesystem fixtures throughout (real JSON files in `tmp_path`) — the
functions under test read real file content, so faking the filesystem
would test nothing real.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.flat_tests_disposition_check import (
    unprocessed,
    validation_errors,
)


def _write_baseline(path: Path, names: "list[str]") -> None:
    path.write_text(json.dumps(names), encoding="utf-8")


def _write_disposition(path: Path, entries: dict) -> None:
    path.write_text(json.dumps(entries), encoding="utf-8")


def test_unprocessed_is_baseline_minus_disposition_keys(tmp_path: Path) -> None:
    """Tier 2: THE derivation — a baseline file with a disposition entry is
    accounted for; one without is unprocessed."""
    baseline = tmp_path / "baseline.json"
    disposition = tmp_path / "disposition.json"
    _write_baseline(baseline, ["test_a.py", "test_b.py", "test_c.py"])
    _write_disposition(disposition, {
        "tests/test_a.py": {"disposition": "moved", "to": "tests/x/test_a.py"},
        "tests/test_b.py": {"disposition": "flat-by-decision", "reason": "single-file module, no bucket exists"},
    })
    result = unprocessed(baseline, disposition)
    assert result == {"tests/test_c.py"}


def test_unprocessed_is_empty_set_when_fully_accounted(tmp_path: Path) -> None:
    """Tier 2: accept-side — every baseline file has an entry -> empty set,
    the #3879 stopping condition."""
    baseline = tmp_path / "baseline.json"
    disposition = tmp_path / "disposition.json"
    _write_baseline(baseline, ["test_a.py"])
    _write_disposition(disposition, {
        "tests/test_a.py": {"disposition": "deleted", "reason": "Q3: nobody would miss it, no consumer"},
    })
    assert unprocessed(baseline, disposition) == set()


def test_missing_disposition_file_means_everything_unprocessed(tmp_path: Path) -> None:
    """Tier 2: a disposition.json that doesn't exist yet is the same as an
    empty one — every baseline file is unprocessed, not an error."""
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, ["test_a.py", "test_b.py"])
    missing = tmp_path / "does_not_exist.json"
    assert unprocessed(baseline, missing) == {"tests/test_a.py", "tests/test_b.py"}


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
