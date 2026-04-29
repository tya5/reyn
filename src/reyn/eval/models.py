"""
Evaluation framework data models.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from reyn.pricing import TokenUsage

PASS_THRESHOLD = 0.6  # criterion score >= this is considered passing


# ── Schema validation ─────────────────────────────────────────────────────────

@dataclass
class SchemaResult:
    """Result of validating an artifact against a JSON Schema."""
    passed: bool
    reason: str   # "ok" or first N jsonschema error messages

    @property
    def score(self) -> float:
        return 1.0 if self.passed else 0.0


# ── Cross-phase assertion ─────────────────────────────────────────────────────

@dataclass
class CrossPhaseAssertion:
    """Deterministic equality check between a field in two different phases."""
    phase_a: str   # e.g. "write_memo"
    path_a: str    # dot-notation field path in phase_a's artifact data
    op: str        # "==" (only operator supported for now)
    phase_b: str   # e.g. "read_verify"
    path_b: str    # dot-notation field path in phase_b's artifact data
    raw: str       # original line text for display


@dataclass
class CrossPhaseResult:
    """Result of evaluating one CrossPhaseAssertion."""
    assertion: CrossPhaseAssertion
    passed: bool
    reason: str

    @property
    def score(self) -> float:
        return 1.0 if self.passed else 0.0


# ── Quality criterion (with tag) ──────────────────────────────────────────────

@dataclass
class QualityCriterion:
    """A quality criterion with an optional tag."""
    text: str
    tag: str = "required"   # "required" | "aspirational"


# ── LLM-judge criterion result ────────────────────────────────────────────────

@dataclass
class CriterionResult:
    criterion: str
    score: float        # 0.0–1.0
    reason: str
    tag: str = "required"   # "required" | "aspirational" — set by runner after judge

    @property
    def passed(self) -> bool:
        return self.score >= PASS_THRESHOLD


# ── Phase result (schema + quality combined) ──────────────────────────────────

@dataclass
class PhaseEvalResult:
    """Evaluation of one phase's output artifact."""
    phase: str          # phase name, or "final"
    visit: int          # how many times the phase was visited
    artifact_type: str
    schema_results: list[SchemaResult]     # deterministic checks
    criteria: list[CriterionResult]        # LLM-judged quality checks

    def _required_criteria(self) -> list[CriterionResult]:
        return [c for c in self.criteria if c.tag == "required"]

    def _aspirational_criteria(self) -> list[CriterionResult]:
        return [c for c in self.criteria if c.tag == "aspirational"]

    @property
    def score(self) -> float:
        """Score counting schema + required quality only."""
        items = [s.score for s in self.schema_results] + [c.score for c in self._required_criteria()]
        return sum(items) / len(items) if items else 1.0

    @property
    def passed(self) -> int:
        return (sum(1 for s in self.schema_results if s.passed)
                + sum(1 for c in self._required_criteria() if c.passed))

    @property
    def total(self) -> int:
        return len(self.schema_results) + len(self._required_criteria())

    @property
    def aspirational_passed(self) -> int:
        return sum(1 for c in self._aspirational_criteria() if c.passed)

    @property
    def aspirational_total(self) -> int:
        return len(self._aspirational_criteria())


# ── Case result ───────────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    """Result of running one eval case."""
    case_name: str
    input: str
    run_status: str     # "finished" | "loop_limit_exceeded" | "aborted" | "error"
    phase_results: list[PhaseEvalResult]
    final_result: PhaseEvalResult | None
    cross_phase_results: list[CrossPhaseResult] = field(default_factory=list)
    run_events_path: str | None = None
    error: str | None = None

    @property
    def all_phase_results(self) -> list[PhaseEvalResult]:
        return self.phase_results + ([self.final_result] if self.final_result else [])

    @property
    def score(self) -> float:
        """Score counting schema + required quality + cross-phase only."""
        items = [
            x for pr in self.all_phase_results
            for x in ([s.score for s in pr.schema_results]
                      + [c.score for c in pr.criteria if c.tag == "required"])
        ] + [r.score for r in self.cross_phase_results]
        return sum(items) / len(items) if items else 1.0

    @property
    def passed(self) -> int:
        return (sum(pr.passed for pr in self.all_phase_results)
                + sum(1 for r in self.cross_phase_results if r.passed))

    @property
    def total(self) -> int:
        return (sum(pr.total for pr in self.all_phase_results)
                + len(self.cross_phase_results))

    @property
    def aspirational_passed(self) -> int:
        return sum(pr.aspirational_passed for pr in self.all_phase_results)

    @property
    def aspirational_total(self) -> int:
        return sum(pr.aspirational_total for pr in self.all_phase_results)


# ── Spec models ───────────────────────────────────────────────────────────────

@dataclass
class PhaseCriteria:
    """Criteria spec for one phase within a case."""
    phase: str | None                       # None = "final"
    schema: dict | None                     # JSON Schema object, or None if not specified
    criteria: list[QualityCriterion]        # quality criteria (with tags)


@dataclass
class EvalCase:
    name: str
    input: str
    phase_criteria: list[PhaseCriteria]
    cross_phase: list[CrossPhaseAssertion] = field(default_factory=list)


@dataclass
class EvalSpec:
    app_dsl_path: str
    dsl_root: str | None
    model: str | None
    judge_model: str | None
    cases: list[EvalCase]


# ── Cost summary ─────────────────────────────────────────────────────────────

@dataclass
class CostSummary:
    """Aggregated token usage and cost estimate for an eval run."""
    app_tokens: TokenUsage          # tokens from app execution (LLM calls in runtime)
    judge_tokens: TokenUsage        # tokens from LLM-as-judge calls
    estimated_cost_usd: float | None
    pricing_snapshot: dict | None   # prices used at time of run — stored for future auditing

    @property
    def total_tokens(self) -> TokenUsage:
        return self.app_tokens + self.judge_tokens

    def to_dict(self) -> dict:
        return {
            "app_tokens": self.app_tokens.to_dict(),
            "judge_tokens": self.judge_tokens.to_dict(),
            "total_tokens": self.total_tokens.to_dict(),
            "estimated_cost_usd": self.estimated_cost_usd,
            "pricing_snapshot": self.pricing_snapshot,
        }


# ── Run result ────────────────────────────────────────────────────────────────

@dataclass
class EvalRunResult:
    """Complete result of one eval run across all cases."""
    spec_path: str
    app_name: str
    model: str
    judge_model: str
    timestamp: str
    case_results: list[CaseResult]
    cost_summary: CostSummary | None = None

    @property
    def overall_score(self) -> float:
        items = [
            x for cr in self.case_results
            for pr in cr.all_phase_results
            for x in ([s.score for s in pr.schema_results]
                      + [c.score for c in pr.criteria if c.tag == "required"])
        ] + [r.score for cr in self.case_results for r in cr.cross_phase_results]
        return sum(items) / len(items) if items else 1.0

    @property
    def overall_passed(self) -> int:
        return sum(cr.passed for cr in self.case_results)

    @property
    def overall_total(self) -> int:
        return sum(cr.total for cr in self.case_results)

    def weakest_phase(self) -> str | None:
        from collections import defaultdict
        phase_scores: dict[str, list[float]] = defaultdict(list)
        for cr in self.case_results:
            for pr in cr.all_phase_results:
                if pr.total > 0:
                    phase_scores[pr.phase].append(pr.score)
        if not phase_scores:
            return None
        return min(phase_scores, key=lambda p: sum(phase_scores[p]) / len(phase_scores[p]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_path": self.spec_path,
            "app_name": self.app_name,
            "model": self.model,
            "judge_model": self.judge_model,
            "timestamp": self.timestamp,
            "overall_score": self.overall_score,
            "overall_passed": self.overall_passed,
            "overall_total": self.overall_total,
            "cost_summary": self.cost_summary.to_dict() if self.cost_summary else None,
            "cases": [
                {
                    "name": cr.case_name,
                    "input": cr.input,
                    "run_status": cr.run_status,
                    "score": cr.score,
                    "passed": cr.passed,
                    "total": cr.total,
                    "aspirational_passed": cr.aspirational_passed,
                    "aspirational_total": cr.aspirational_total,
                    "error": cr.error,
                    "run_events_path": cr.run_events_path,
                    "phases": [
                        {
                            "phase": pr.phase,
                            "visit": pr.visit,
                            "artifact_type": pr.artifact_type,
                            "score": pr.score,
                            "passed": pr.passed,
                            "total": pr.total,
                            "aspirational_passed": pr.aspirational_passed,
                            "aspirational_total": pr.aspirational_total,
                            "schema": [
                                {"passed": s.passed, "reason": s.reason}
                                for s in pr.schema_results
                            ],
                            "criteria": [
                                {"criterion": c.criterion, "score": c.score,
                                 "reason": c.reason, "passed": c.passed, "tag": c.tag}
                                for c in pr.criteria
                            ],
                        }
                        for pr in cr.all_phase_results
                    ],
                    "cross_phase": [
                        {
                            "raw": r.assertion.raw,
                            "passed": r.passed,
                            "reason": r.reason,
                        }
                        for r in cr.cross_phase_results
                    ],
                }
                for cr in self.case_results
            ],
        }
