"""Tier 1: `check_silent_except_return_reachability.py`'s own detector logic
(#5990 stage 2-2).

Real fixture files (`tests/scripts/fixtures/silent_except_return_5990/`),
never a strip-and-revert scratch — same discipline
`test_silent_except_ratchet_5990.py` already established for the sibling
gate, per lead-coder's own review of THAT gate's design.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_silent_except_return_reachability import (
    _VOCABULARY,
    _site_marker,
    find_violation_sites,
    measured,
)
from tests._support.paths import REPO_ROOT

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "silent_except_return_5990"


def test_a_return_reaching_assign_only_handler_is_a_violation_site():
    """Tier 1: the false-accept-side witness — an assign-only handler
    whose bound name reaches this function's own `return` is found,
    regardless of whether it carries a marker."""
    sites = find_violation_sites(_FIXTURES / "violation_unmarked.py")
    assert any(s.lineno == 10 for s in sites), (
        "the fixture's own except Exception: handler (line 10) was not found"
    )


def test_module_level_handler_is_correct_side():
    """Tier 1: the false-reject-side witness — a module-level handler (no
    enclosing function) has nothing to `return` to, so it is never
    flagged even though its body is assign-only."""
    sites = find_violation_sites(_FIXTURES / "correct_side.py")
    lines = {s.lineno for s in sites}
    assert 7 not in lines, "the module-level handler must not be flagged"


def test_non_assign_only_body_is_correct_side():
    """Tier 1: a handler body containing anything besides a plain
    `Assign` (here, a `raise` after the assign) is outside this axis's
    population entirely."""
    sites = find_violation_sites(_FIXTURES / "correct_side.py")
    lines = {s.lineno for s in sites}
    assert 20 not in lines, "a non-assign-only body must not be flagged"


def test_a_bound_name_that_never_reaches_return_is_correct_side():
    """Tier 1: assign-only, but the function's own `return` never
    references the bound name — correct-side."""
    sites = find_violation_sites(_FIXTURES / "correct_side.py")
    lines = {s.lineno for s in sites}
    assert 32 not in lines, "a bound name that never reaches return must not be flagged"


def test_a_name_shadowed_inside_a_nested_function_is_correct_side():
    """Tier 1: the outer function's own `return` never references the
    handler's binding — a same-spelled local inside a NESTED function
    must not count (a different scope entirely)."""
    sites = find_violation_sites(_FIXTURES / "correct_side.py")
    lines = {s.lineno for s in sites}
    assert 46 not in lines, "a name shadowed inside a nested function must not be flagged"


def test_correct_side_fixture_has_exactly_zero_violation_sites():
    """Tier 1: integration — every one of `correct_side.py`'s 4 functions
    is a deliberate negative control; none should ever be flagged."""
    assert find_violation_sites(_FIXTURES / "correct_side.py") == []


def _resolve(fixture_name: str) -> "tuple[str, 'Exception | None'] | None":
    """One fixture file's own single violation site, run through
    `_site_marker` directly — `measured()`'s own population scan is
    scoped to `src/reyn` (`git ls-files -- src/reyn/*.py`), so it never
    sees these `tests/` fixtures; exercising `_site_marker` directly is
    the correct level for a fixture-driven unit test, not `measured`
    itself (that function's own real-tree behaviour is covered
    separately below, against `src/reyn` itself)."""
    path = _FIXTURES / fixture_name
    sites = find_violation_sites(path)
    assert len(sites) == 1, f"{fixture_name} fixture drifted — expected exactly 1 violation site"
    lines = path.read_text(encoding="utf-8").splitlines()
    return _site_marker(sites[0], lines)


def test_a_valid_marker_excludes_the_site_from_measured_output():
    """Tier 2: the accept criterion's own ⑵ direction — a violation-side
    site carrying a CORRECTLY-formed, vocabulary-matching marker resolves
    to `(category, reason)`, not a problem. Without this test, a detector
    that flags every except unconditionally (which would ALSO pass the
    false-accept test above) could still pass the suite."""
    outcome = _resolve("violation_marked_ok.py")
    assert isinstance(outcome, tuple), (
        "a correctly-marked site must resolve to (category, reason), not a problem"
    )


def test_an_unmarked_site_appears_in_measured_output():
    """Tier 2: the accept criterion's own ⑴ direction — a violation-side
    site with NO marker at all resolves to `None` (distinct from a
    malformed-marker problem, which resolves to an `Exception`)."""
    outcome = _resolve("violation_unmarked.py")
    assert outcome is None, "an unmarked site must resolve to None, not a marker-format error"


def test_a_marker_with_an_invalid_category_appears_in_measured_output():
    """Tier 2: the SECOND gate direction lead-coder's ruling names
    explicitly — a marker IS present but names a category outside the
    fixed vocabulary. This must ALSO be flagged, as a DIFFERENT outcome
    (an `Exception`, not `None`) than the unmarked case above — a marker
    that always passes regardless of its own content would be a
    signature line, not a gate."""
    outcome = _resolve("violation_marked_invalid.py")
    assert isinstance(outcome, Exception), "a malformed/mis-categorized marker must still be flagged"
    assert "made-up-reason" in str(outcome)


def test_site_marker_reads_category_and_reason_from_a_valid_marker():
    """Tier 1: `_site_marker`'s own direct contract — a valid marker
    yields `(category, reason)`, both non-empty."""
    site = find_violation_sites(_FIXTURES / "violation_marked_ok.py")[0]
    lines = (_FIXTURES / "violation_marked_ok.py").read_text(encoding="utf-8").splitlines()
    result = _site_marker(site, lines)
    assert isinstance(result, tuple)
    category, reason = result
    assert category == "internal-hash-fallback"
    assert reason.strip()


def test_vocabulary_has_exactly_the_five_categories_6057_used():
    """Tier 2: pins the vocabulary itself — lead-coder's explicit
    instruction was to reuse #6057's own PR-body categories verbatim, not
    invent a new set. A future edit that silently adds/removes a category
    should have to touch THIS test, not slip through unnoticed."""
    assert _VOCABULARY == frozenset({
        "internal-hash-fallback",
        "expected-import-error",
        "external-content-caller-cannot-act",
        "reported-elsewhere",
        "low-stakes-display-value",
    })


def test_a_file_that_fails_to_parse_raises_rather_than_counting_zero(tmp_path: Path) -> None:
    """Tier 2: same fail-CLOSED discipline `silent_except_ratchet.py`
    already established (#5990) — a file this script cannot parse must
    propagate the parse error, not silently contribute 0 to the
    population.

    NON-VACUITY (strip-falsified by hand, in-file Edit -> run -> Edit
    back): wrapping `ast.parse` in a `try/except: return []` shape makes
    this assertion fail — it would return an empty list instead of
    raising."""
    import pytest

    broken = tmp_path / "broken.py"
    broken.write_text("def f(:\n    pass\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        find_violation_sites(broken)


def test_the_real_scan_against_the_current_tree_is_fully_resolved() -> None:
    """Tier 1: the load-bearing witness — running the real detector
    against the real repo tree, right now, finds nothing unresolved.
    Mirrors `silent_except_ratchet.py`'s own identically-shaped
    real-tree test."""
    unresolved, scanned = measured(REPO_ROOT)
    assert scanned > 0, "the scan found 0 files — a scanner failure, not a clean population"
    assert unresolved == [], (
        f"{len(unresolved)} violation-side site(s) under src/reyn/ have no "
        f"valid marker: {unresolved}"
    )
