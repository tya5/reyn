"""Tier 1: `silent_except_ratchet.py`'s own detector logic (#5990 follow-up gate).

lead-coder's own correction on the design review: "marker detection removed
entirely -> mass red" only proves the detector reacts to losing its own
logic, never that it correctly discriminates a genuinely silent except from
a genuinely reported one. The real witness needs BOTH directions on real
fixture files, permanently checked in (not a strip-and-revert scratch) —
`tests/scripts/fixtures/silent_except_5990/truly_silent.py` and
`has_marker.py` ARE that witness.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.silent_except_ratchet import (
    grown_files,
    load_baseline,
    silent_except_count,
    write_baseline,
)
from tests._support.paths import REPO_ROOT

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "silent_except_5990"


def test_a_truly_silent_except_is_counted():
    """Tier 1: the false-accept-side witness — a genuinely silent except
    (no log above debug, no re-raise, no marker) is counted. This
    fixture's two functions each carry exactly one such block."""
    assert silent_except_count(_FIXTURES / "truly_silent.py") == 2


def test_an_except_with_a_visible_marker_is_not_counted():
    """Tier 1: the false-reject-side witness, required alongside the test
    above so a detector that flags every except unconditionally (which
    would ALSO pass the truly-silent test) cannot pass this suite. This
    fixture's two functions each carry a visible marker (a `logger.error`
    call, a bare re-raise) and must NOT be counted."""
    assert silent_except_count(_FIXTURES / "has_marker.py") == 0


def test_debug_only_does_not_count_as_visible():
    """Tier 2: #5990's own headline finding — `logger.debug` is the ONE
    log level this detector deliberately does not treat as reported (a
    debug-only site cost 61 commits of blind bisection in the real
    incident this issue exists to close)."""
    fixture = _FIXTURES / "truly_silent.py"
    text = fixture.read_text(encoding="utf-8")
    assert 'logger.debug("swallowed' in text, (
        "fixture drifted — this test assumes truly_silent.py's SECOND "
        "function is a debug-only except, not just a bare `pass`"
    )
    # Both of truly_silent.py's two except blocks (the bare `pass` one AND
    # the logger.debug-only one) are counted — confirmed by the count-of-2
    # assertion in test_a_truly_silent_except_is_counted above; this test
    # exists only to pin WHY the fixture has two, not one.


def test_grown_files_flags_a_new_silent_site():
    """Tier 2: the ratchet arithmetic itself (shared shape with
    suspected_time_dependence_ratchet.py's own grown_files) — a file
    whose measured count exceeds its baseline is new debt."""
    assert grown_files({"a.py": 2}, {"a.py": 1}) == {"a.py": (1, 2)}


def test_grown_files_silently_allows_a_shrink():
    """Tier 2: a file whose count dropped is never reported — the
    silent-shrink contract every ratchet in this repo shares."""
    assert grown_files({"a.py": 1}, {"a.py": 3}) == {}


def test_write_baseline_then_load_baseline_round_trips(tmp_path: Path) -> None:
    """Tier 2: the baseline format round-trips through write/load."""
    path = tmp_path / "baseline.json"
    write_baseline({"b.py": 2, "a.py": 1}, path)
    assert load_baseline(path) == {"a.py": 1, "b.py": 2}


def test_a_file_that_fails_to_parse_raises_rather_than_counting_zero(tmp_path: Path) -> None:
    """Tier 2: #5990's own fail-closed divergence from #4846's silent-zero
    behaviour — a file this script cannot parse must propagate the parse
    error, not silently contribute 0 to the count.

    NON-VACUITY (strip-falsified locally, in-file Edit -> run -> Edit
    back): reverting `silent_except_count` to a `try/except: return 0`
    shape (the #4846 pattern) makes this assertion fail — it would return
    0 instead of raising."""
    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        silent_except_count(broken)


def test_the_real_scan_against_the_current_tree_matches_the_baseline() -> None:
    """Tier 1: the load-bearing witness — running the real scan against
    the real repo tree, right now, must find nothing beyond what's
    baselined. Mirrors every other ratchet's own identically-shaped test
    (e.g. check_subprocess_reyn_pin_5028.py, suspected_time_dependence's
    own baseline test)."""
    from scripts.silent_except_ratchet import _BASELINE_PATH, measured

    baseline = load_baseline(_BASELINE_PATH)
    counts, scanned = measured(REPO_ROOT)
    assert scanned > 0, "the scan found 0 files — a scanner failure, not a clean population"
    assert grown_files(counts, baseline) == {}


def test_a_new_uncommitted_silent_site_would_be_caught() -> None:
    """Tier 2: integration — planting a fresh copy of the truly-silent
    fixture as a new "file" beyond the baseline is measured as grown
    debt. Confirms the ratchet arithmetic is wired to the real detector,
    not just unit-tested in isolation above."""
    measured_now = {"new_module.py": silent_except_count(_FIXTURES / "truly_silent.py")}
    assert grown_files(measured_now, {}) == {"new_module.py": (0, 2)}
