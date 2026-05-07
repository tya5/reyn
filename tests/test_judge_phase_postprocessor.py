"""Tier 2: judge_phase postprocessor pins arithmetic correctness.

The judge_phase skill delegates score computation (= passed/total over
criteria_results) to a deterministic Python postprocessor step rather than
to the LLM. These tests pin the contract of that pure function so the
arithmetic stays correct under refactors.

Tier 2 (= OS / pure-helper invariant). No mocks; the function is a plain
Python callable, imported directly.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

# Load the postprocessor module directly from the skill directory so the
# test does not depend on any package-style import path for stdlib skills.
_PP_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "judge_phase" / "postprocessor.py"
)
_spec = importlib.util.spec_from_file_location("judge_phase_postprocessor", _PP_PATH)
assert _spec is not None and _spec.loader is not None
_pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pp)
compute_score = _pp.compute_score


def _wrap(criteria_results: list[dict]) -> dict:
    """Build a wrapped {type, data} artifact with the given criteria_results."""
    return {
        "type": "phase_judgment_raw",
        "data": {
            "phase_name": "p",
            "passed": False,
            "criteria_results": criteria_results,
            "summary": "",
        },
    }


# ── empty / missing input ────────────────────────────────────────────────────


def test_compute_score_empty_criteria_returns_zero() -> None:
    """Tier 2: empty criteria_results yields 0.0 (no criteria, no signal)."""
    assert compute_score(_wrap([])) == 0.0


def test_compute_score_missing_criteria_results_returns_zero() -> None:
    """Tier 2: artifact without criteria_results key yields 0.0 (defensive default)."""
    artifact = {"type": "phase_judgment_raw", "data": {}}
    assert compute_score(artifact) == 0.0


def test_compute_score_missing_data_returns_zero() -> None:
    """Tier 2: artifact without data key yields 0.0 (defensive default)."""
    assert compute_score({"type": "phase_judgment_raw"}) == 0.0


# ── full pass / full fail ────────────────────────────────────────────────────


def test_compute_score_all_passed_returns_one() -> None:
    """Tier 2: all criteria met → 1.0."""
    results = [{"met": True}, {"met": True}, {"met": True}]
    assert compute_score(_wrap(results)) == 1.0


def test_compute_score_none_passed_returns_zero() -> None:
    """Tier 2: no criteria met → 0.0."""
    results = [{"met": False}, {"met": False}]
    assert compute_score(_wrap(results)) == 0.0


# ── fractional / rounding ────────────────────────────────────────────────────


def test_compute_score_one_of_three_rounds_to_two_decimals() -> None:
    """Tier 2: 1/3 = 0.3333… is rounded to 0.33 (two decimals)."""
    results = [{"met": True}, {"met": False}, {"met": False}]
    assert compute_score(_wrap(results)) == 0.33


def test_compute_score_two_of_three_rounds_to_two_decimals() -> None:
    """Tier 2: 2/3 = 0.6666… is rounded to 0.67 (two decimals)."""
    results = [{"met": True}, {"met": True}, {"met": False}]
    assert compute_score(_wrap(results)) == 0.67


def test_compute_score_half_returns_half() -> None:
    """Tier 2: 2/4 = 0.5 returned as 0.5 (no rounding artefact)."""
    results = [{"met": True}, {"met": True}, {"met": False}, {"met": False}]
    assert compute_score(_wrap(results)) == 0.5


# ── met semantics: only `True` counts ────────────────────────────────────────


def test_compute_score_only_true_counts_as_passed() -> None:
    """Tier 2: only `met=True` counts; missing / None / False all count as not-met.

    Pins that the helper does not treat truthy values (non-empty strings,
    truthy ints) as passed — the criterion must be the literal boolean True.
    This matches the artifact schema's `met: boolean` declaration.
    """
    results = [
        {"met": True},
        {"met": False},
        {"met": None},
        {},  # missing met key
        {"met": "true"},  # truthy string but not boolean True
        {"met": 1},  # truthy int but not boolean True
    ]
    # Only the first entry is the literal boolean True → 1/6 → 0.17
    assert compute_score(_wrap(results)) == 0.17


# ── return-type contract ─────────────────────────────────────────────────────


def test_compute_score_return_type_is_float() -> None:
    """Tier 2: return value is `float` (postprocessor step output_schema = number)."""
    score = compute_score(_wrap([{"met": True}, {"met": False}]))
    assert isinstance(score, float)


# ── range invariant ──────────────────────────────────────────────────────────


def test_compute_score_always_in_unit_interval() -> None:
    """Tier 2: result is always within [0.0, 1.0] regardless of input mix."""
    cases = [
        [],
        [{"met": True}],
        [{"met": False}],
        [{"met": True}, {"met": False}],
        [{"met": True}] * 7 + [{"met": False}] * 3,
    ]
    for results in cases:
        score = compute_score(_wrap(results))
        assert 0.0 <= score <= 1.0
