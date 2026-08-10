"""Tier 2: #3878 Phase 2 mechanization — the 5 candidate-surfacing signals.

Real filesystem fixtures throughout (a real `tmp_path` tree of `.py` files)
— the function under test reads real file content and parses real ASTs, so
faking the filesystem would test nothing real. Each signal is enumeration
only (no judgment) — these tests pin "does the signal fire on the exact
shape it exists to catch", not "is the flagged test actually Tier 4".
"""
from __future__ import annotations

import ast
from pathlib import Path

from scripts.tier4_candidate_signals import (
    docstring_negative_with_issue,
    mass_produced_assert_shape,
    narrow_tier2,
    regression_named,
    third_party_only_asserts,
)


def _write(tmp_path: Path, name: str, body: str) -> None:
    (tmp_path / name).write_text(body, encoding="utf-8")


# ── signal 1 ─────────────────────────────────────────────────────────────


def test_third_party_only_asserts_flags_a_reyn_free_assert(tmp_path: Path) -> None:
    """Tier 2: THE case this signal exists to catch — every assert in the
    test compares plain literals, never anything traced from the file's
    own reyn imports."""
    _write(
        tmp_path, "test_a.py",
        "from reyn.core.foo import bar\n\n"
        "def test_x():\n"
        "    result = bar()\n"
        "    assert 1 + 1 == 2\n",
    )
    candidates = third_party_only_asserts(tmp_path)
    assert [c.test_name for c in candidates] == ["test_x"]


def test_third_party_only_asserts_not_flagged_when_assert_touches_reyn(tmp_path: Path) -> None:
    """Tier 2: accept-side — an assert that DOES reference the imported
    reyn name, in the SAME expression, must not be flagged."""
    _write(
        tmp_path, "test_a.py",
        "from reyn.core.foo import bar\n\n"
        "def test_x():\n"
        "    assert bar() == 1\n",
    )
    candidates = third_party_only_asserts(tmp_path)
    assert candidates == []


def test_third_party_only_asserts_known_limitation_misses_a_variable_hop(
    tmp_path: Path,
) -> None:
    """Tier 2: KNOWN LIMITATION, pinned rather than hidden — the signal
    checks only the NAMES literally appearing in the assert's own
    expression, no data-flow tracing. `result = bar(); assert result == 1`
    is textually indistinguishable from an assert on a pure third-party
    value, even though `result` traces back to a reyn call one line above
    — this is measured (#3878 dispatch) as the likely cause of the signal's
    69% real-tree fire rate, far too broad to prioritize reading order. A
    real fix needs data-flow tracing (does the asserted name's binding,
    anywhere earlier in the function, come from a reyn call?), out of
    scope for this enumeration pass."""
    _write(
        tmp_path, "test_a.py",
        "from reyn.core.foo import bar\n\n"
        "def test_x():\n"
        "    result = bar()\n"
        "    assert result == 1\n",
    )
    candidates = third_party_only_asserts(tmp_path)
    # Documents the false positive — does NOT assert this is correct
    # behavior, only that it's the CURRENT, known, measured behavior.
    assert [c.test_name for c in candidates] == ["test_x"]


def test_third_party_only_asserts_skips_files_with_no_reyn_import(tmp_path: Path) -> None:
    """Tier 2: a file with no reyn import at all is out of scope for this
    signal (nothing to compare against) — not the same as "flag everything"."""
    _write(tmp_path, "test_a.py", "def test_x():\n    assert 1 == 1\n")
    candidates = third_party_only_asserts(tmp_path)
    assert candidates == []


# ── signal 2 ─────────────────────────────────────────────────────────────


def test_docstring_negative_with_issue_flags_issue_plus_negative(tmp_path: Path) -> None:
    """Tier 2: THE case this signal exists to catch — a docstring naming an
    issue AND using negative framing, the past-bug-fingerprint shape."""
    _write(
        tmp_path, "test_a.py",
        'def test_x():\n'
        '    """Tier 2: #123 the value is not None."""\n'
        '    assert 1 == 1\n',
    )
    candidates = docstring_negative_with_issue(tmp_path)
    assert [c.test_name for c in candidates] == ["test_x"]


def test_docstring_negative_with_issue_not_flagged_without_both(tmp_path: Path) -> None:
    """Tier 2: accept-side — an issue number ALONE, or a negative word
    ALONE, does not fire; both must co-occur."""
    _write(
        tmp_path, "test_a.py",
        'def test_x():\n'
        '    """Tier 2: #123 the value equals the expected constant."""\n'
        '    assert 1 == 1\n\n'
        'def test_y():\n'
        '    """Tier 2: the value is not empty."""\n'
        '    assert 1 == 1\n',
    )
    candidates = docstring_negative_with_issue(tmp_path)
    assert candidates == []


# ── signal 3 ─────────────────────────────────────────────────────────────


def test_regression_named_flags_the_three_name_shapes(tmp_path: Path) -> None:
    """Tier 2: all three naming smells fire (`regression`, `not_`, `no_`),
    an ordinary name does not."""
    _write(
        tmp_path, "test_a.py",
        "def test_known_regression_case():\n    assert True\n\n"
        "def test_a_not_flagged_path_is_clean():\n    assert True\n\n"
        "def test_no_leak_occurs():\n    assert True\n\n"
        "def test_an_ordinary_case():\n    assert True\n",
    )
    candidates = {c.test_name for c in regression_named(tmp_path)}
    assert candidates == {
        "test_known_regression_case",
        "test_a_not_flagged_path_is_clean",
        "test_no_leak_occurs",
    }


# ── signal 4 ─────────────────────────────────────────────────────────────


def test_mass_produced_assert_shape_flags_repeated_shape(tmp_path: Path) -> None:
    """Tier 2: every assert sharing the repeated shape is flagged, not just
    some of them (completeness, checked against the source's own assert
    line numbers — not a hardcoded count)."""
    body = "def test_x():\n"
    for i in range(6):
        body += f"    assert data[{i}] == 1\n"
    _write(tmp_path, "test_a.py", body)
    candidates = mass_produced_assert_shape(tmp_path, threshold=5)
    # normalized shape ignores the literal 1 AND the subscript index (both
    # Constants) -- all asserts in the source share one shape.
    all_assert_lines = {n.lineno for n in ast.walk(ast.parse(body)) if isinstance(n, ast.Assert)}
    assert {c.lineno for c in candidates} == all_assert_lines
    assert all(c.test_name == "test_x" for c in candidates)


def test_mass_produced_assert_shape_not_flagged_below_threshold(tmp_path: Path) -> None:
    """Tier 2: accept-side — fewer repeats than the threshold does not fire."""
    _write(
        tmp_path, "test_a.py",
        "def test_x():\n    assert data[0] == 1\n    assert data[1] == 1\n",
    )
    candidates = mass_produced_assert_shape(tmp_path, threshold=5)
    assert candidates == []


def test_mass_produced_assert_shape_distinguishes_different_shapes(tmp_path: Path) -> None:
    """Tier 2: non-vacuity — asserts on DIFFERENT variables/operators don't
    collapse into the same shape just because both use literals."""
    # Built once, reused for both the written file AND the independent
    # line-number computation below -- no duplicated construction to drift
    # out of sync with each other.
    source = "def test_x():\n" + ("    assert a == 1\n" * 3) + ("    assert b != 2\n" * 3)
    _write(tmp_path, "test_a.py", source)
    candidates = mass_produced_assert_shape(tmp_path, threshold=3)
    # two distinct shapes, 3 each -- both meet threshold=3, tracked as
    # separate shape groups. Completeness check against the source's own
    # assert lines, not a hardcoded count.
    all_assert_lines = {n.lineno for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Assert)}
    assert {c.lineno for c in candidates} == all_assert_lines


# ── signal 5 ─────────────────────────────────────────────────────────────


def test_narrow_tier2_flags_single_reyn_call_site(tmp_path: Path) -> None:
    """Tier 2: THE case this signal exists to catch — a Tier 2 declaration
    resting on exactly one distinct reyn-sourced call site."""
    _write(
        tmp_path, "test_a.py",
        "from reyn.core.foo import bar\n\n"
        "def test_x():\n"
        '    """Tier 2: bar behaves correctly."""\n'
        "    assert bar() == 1\n",
    )
    candidates = narrow_tier2(tmp_path)
    assert [c.test_name for c in candidates] == ["test_x"]


def test_narrow_tier2_not_flagged_with_multiple_reyn_names(tmp_path: Path) -> None:
    """Tier 2: accept-side — touching TWO distinct reyn names does not fire
    (the signal is specifically about a SINGLE narrow call site)."""
    _write(
        tmp_path, "test_a.py",
        "from reyn.core.foo import bar, baz\n\n"
        "def test_x():\n"
        '    """Tier 2: bar and baz interact correctly."""\n'
        "    assert bar() == baz()\n",
    )
    candidates = narrow_tier2(tmp_path)
    assert candidates == []


def test_narrow_tier2_not_flagged_for_non_tier2_docstring(tmp_path: Path) -> None:
    """Tier 2: accept-side — a Tier 1 declaration with the same narrow
    call-site shape does not fire; the signal is specifically about a
    Tier 2 claim, not narrowness alone."""
    _write(
        tmp_path, "test_a.py",
        "from reyn.core.foo import bar\n\n"
        "def test_x():\n"
        '    """Tier 1: contract for bar()."""\n'
        "    assert bar() == 1\n",
    )
    candidates = narrow_tier2(tmp_path)
    assert candidates == []
