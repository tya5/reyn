"""Tier 1/2: `silent_warnings_category_gate.py`'s own detector logic
(#6143/#6144).

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

#6144 co-vet round 2 (lead-coder): the gate's v1 axis (silent-by-default
categories only) was itself trivially defeated -- swap
`DeprecationWarning` -> `UserWarning` in any one call site and the gate
goes green with the operator's actual visibility unchanged (architect's
own measurement: `UserWarning` reaches `reyn.log`, never an interactive
operator's screen). v2's population is EVERY `warnings.warn(...)` call
under `src/reyn/**`, category-blind -- the tests below were updated to
match (`test_a_user_warning_or_omitted_category_is_never_counted`
inverted into `test_every_warnings_warn_call_is_counted_regardless_of_
category`, and the shipped-table tests now check the 18
`#6145`-tracked pre-existing entries rather than an empty table).

#6144 co-vet round 3 (lead-coder): v2's `(relpath, lineno)` key was
ITSELF gameable -- not by an author dodging the gate, but by an
ordinary edit: an exempted site moves down (any edit above it does
this), and a genuinely NEW, unaudited `warnings.warn` lands on the now-
vacant line number, silently inheriting the old exemption. Position
alone is not identity. The key gained a 3rd coordinate,
`message_template` (the message argument with every f-string expression
normalised to `"{}"`, truncated to 40 chars) -- `test_a_different_
message_at_an_exempted_line_is_not_silently_exempted` below is the
direct witness for exactly the scenario lead-coder described.
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
    categories = sorted(category for _, _, category, _ in sites)
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
    lines = {lineno for _, lineno, _, _ in sites}
    text = (_FIXTURES / "silent_categories.py").read_text(encoding="utf-8")
    keyword_call_line = next(
        i + 1 for i, line in enumerate(text.splitlines()) if "category=DeprecationWarning" in line
    )
    assert keyword_call_line in lines, (
        "the keyword-form warnings.warn(..., category=DeprecationWarning) "
        "call was not detected -- category resolution only handled the "
        "positional form"
    )


def test_every_warnings_warn_call_is_counted_regardless_of_category():
    """Tier 1: v2's own axis (#6144 co-vet round 2) -- EVERY
    `warnings.warn(...)` call counts, category-blind, so a call site
    cannot dodge the gate by swapping to `UserWarning` or omitting the
    category. `loud_categories.py`'s two `warnings.warn` calls (explicit
    UserWarning, omitted category) must BOTH be counted; its
    `logger.warning` call is not even a `warnings.warn` call and must
    not be (the false-reject-side witness, required alongside this test
    so a detector that counts every CALL in the file, not just
    `warnings.warn` ones, cannot also pass)."""
    sites = silent_category_sites(_FIXTURES / "loud_categories.py", root=_FIXTURES)
    # exactly 2 sites -- unpacking itself raises if the count is off
    (_site_a, _site_b) = sites
    assert sorted(category for _, _, category, _ in sites) == ["UserWarning", "UserWarning"]


# ── the exception-table arithmetic ───────────────────────────────────────


def test_a_site_with_no_table_entry_is_unexplained():
    """Tier 2: the gate's own pass/fail arithmetic -- a measured site
    whose full (relpath, lineno, message_template) key is absent from
    `_EXCEPTION_TABLE` is what makes the gate red."""
    sites = [("some/file.py", 10, "DeprecationWarning", "some message")]
    assert unexplained(sites) == sites


def test_a_site_matching_a_table_entry_is_explained():
    """Tier 2: deny side -- a site whose (relpath, lineno,
    message_template) IS in the table is not reported, regardless of the
    category string (the table key is positional+message identity,
    never category)."""
    sites = [("some/file.py", 10, "DeprecationWarning", "some message")]
    assert unexplained(
        sites, table={("some/file.py", 10, "some message"): "reviewed, dev-only"},
    ) == []


def test_a_different_message_at_an_exempted_line_is_not_silently_exempted():
    """Tier 2: the DIRECT witness for #6144 co-vet round 3's own
    scenario -- an ordinary edit moves an exempted site down, and a
    genuinely new, unaudited `warnings.warn` lands on the now-vacant
    line number. Sharing only (relpath, lineno) with a table entry must
    NOT be enough: the message differs, so the composite key misses, and
    this new site is unexplained -- exactly what a `(relpath, lineno)`-
    only key (v2) would have missed."""
    sites = [("some/file.py", 10, "UserWarning", "a completely different message")]
    assert unexplained(
        sites, table={("some/file.py", 10, "the OLD exempted message"): "reviewed, dev-only"},
    ) == sites


def test_the_shipped_exception_table_matches_the_real_measured_population():
    """Tier 1: #6143 fixed and DELETED all 7 originally-flagged sites (6
    promoted to `logger.warning`, the 7th -- `permissions.py`'s legacy
    `http.get` compat notice -- deleted outright in #6144's co-vet round
    2, since it duplicated an already-firing real prompt). Widening the
    axis to category-blind (#6144 co-vet round 2) newly surfaces
    pre-existing sites this PR's own dispatch never covered -- this test
    pins BEHAVIOR, not a bare count: the table's own keys must equal
    EXACTLY the real tree's measured population (neither a stale entry
    for a site that no longer exists, nor a gap), and every reason must
    reference #6145, the tracking issue for auditing and promoting them
    -- never a bare "fine as-is", which #6144's own review already ruled
    none of these currently are."""
    measured_keys = {
        (relpath, lineno, template) for relpath, lineno, _, template in measured(REPO_ROOT)
    }
    assert set(_EXCEPTION_TABLE) == measured_keys, (
        f"table declares {set(_EXCEPTION_TABLE) - measured_keys} that no "
        f"longer exist, and is missing {measured_keys - set(_EXCEPTION_TABLE)}"
    )
    assert all("#6145" in reason for reason in _EXCEPTION_TABLE.values()), _EXCEPTION_TABLE


def test_the_real_scan_against_the_current_tree_has_nothing_unexplained() -> None:
    """Tier 1: the load-bearing witness -- running the real scan against
    the real repo tree, right now, must find nothing outside the
    exception table. Mirrors `silent_except_ratchet.py`'s own identically
    -shaped tree-scan test."""
    sites = measured(REPO_ROOT)
    assert unexplained(sites) == [], (
        "a warnings.warn(...) call under src/reyn/ has no "
        "_EXCEPTION_TABLE entry -- promote it to logger.warning or add "
        "a reasoned table entry"
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
