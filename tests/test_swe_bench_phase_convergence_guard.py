"""Tier 2: OS invariant — swe_bench apply/verify phases contain convergence
guard instructions (FP-0008 PR-P v8).

Primary evidence backing (= per-turn act-op observation on v8 trace):

Instance 13579 (apply, 32 turns, budget=30):
  - T5–T19: 15 consecutive read(sliced_low_level_wcs.py) with NO edit/write
  - T21–T31: 11 consecutive read(sliced_low_level_wcs.py) with NO edit/write
  - 27/32 turns were read-only; CIR at T20 held 17 duplicate read results
  - T32: decide:abort (budget exhausted)

Instance 14182 (verify, 35 turns, budget=30):
  - T4–T34: 30 consecutive write(.reyn/swe_bench_test.patch) + sh(git apply)
  - IDENTICAL patch content all 30 times; IDENTICAL rc=128 error all 30 times
  - T35: decide:abort (budget exhausted)

Root cause (B2, dominant): apply.md and verify.md lacked explicit convergence
signal — the LLM had no instruction to STOP re-reading / retrying when making
no forward progress.  B1 (budget too tight) and B3 (context balloon) were
secondary contributors but not the dominant cause:
  - B1 ruled out: LLM was NOT making progress-shaped ops before cutoff.
    27/32 turns in 13579 were reads. Raising the budget would extend the loop.
  - B3 ruled out as dominant: individual CIR entries were small (354 chars per
    read), so context did not balloon to the 100K+ range observed in PR-N
    instances.  The accumulation was a symptom, not the driver.

Fix: add a "Convergence guard — MANDATORY" section to apply.md and verify.md
that explicitly instructs the LLM to stop after N consecutive same-op fails.

This test pins that both phase instruction files contain the convergence guard
section, preventing regression.

No mocks.  No private-state assertions.  Uses on-disk file reads only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_PHASES_DIR = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "swe_bench" / "phases"
)

_APPLY_MD = _PHASES_DIR / "apply.md"
_VERIFY_MD = _PHASES_DIR / "verify.md"

# The exact section header that must appear in both files.
_CONVERGENCE_HEADER = "## Convergence guard"

# Minimum number of turns that must be mentioned in the guard
# (= the threshold beyond which the LLM must stop re-reading/retrying).
_MIN_THRESHOLD_MENTIONED = 3


def _read_phase(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1: apply.md contains convergence guard section
# ---------------------------------------------------------------------------

def test_apply_md_has_convergence_guard_section():
    """Tier 2: apply.md must contain a Convergence guard section.

    Without this section the LLM has no explicit signal to stop re-reading
    the same file in consecutive turns.  Per-turn observation of instance
    13579 (v8 trace) showed 27/32 turns were read-only, consuming the
    entire budget with zero forward progress.

    Regression prevention: this test blocks any edit to apply.md that
    removes the convergence guard header.
    """
    text = _read_phase(_APPLY_MD)
    assert _CONVERGENCE_HEADER in text, (
        f"apply.md must contain a '{_CONVERGENCE_HEADER}' section. "
        f"Per-turn observation of instance 13579 (FP-0008 v8) showed 27/32 "
        f"turns were read-only without this guard — budget exhausted with no "
        f"progress.  Add the convergence guard back to apply.md."
    )


# ---------------------------------------------------------------------------
# Test 2: verify.md contains convergence guard section
# ---------------------------------------------------------------------------

def test_verify_md_has_convergence_guard_section():
    """Tier 2: verify.md must contain a Convergence guard section.

    Without this section the LLM has no explicit signal to stop retrying
    a failing git apply command.  Per-turn observation of instance 14182
    (v8 trace) showed 30/35 turns were identical write+apply pairs, all
    returning rc=128 with the same error — budget exhausted with zero
    forward progress.

    Regression prevention: this test blocks any edit to verify.md that
    removes the convergence guard header.
    """
    text = _read_phase(_VERIFY_MD)
    assert _CONVERGENCE_HEADER in text, (
        f"verify.md must contain a '{_CONVERGENCE_HEADER}' section. "
        f"Per-turn observation of instance 14182 (FP-0008 v8) showed 30/35 "
        f"turns were identical write+shell pairs — budget exhausted.  Add the "
        f"convergence guard back to verify.md."
    )


# ---------------------------------------------------------------------------
# Test 3: apply.md convergence guard mentions a concrete threshold
# ---------------------------------------------------------------------------

def test_apply_md_convergence_guard_mentions_threshold():
    """Tier 2: apply.md convergence guard must mention a concrete N-turn threshold.

    A guard that says 'stop looping' without a concrete number is
    under-specified.  The LLM needs an unambiguous threshold: 'if you have
    read the same file N times in a row without writing, STOP'.
    """
    text = _read_phase(_APPLY_MD)
    # Extract the convergence guard section
    start = text.find(_CONVERGENCE_HEADER)
    assert start != -1  # already checked by test_apply_md_has_convergence_guard_section
    guard_section = text[start:]

    # The guard section must mention a number (the consecutive-op threshold).
    # We check for the minimum threshold (3) as a digit within the section.
    found_number = any(
        str(n) in guard_section
        for n in range(_MIN_THRESHOLD_MENTIONED, 10)
    )
    assert found_number, (
        f"apply.md Convergence guard section must mention a numeric threshold "
        f"(>= {_MIN_THRESHOLD_MENTIONED}) for consecutive same-file reads before "
        f"stopping.  A concrete number prevents ambiguity for the LLM."
    )


# ---------------------------------------------------------------------------
# Test 4: verify.md convergence guard mentions a concrete threshold
# ---------------------------------------------------------------------------

def test_verify_md_convergence_guard_mentions_threshold():
    """Tier 2: verify.md convergence guard must mention a concrete N-turn threshold.

    Same rationale as the apply.md test: the guard must be quantitative.
    For verify, the threshold applies to consecutive failing git apply
    invocations with the same error.
    """
    text = _read_phase(_VERIFY_MD)
    start = text.find(_CONVERGENCE_HEADER)
    assert start != -1  # already checked by test_verify_md_has_convergence_guard_section
    guard_section = text[start:]

    found_number = any(
        str(n) in guard_section
        for n in range(_MIN_THRESHOLD_MENTIONED, 10)
    )
    assert found_number, (
        f"verify.md Convergence guard section must mention a numeric threshold "
        f"(>= {_MIN_THRESHOLD_MENTIONED}) for consecutive git apply failures before "
        f"stopping.  A concrete number prevents ambiguity for the LLM."
    )


# ---------------------------------------------------------------------------
# Test 5: apply.md convergence guard explicitly mentions transition as the action
# ---------------------------------------------------------------------------

def test_apply_md_convergence_guard_has_action():
    """Tier 2: apply.md convergence guard must specify a concrete action to take.

    The guard must tell the LLM WHAT to do (transition or issue the edit),
    not just WHEN to stop.  Without a prescribed action, the LLM may stop
    reading but then idle or abort instead of transitioning productively.
    """
    text = _read_phase(_APPLY_MD)
    start = text.find(_CONVERGENCE_HEADER)
    assert start != -1
    guard_section = text[start:]

    # The guard should mention transition or edit as an action.
    action_keywords = ("transition", "edit", "write op")
    assert any(kw in guard_section.lower() for kw in action_keywords), (
        f"apply.md Convergence guard must specify a concrete action (one of: "
        f"{action_keywords}).  Telling the LLM when to stop is insufficient "
        f"without also telling it what to do instead."
    )


# ---------------------------------------------------------------------------
# Test 6: verify.md convergence guard has transition-back-to-apply as action
# ---------------------------------------------------------------------------

def test_verify_md_convergence_guard_has_apply_transition_action():
    """Tier 2: verify.md convergence guard must specify transitioning back to apply.

    In the observed failure pattern (14182), the correct action when git apply
    keeps failing is to go back to the apply phase to re-examine the fix.
    The guard must explicitly prescribe this transition so the LLM exits the
    verify dead-end loop productively rather than aborting.
    """
    text = _read_phase(_VERIFY_MD)
    start = text.find(_CONVERGENCE_HEADER)
    assert start != -1
    guard_section = text[start:]

    # The guard section must mention transitioning back to apply.
    assert "apply" in guard_section.lower(), (
        "verify.md Convergence guard must mention transitioning back to 'apply' "
        "as the action when git apply fails repeatedly.  Without this, the LLM "
        "will abort instead of returning to the apply phase for re-examination."
    )
