"""Tier 1/2: `silent_warnings_category_gate.py`'s own detector logic
(#6143).

lead-coder's own review point (same lesson #5990's ratchet test drew): a
strip-falsify that only reverts the DETECTOR's category-matching logic
never proves the underlying claim -- "a `warnings.warn(...,
DeprecationWarning)` fired from `src/reyn/**` reaches nobody" -- is
actually true in a real process.
`test_a_silent_category_site_is_genuinely_silent_under_the_stock_default_filter`
below is that witness: it runs real subprocesses with Python's OWN
default filters (no `-W` flag, no `simplefilter("always")`, no
pytest-injected filter) -- a test that adds `simplefilter("always")`
itself measures its own filter, not production's.
"""
# EXEMPT: the sys.executable spawns below run entry.py/entry_direct_warn.py,
# throwaway fixture modules under production_filter_witness/ that only call
# warnings.warn -- none of them imports reyn.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts.silent_warnings_category_gate import (
    _EXCEPTION_TABLE,
    measured,
    silent_category_sites,
    unexplained,
)
from tests._support.paths import REPO_ROOT

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "silent_warnings_category_6143"
_PROD_FILTER_FIXTURES = _FIXTURES / "production_filter_witness"


# ── the detector's own category-matching logic ──────────────────────────


def test_every_silent_by_default_category_is_counted():
    """Tier 1: the false-accept-side witness -- one call per silent-by-
    default category (DeprecationWarning / PendingDeprecationWarning /
    ImportWarning / ResourceWarning), positional form, all counted.
    `silent_categories.py` carries exactly 5 such calls (4 positional + 1
    keyword, see the next test) -- this test only checks the positional
    4 by name so a category this detector fails to recognise cannot hide
    behind an aggregate count."""
    sites = silent_category_sites(_FIXTURES / "silent_categories.py", root=_FIXTURES)
    categories = sorted(category for _, _, category in sites)
    assert categories == [
        "DeprecationWarning",
        "DeprecationWarning",
        "ImportWarning",
        "PendingDeprecationWarning",
        "ResourceWarning",
    ], categories


def test_keyword_category_form_is_recognised():
    """Tier 1: `category=DeprecationWarning` (keyword) must be recognised
    the same as positional arg 2 -- `silent_categories.py`'s
    `keyword_deprecation` function is the one keyword-form call among its
    5 total sites."""
    sites = silent_category_sites(_FIXTURES / "silent_categories.py", root=_FIXTURES)
    lines = {lineno for _, lineno, _ in sites}
    text = (_FIXTURES / "silent_categories.py").read_text(encoding="utf-8")
    keyword_call_line = next(
        i + 1 for i, line in enumerate(text.splitlines()) if "category=DeprecationWarning" in line
    )
    assert keyword_call_line in lines, (
        "the keyword-form warnings.warn(..., category=DeprecationWarning) "
        "call was not detected -- category resolution only handled the "
        "positional form"
    )


def test_a_user_warning_or_omitted_category_is_never_counted():
    """Tier 1: the false-reject-side witness, required alongside the test
    above so a detector that flags every `warnings.warn` call
    unconditionally (which would ALSO pass the silent-categories test)
    cannot pass this suite. `loud_categories.py`'s two `warnings.warn`
    calls (explicit UserWarning, omitted category) are both visible under
    the stock default filter and must not be flagged; its
    `logger.warning` call is not even a `warnings.warn` call."""
    sites = silent_category_sites(_FIXTURES / "loud_categories.py", root=_FIXTURES)
    assert sites == []


# ── the exception-table arithmetic ───────────────────────────────────────


def test_a_site_with_no_table_entry_is_unexplained():
    """Tier 2: the gate's own pass/fail arithmetic -- a measured site
    absent from `_EXCEPTION_TABLE` is what makes the gate red."""
    sites = [("some/file.py", 10, "DeprecationWarning")]
    assert unexplained(sites) == sites


def test_a_site_matching_a_table_entry_is_explained():
    """Tier 2: deny side -- a site whose (relpath, lineno) IS in the
    table is not reported, regardless of the category string (the table
    key is positional identity, not category)."""
    sites = [("some/file.py", 10, "DeprecationWarning")]
    assert unexplained(sites, table={("some/file.py", 10): "reviewed, dev-only"}) == []


def test_the_shipped_exception_table_is_empty():
    """Tier 2: #6143's own claim -- every known silent-by-default site
    was promoted to `logger.warning` in the SAME PR that added this
    gate, so nothing needs grandfathering. A future PR that adds a table
    entry does so deliberately; this test only pins today's starting
    point so a silent, undiscussed table addition would show as a diff
    here."""
    assert _EXCEPTION_TABLE == {}


def test_the_real_scan_against_the_current_tree_has_nothing_unexplained() -> None:
    """Tier 1: the load-bearing witness -- running the real scan against
    the real repo tree, right now, must find nothing outside the
    exception table. Mirrors `silent_except_ratchet.py`'s own identically
    -shaped tree-scan test."""
    sites = measured(REPO_ROOT)
    assert unexplained(sites) == [], (
        "a warnings.warn(...) call under src/reyn/ uses a silent-by-"
        "default category with no _EXCEPTION_TABLE entry -- promote it "
        "to logger.warning or add a reasoned table entry"
    )


def test_a_new_uncommitted_silent_site_would_be_caught() -> None:
    """Tier 2: integration -- planting the silent-categories fixture as a
    fresh "file" beyond the (empty) table is measured as unexplained.
    Confirms the table-arithmetic is wired to the real detector, not
    just unit-tested in isolation above."""
    sites = silent_category_sites(_FIXTURES / "silent_categories.py", root=_FIXTURES)
    assert len(unexplained(sites)) == 5


# ── production-filter witness: is the underlying claim even true? ───────


def test_a_silent_category_site_is_genuinely_silent_under_the_stock_default_filter() -> None:
    """Tier 3a: the claim this whole gate exists to enforce, checked
    directly -- NOT via this test's own `simplefilter`, which would only
    prove the test's own filter setting, never production's. Runs a real
    `python3` subprocess with NO `-W` flag and no injected filter (the
    same defaults any real reyn invocation starts with) against a 3-frame
    fixture (`entry.py` -> `caller_mod.py` -> `lib_mod.py`) where the
    `warnings.warn(..., DeprecationWarning, stacklevel=2)` call is
    several imports below the only `__main__` frame -- the realistic
    shape of a `src/reyn/**` call site, never itself the process entry
    point.

    NON-VACUITY: `test_the_SAME_category_IS_visible_when_the_call_site_
    IS_main` below is the required contrast -- without it, "produces no
    stderr" could just as well mean "this subprocess produces no stderr
    for anything", which would pass trivially."""
    result = subprocess.run(
        [sys.executable, "entry.py"],
        cwd=_PROD_FILTER_FIXTURES, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == "", (
        f"expected NO warning output under Python's stock default filter "
        f"(the call site is 2 frames below __main__), got: {result.stderr!r}"
    )


def test_the_same_category_is_visible_when_the_call_site_is_main() -> None:
    """Tier 3a: the required contrast for the test above -- the identical
    category, fired directly from a module that IS `__main__` when run,
    DOES produce stderr output under the same stock default filter
    (`('default', None, DeprecationWarning, '__main__', 0)`). Proves the
    silence above is the filter discriminating on the caller's module,
    not this subprocess harness swallowing everything."""
    result = subprocess.run(
        [sys.executable, "entry_direct_warn.py"],
        cwd=_PROD_FILTER_FIXTURES, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "DeprecationWarning" in result.stderr, (
        f"expected the stock default filter to show a DeprecationWarning "
        f"fired directly from __main__, got: {result.stderr!r}"
    )
