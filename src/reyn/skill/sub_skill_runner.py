"""
Shared helper for invoking a sub-app from within the OS.

Used by both:
  - ControlIRExecutor (LLM-triggered run_app IR op)
  - PreprocessorExecutor (OS-triggered preprocessor run_app step)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from reyn.llm.pricing import TokenUsage

if TYPE_CHECKING:
    from reyn.schemas.models import Skill
    from reyn.llm.model_resolver import ModelResolver
    from reyn.user_intervention import InterventionBus


@dataclass
class SubSkillResult:
    data: dict
    token_usage: TokenUsage | None
    status: str
    phase_artifacts: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "finished"


async def invoke_sub_skill(
    sub_skill: "Skill",
    input_artifact: dict,
    *,
    model: str,
    subscribers: list,
    resolver: "ModelResolver",
    intervention_bus: "InterventionBus | None" = None,
    output_language: str | None = None,
    max_phase_visits: int = 25,
    caller: str = "direct",
    parent_run_id: str | None = None,
) -> SubSkillResult:
    """Run a sub-app and return a SubSkillResult.

    Callers are responsible for event emission around this call and for
    accumulating token_usage into their own counter.

    `caller` is propagated from the parent so the sub-skill's events land
    under the same `events/<caller>/skill_runs/...` tree (PR20).

    ``parent_run_id`` (R-D13) is recorded on the child run's
    SkillSnapshot so the parent / child tree can be reconstructed by
    ``/skill list`` and resume bookkeeping. ``None`` = top-level
    (preprocessor / standalone invocation).
    """
    from reyn.agent import Agent

    agent = Agent(
        model=model,
        strict=False,
        subscribers=subscribers,
        resolver=resolver,
        intervention_bus=intervention_bus,
        caller=caller,
    )
    run_result = await agent.run(
        sub_skill, input_artifact,
        output_language=output_language,
        parent_run_id=parent_run_id,
    )
    return SubSkillResult(
        data=run_result.data,
        token_usage=run_result.token_usage,
        status=run_result.status,
        phase_artifacts=agent.phase_artifacts,
    )
