"""
EvalRunner: orchestrates per-case app execution and per-phase LLM-as-judge evaluation.
"""
from __future__ import annotations
import jsonschema
from pathlib import Path
from typing import Any, Callable

_FILE_EXTENSIONS = {".md", ".yaml", ".yml", ".txt"}

from reyn.eval.models import (
    EvalSpec, EvalCase, EvalRunResult, CaseResult, PhaseEvalResult,
    CriterionResult, SchemaResult,
    CrossPhaseAssertion, CrossPhaseResult, CostSummary,
)
from reyn.eval.judge import judge_artifact
from reyn.agent import Agent
from reyn.model_resolver import ModelResolver
from reyn.models import App
from reyn.pricing import TokenUsage, estimate_cost


# ── Schema evaluation ─────────────────────────────────────────────────────────

def _collect_file_contents(artifact: dict) -> dict[str, str]:
    """Read files referenced by path strings inside artifact data."""
    contents: dict[str, str] = {}
    data = artifact.get("data", {})

    def _scan(val: Any) -> None:
        if isinstance(val, str) and Path(val).suffix in _FILE_EXTENSIONS:
            p = Path(val)
            if p.exists() and p.is_file() and val not in contents:
                try:
                    contents[val] = p.read_text(encoding="utf-8")
                except Exception:
                    pass
        elif isinstance(val, list):
            for item in val:
                _scan(item)
        elif isinstance(val, dict):
            for v in val.values():
                _scan(v)

    _scan(data)
    return contents


def _get_nested(data: dict, path: str) -> tuple[bool, Any]:
    """Resolve plain dot-notation path into data dict. Returns (found, value)."""
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _evaluate_schema(artifact: dict, schema: dict | None) -> list[SchemaResult]:
    """Validate artifact data against a JSON Schema. Returns 0 results if no schema."""
    if not schema:
        return []
    data = artifact.get("data", {})
    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=str)
    if not errors:
        return [SchemaResult(passed=True, reason="ok")]
    reasons = [e.message for e in errors[:5]]
    return [SchemaResult(passed=False, reason="; ".join(reasons))]


# ── Cross-phase evaluation ────────────────────────────────────────────────────

def _evaluate_cross_phase(
    phase_data: dict[str, dict],
    assertions: list[CrossPhaseAssertion],
) -> list[CrossPhaseResult]:
    """Check equality between fields from two different phase artifacts."""
    results: list[CrossPhaseResult] = []

    for a in assertions:
        # Resolve phase_a
        if a.phase_a not in phase_data:
            results.append(CrossPhaseResult(
                assertion=a, passed=False,
                reason=f"phase '{a.phase_a}' did not execute",
            ))
            continue
        found_a, val_a = _get_nested(phase_data[a.phase_a], a.path_a)
        if not found_a:
            results.append(CrossPhaseResult(
                assertion=a, passed=False,
                reason=f"field '{a.path_a}' not found in phase '{a.phase_a}'",
            ))
            continue

        # Resolve phase_b
        if a.phase_b not in phase_data:
            results.append(CrossPhaseResult(
                assertion=a, passed=False,
                reason=f"phase '{a.phase_b}' did not execute",
            ))
            continue
        found_b, val_b = _get_nested(phase_data[a.phase_b], a.path_b)
        if not found_b:
            results.append(CrossPhaseResult(
                assertion=a, passed=False,
                reason=f"field '{a.path_b}' not found in phase '{a.phase_b}'",
            ))
            continue

        if a.op == "==":
            passed = val_a == val_b
            reason = "ok" if passed else f"{val_a!r} != {val_b!r}"
            results.append(CrossPhaseResult(assertion=a, passed=passed, reason=reason))

    return results


# ── Runner ────────────────────────────────────────────────────────────────────

class EvalRunner:
    def __init__(
        self,
        spec: EvalSpec,
        app: App,
        model: str,
        judge_model: str,
        state_dir: str = ".reyn/eval_runs",
        output_language: str = "ja",
        app_subscribers: list[Callable] | None = None,
        on_case_start: Callable[[str], None] | None = None,
        on_phase_judged: Callable[[str, PhaseEvalResult], None] | None = None,
        resolver: ModelResolver | None = None,
    ) -> None:
        self.spec = spec
        self.app = app
        self.model = model
        self.judge_model = judge_model
        self.state_dir = state_dir
        self.output_language = output_language
        self.app_subscribers = app_subscribers or []
        self.on_case_start = on_case_start
        self.on_phase_judged = on_phase_judged
        self._resolver = resolver or ModelResolver({})
        # Accumulated token usage across all run_case() calls
        self._app_tokens: TokenUsage = TokenUsage()
        self._judge_tokens: TokenUsage = TokenUsage()

    def build_cost_summary(self) -> CostSummary:
        """Build cost summary from accumulated token usage across all run_case() calls."""
        total = self._app_tokens + self._judge_tokens
        # Cost is estimated using the app model; judge model may differ.
        # We compute separate estimates and sum them, falling back to app model for judge
        # if judge_model is not in the pricing table.
        app_cost, app_snapshot = estimate_cost(self.model, self._app_tokens)
        judge_cost, judge_snapshot = estimate_cost(self.judge_model, self._judge_tokens)

        if app_cost is not None or judge_cost is not None:
            estimated_cost = (app_cost or 0.0) + (judge_cost or 0.0)
            # Use the snapshot for the model that covers the most tokens
            snapshot = app_snapshot or judge_snapshot
        else:
            estimated_cost = None
            snapshot = None

        return CostSummary(
            app_tokens=self._app_tokens,
            judge_tokens=self._judge_tokens,
            estimated_cost_usd=estimated_cost,
            pricing_snapshot=snapshot,
        )

    def run_case(self, case: EvalCase) -> CaseResult:
        if self.on_case_start:
            self.on_case_start(case.name)

        case_state_dir = str(
            Path(self.state_dir) / self.app.name / case.name
        )
        agent = Agent(
            model=self.model,
            state_dir=case_state_dir,
            subscribers=self.app_subscribers,
            resolver=self._resolver,
        )

        try:
            run_result = agent.run(
                self.app,
                {"type": "user_message", "data": {"text": case.input}},
                output_language=self.output_language,
            )
            run_status = run_result.status
            final_data = run_result.data
            if run_result.token_usage:
                self._app_tokens += run_result.token_usage
        except Exception as exc:
            return CaseResult(
                case_name=case.name,
                input=case.input,
                run_status="error",
                phase_results=[],
                final_result=None,
                error=str(exc),
            )

        stored = agent.phase_artifacts
        events_path = str(agent.events_path) if agent.events_path else None

        phase_results: list[PhaseEvalResult] = []
        final_result: PhaseEvalResult | None = None

        # Map phase_name → last primary-type artifact data (for cross-phase checks)
        phase_artifact_data: dict[str, dict] = {}

        for pc in case.phase_criteria:
            if pc.phase is None:
                artifact = {"type": self.app.final_output_name, "data": final_data}
                schema_results = _evaluate_schema(artifact, pc.schema)
                if pc.criteria:
                    criteria_results, judge_usage = judge_artifact(
                        self.judge_model, artifact, [c.text for c in pc.criteria],
                        context=f"final output of '{self.app.name}'",
                        file_contents=_collect_file_contents(artifact),
                    )
                    self._judge_tokens += judge_usage
                else:
                    criteria_results = []
                for cr, qc in zip(criteria_results, pc.criteria):
                    cr.tag = qc.tag
                final_result = PhaseEvalResult(
                    phase="final",
                    visit=1,
                    artifact_type=self.app.final_output_name,
                    schema_results=schema_results,
                    criteria=criteria_results,
                )
                phase_artifact_data["final"] = final_data
                if self.on_phase_judged:
                    self.on_phase_judged(case.name, final_result)
            else:
                phase_artifacts = [a for a in stored if a["phase"] == pc.phase]
                if not phase_artifacts:
                    continue

                primary_type = phase_artifacts[0]["artifact"].get("type", "")
                same_type = [a for a in phase_artifacts
                             if a["artifact"].get("type") == primary_type]
                last = (same_type[-1] if same_type else phase_artifacts[-1])["artifact"]

                schema_results = _evaluate_schema(last, pc.schema)
                if pc.criteria:
                    criteria_results, judge_usage = judge_artifact(
                        self.judge_model, last, [c.text for c in pc.criteria],
                        context=f"phase '{pc.phase}' of '{self.app.name}'",
                        file_contents=_collect_file_contents(last),
                    )
                    self._judge_tokens += judge_usage
                else:
                    criteria_results = []
                for cr, qc in zip(criteria_results, pc.criteria):
                    cr.tag = qc.tag

                pr = PhaseEvalResult(
                    phase=pc.phase,
                    visit=len(phase_artifacts),
                    artifact_type=last.get("type", "unknown"),
                    schema_results=schema_results,
                    criteria=criteria_results,
                )
                phase_artifact_data[pc.phase] = last.get("data", {})
                phase_results.append(pr)
                if self.on_phase_judged:
                    self.on_phase_judged(case.name, pr)

        cross_phase_results = _evaluate_cross_phase(phase_artifact_data, case.cross_phase)

        return CaseResult(
            case_name=case.name,
            input=case.input,
            run_status=run_status,
            phase_results=phase_results,
            final_result=final_result,
            cross_phase_results=cross_phase_results,
            run_events_path=events_path,
        )
