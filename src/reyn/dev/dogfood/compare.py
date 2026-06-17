"""Baseline vs candidate regression compare (FP-0036 Component E).

Mirrors ``reyn eval compare``: takes two RunResults (baseline, candidate),
emits a regression report. Exits 1 if any scenario regressed AND the
verified-rate drop exceeds ``threshold`` (default 0.05 = 5 percentage points).

Outcome ordering (worst → best):
  blocked < refuted < inconclusive < verified

A scenario is ``regressed`` when its candidate outcome ranks lower than
its baseline outcome.  A scenario is ``improved`` when the candidate ranks
higher than the baseline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.dev.dogfood.runner import RunResult

from reyn.dev.dogfood.runner import OUTCOME_ORDER, _outcome_rank

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ScenarioDelta:
    """Per-scenario comparison between a baseline and a candidate run."""

    scenario_id: str
    baseline_outcome: str
    candidate_outcome: str
    regressed: bool    # candidate is worse than baseline (e.g. verified → refuted)
    improved: bool     # candidate is better than baseline (e.g. refuted → verified)


@dataclass
class CompareReport:
    """Aggregated comparison between a baseline run and a candidate run."""

    deltas: list[ScenarioDelta] = field(default_factory=list)
    baseline_verified_rate: float = 0.0
    candidate_verified_rate: float = 0.0
    regressed_scenarios: list[str] = field(default_factory=list)
    improved_scenarios: list[str] = field(default_factory=list)

    @property
    def verified_rate_delta(self) -> float:
        """Candidate verified rate minus baseline verified rate.

        Positive = improvement. Negative = regression.
        """
        return self.candidate_verified_rate - self.baseline_verified_rate

    @property
    def regression_detected(self) -> bool:
        """True if any scenario regressed (regardless of threshold)."""
        return len(self.regressed_scenarios) > 0

    def exceeds_threshold(self, threshold: float = 0.05) -> bool:
        """Return True if the verified_rate_delta drops by more than *threshold*.

        The CLI uses this to determine exit code 1 (regression alert).
        """
        return self.verified_rate_delta < -abs(threshold)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compare_runs(
    baseline: "RunResult",
    candidate: "RunResult",
    *,
    threshold: float = 0.05,
) -> CompareReport:
    """Produce a ``CompareReport`` comparing *candidate* against *baseline*.

    The caller (= CLI) decides exit code based on
    ``report.exceeds_threshold(threshold)``.

    Parameters
    ----------
    baseline:
        The reference ``RunResult`` (earlier / known-good run).
    candidate:
        The ``RunResult`` being validated against the baseline.
    threshold:
        Verified-rate drop that triggers a regression alert.  Default 0.05 =
        5 percentage points.  The CLI uses ``report.exceeds_threshold()`` to
        check this after the report is returned.

    Returns
    -------
    CompareReport
    """
    # Build lookup by scenario_id for both sides
    baseline_map: dict[str, str] = {
        sr.scenario_id: sr.overall_outcome
        for sr in baseline.scenario_results
    }
    candidate_map: dict[str, str] = {
        sr.scenario_id: sr.overall_outcome
        for sr in candidate.scenario_results
    }

    # All scenario IDs seen in either run
    all_ids = sorted(set(baseline_map) | set(candidate_map))

    deltas: list[ScenarioDelta] = []
    regressed: list[str] = []
    improved: list[str] = []

    for sid in all_ids:
        base_outcome = baseline_map.get(sid, "inconclusive")
        cand_outcome = candidate_map.get(sid, "inconclusive")

        base_rank = _outcome_rank(base_outcome)
        cand_rank = _outcome_rank(cand_outcome)

        is_regressed = cand_rank < base_rank
        is_improved = cand_rank > base_rank

        delta = ScenarioDelta(
            scenario_id=sid,
            baseline_outcome=base_outcome,
            candidate_outcome=cand_outcome,
            regressed=is_regressed,
            improved=is_improved,
        )
        deltas.append(delta)

        if is_regressed:
            regressed.append(sid)
        elif is_improved:
            improved.append(sid)

    # Verified rates
    def _verified_rate(run: "RunResult") -> float:
        if not run.scenario_results:
            return 0.0
        verified_count = sum(
            1 for sr in run.scenario_results if sr.overall_outcome == "verified"
        )
        return verified_count / len(run.scenario_results)

    return CompareReport(
        deltas=deltas,
        baseline_verified_rate=_verified_rate(baseline),
        candidate_verified_rate=_verified_rate(candidate),
        regressed_scenarios=regressed,
        improved_scenarios=improved,
    )
