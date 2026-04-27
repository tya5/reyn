"""
EvalRunner: orchestrates per-case app execution and per-phase LLM-as-judge evaluation.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, Callable

from .eval_models import (
    EvalSpec, EvalCase, EvalRunResult, CaseResult, PhaseEvalResult,
    CriterionResult, SchemaAssertion, SchemaResult,
    CrossPhaseAssertion, CrossPhaseResult, CostSummary,
)
from .eval_judge import judge_artifact
from .agent import Agent
from .models import App
from .pricing import TokenUsage, estimate_cost


# ── Schema evaluation ─────────────────────────────────────────────────────────

def _get_nested(data: dict, path: str) -> tuple[bool, Any]:
    """Resolve dot-notation path into data dict. Returns (found, value)."""
    parts = path.split(".")
    cur: Any = data
    for part in parts:
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _check_type(value: Any, type_str: str) -> tuple[bool, str]:
    if type_str == "string":
        ok = isinstance(value, str)
        return ok, ("ok" if ok else f"expected string, got {type(value).__name__}")
    if type_str == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return ok, ("ok" if ok else f"expected number, got {type(value).__name__}: {value!r}")
    if type_str == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
        return ok, ("ok" if ok else f"expected integer, got {type(value).__name__}: {value!r}")
    if type_str == "boolean":
        ok = isinstance(value, bool)
        return ok, ("ok" if ok else f"expected boolean, got {type(value).__name__}: {value!r}")
    if type_str == "array":
        ok = isinstance(value, list)
        return ok, ("ok" if ok else f"expected array, got {type(value).__name__}")
    if type_str == "object":
        ok = isinstance(value, dict)
        return ok, ("ok" if ok else f"expected object, got {type(value).__name__}")
    return False, f"unknown type '{type_str}'"


def _check_constraints(value: Any, type_str: str, constraints: dict) -> tuple[bool, str]:
    if not constraints:
        return True, "ok"

    if "range" in constraints:
        lo, hi = constraints["range"]
        if not (lo <= value <= hi):
            return False, f"{value} not in range [{lo}, {hi}]"

    if "min" in constraints:
        mn = constraints["min"]
        target = len(value) if type_str == "array" else value
        if target < mn:
            label = f"array length {len(value)}" if type_str == "array" else str(value)
            return False, f"{label} < min {mn}"

    if "max" in constraints:
        mx = constraints["max"]
        target = len(value) if type_str == "array" else value
        if target > mx:
            label = f"array length {len(value)}" if type_str == "array" else str(value)
            return False, f"{label} > max {mx}"

    if "min_length" in constraints:
        ml = constraints["min_length"]
        if len(value) < ml:
            return False, f"length {len(value)} < min_length {ml}"

    if "max_length" in constraints:
        ml = constraints["max_length"]
        if len(value) > ml:
            return False, f"length {len(value)} > max_length {ml}"

    if "equals" in constraints:
        expected = constraints["equals"]
        if value != expected:
            return False, f"{value!r} != {expected!r}"

    if "contains" in constraints:
        needle = constraints["contains"]
        if isinstance(value, str):
            if needle not in value:
                return False, f"{needle!r} not found in string"
        elif isinstance(value, list):
            if not any(needle in str(item) for item in value):
                return False, f"no element contains {needle!r}"
        else:
            return False, f"contains not applicable to {type(value).__name__}"

    return True, "ok"


def _evaluate_schema(
    artifact: dict,
    assertions: list[SchemaAssertion],
) -> list[SchemaResult]:
    """Deterministically validate artifact data against schema assertions."""
    data = artifact.get("data", {})
    results: list[SchemaResult] = []

    for assertion in assertions:
        found, value = _get_nested(data, assertion.path)
        if not found:
            results.append(SchemaResult(
                assertion=assertion, passed=False,
                reason=f"field '{assertion.path}' not found",
            ))
            continue

        type_ok, type_reason = _check_type(value, assertion.type)
        if not type_ok:
            results.append(SchemaResult(assertion=assertion, passed=False, reason=type_reason))
            continue

        passed, reason = _check_constraints(value, assertion.type, assertion.constraints)
        results.append(SchemaResult(assertion=assertion, passed=passed, reason=reason))

    return results


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
        workspace_dir: str = "./workspace",
        output_language: str = "ja",
        app_subscribers: list[Callable] | None = None,
        on_case_start: Callable[[str], None] | None = None,
        on_phase_judged: Callable[[str, PhaseEvalResult], None] | None = None,
        extra_read_roots: list[str] | None = None,
    ) -> None:
        self.spec = spec
        self.app = app
        self.model = model
        self.judge_model = judge_model
        self.workspace_dir = workspace_dir
        self.output_language = output_language
        self.app_subscribers = app_subscribers or []
        self.on_case_start = on_case_start
        self.on_phase_judged = on_phase_judged
        self.extra_read_roots = extra_read_roots or []
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

        case_workspace = str(
            Path(self.workspace_dir) / "evals" / self.app.name / case.name
        )
        agent = Agent(
            model=self.model,
            workspace_dir=case_workspace,
            subscribers=self.app_subscribers,
            extra_read_roots=self.extra_read_roots,
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

        stored = [
            a for a in agent._runtime.workspace.artifacts
            if a["phase"] != "_input"
        ]
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
