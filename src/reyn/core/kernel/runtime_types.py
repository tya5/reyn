"""Shared exception types + artifact helpers for OSRuntime decomposition.

Extracted to a leaf module to break circular imports between runtime.py
and the layer modules (phase_executor.py, llm_call_recorder.py,
run_orchestrator.py). Other kernel modules import from here; this file
imports nothing from the rest of reyn.core.kernel.

FP-0020 follow-up: surfaces during Component C (PhaseExecutor) extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from reyn.llm.pricing import TokenUsage


class LoopLimitExceededError(Exception):
    """Raised when a phase is entered more than ``safety.loop.max_phase_visits``
    times in one skill run.

    FP-0004: ``hint_config_key`` carries the user-facing config key the
    operator should raise to allow the run to continue. Callers building
    user-visible messages should append ``f"→ Raise {exc.hint_config_key}
    to allow more iterations."``.
    """

    hint_config_key: str = "safety.loop.max_phase_visits"


class PhaseBudgetExceededError(Exception):
    """Raised when a phase exceeds its wall-clock budget
    (``safety.timeout.phase_seconds`` / legacy ``limits.phase.max_wall_seconds``).

    ``hint_config_key`` (FP-0004) names the config knob the operator
    should adjust to allow longer phase wall-clock budgets.
    """

    hint_config_key: str = "safety.timeout.phase_seconds"

    def __init__(self, phase: str, elapsed: float, budget: float) -> None:
        super().__init__(
            f"Phase '{phase}' exceeded wall-clock budget: {elapsed:.2f}s > {budget:.3g}s. "
            f"→ Raise {PhaseBudgetExceededError.hint_config_key} to allow longer phase runs."
        )
        self.phase = phase
        self.elapsed = elapsed
        self.budget = budget


class WorkflowAbortedError(Exception):
    pass


@dataclass
class RunResult:
    """Typed return value of OSRuntime.run() and SkillRuntime.run().

    FP-0005: ``partial_data`` carries the last completed phase's
    artifact (= "what we have so far") when a safety limit aborts the
    run mid-flight. ``data`` is populated only on a clean ``finished``
    status; ``partial_data`` is populated on any non-``finished``
    status where a phase had completed before the abort. Callers
    rendering a stop reason (TUI / `/list` / chat reply) should fall
    back to ``partial_data`` when ``ok`` is False.
    """
    data: dict[str, Any]
    status: Literal["finished", "loop_limit_exceeded", "phase_budget_exceeded", "budget_exceeded"]
    token_usage: TokenUsage | None = None
    cost_usd: float | None = None
    error: str | None = None
    # FP-0005: last completed phase artifact preserved on abort.
    partial_data: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "finished"


def _normalize_artifact(artifact: dict, expected_type: str | None) -> dict:
    _META = frozenset({
        "type", "next_phase", "status", "ops",
        "reason", "confidence", "final_output", "control",
    })
    if isinstance(artifact.get("data"), dict):
        cleaned_data = {k: v for k, v in artifact["data"].items() if k != "type"}
        return {**artifact, "data": cleaned_data}
    t = artifact.get("type")
    if t is None and expected_type and "|" not in expected_type:
        t = expected_type
    data = {k: v for k, v in artifact.items() if k not in _META}
    return {"type": t, "data": data}


def _validate_artifact_structure(artifact: dict, context: str) -> None:
    if "type" not in artifact:
        raise ValueError(f"[{context}] artifact is missing 'type' field")
    if "data" not in artifact:
        raise ValueError(f"[{context}] artifact is missing 'data' field")
    if not isinstance(artifact["data"], dict):
        raise ValueError(
            f"[{context}] artifact['data'] must be a dict, "
            f"got {type(artifact['data']).__name__}"
        )
