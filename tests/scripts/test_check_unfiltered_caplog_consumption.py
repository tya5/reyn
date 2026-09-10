"""Tier 1: `check_unfiltered_caplog_consumption.py`'s own detector logic --
the recurrence-prevention gate for the caplog-consumption bug pattern that
hit #6031, #6035, and #6038 independently the same day.

Real fixture files under `tests/scripts/fixtures/unfiltered_caplog_6042/`
(permanently checked in, not a strip-and-revert scratch), mirroring
`test_silent_except_ratchet_5990.py`'s own established shape: one fixture
carrying every dangerous shape this gate must catch, one carrying every
safe/subject-specific idiom it must NOT flag -- both directions required
so a detector that flags every caplog access unconditionally (which would
pass the dangerous-fixture test alone) cannot pass this suite either.
"""
from __future__ import annotations

from pathlib import Path

from scripts.check_unfiltered_caplog_consumption import find_violations, measured
from tests._support.paths import REPO_ROOT

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "unfiltered_caplog_6042"


def test_every_dangerous_shape_is_flagged():
    """Tier 1: the false-accept-side witness -- each of the 4 dangerous
    consumption shapes (equality-with-literal-empty, len() comparison,
    tuple/sequence unpacking, unfiltered any()/all()) is caught, one per
    function in the fixture."""
    violations = find_violations(_FIXTURES / "dangerous.py")
    shapes = {shape for _, shape in violations}
    expected_shapes = {
        "equality with literal",
        "len() comparison",
        "tuple/sequence unpacking",
        "unfiltered any()",
    }
    missing = expected_shapes - shapes
    assert not missing, (
        f"gate missed dangerous shape(s) {missing} in the fixture -- got "
        f"{violations}"
    )


def test_every_safe_shape_is_not_flagged():
    """Tier 1: the false-reject-side witness, required alongside the test
    above -- a subject-specific/filtered consumption (message-content
    membership, logger-name-filtered equality/any(), .text containment, a
    two-stage derived-variable filter, and a level-only filter that never
    reaches a dangerous shape at all) must NOT be flagged."""
    violations = find_violations(_FIXTURES / "safe.py")
    assert violations == [], (
        f"gate over-flagged subject-specific/never-consumed caplog usage: "
        f"{violations}"
    )


def test_the_real_repo_tree_is_currently_clean():
    """Tier 2: the gate's own starting population, verified against the
    real, current tree (not assumed) -- mirrors check_no_core_dep_
    importorskip_5058.py's own "run it before shipping it" discipline.
    This gate is zero-tolerance (no baseline) -- both of the read-only
    measurement's remaining live sites (test_5509_media_capability_gate.
    py:220, test_5168_tui_stdio_capture.py:118) are fixed in the same PR
    that adds this gate, so this must be empty; any hit here is a NEW
    regression."""
    by_file, scanned = measured(REPO_ROOT)
    assert scanned > 1000, (
        f"the scan found only {scanned} file(s) under tests/ -- looks like "
        "a scanner failure (git ls-files returning too little), not a "
        "small real tree"
    )
    assert by_file == {}, (
        f"real regression(s) found: {by_file} -- this gate's baseline is "
        "zero (no grandfather list), so any hit here is new, not "
        "inherited debt"
    )
