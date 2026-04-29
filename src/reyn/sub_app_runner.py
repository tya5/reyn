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

from .pricing import TokenUsage

if TYPE_CHECKING:
    from .models import App
    from .model_resolver import ModelResolver


@dataclass
class SubAppResult:
    data: dict
    token_usage: TokenUsage | None
    status: str
    phase_artifacts: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "finished"


def invoke_sub_app(
    sub_app: "App",
    input_artifact: dict,
    *,
    model: str,
    state_dir: str | Path,
    subscribers: list,
    resolver: "ModelResolver",
    output_language: str = "ja",
) -> SubAppResult:
    """Run a sub-app and return a SubAppResult.

    Callers are responsible for event emission around this call and for
    accumulating token_usage into their own counter.
    """
    from .agent import Agent

    agent = Agent(
        model=model,
        state_dir=str(state_dir),
        strict=False,
        subscribers=subscribers,
        resolver=resolver,
    )
    run_result = agent.run(sub_app, input_artifact, output_language=output_language)
    return SubAppResult(
        data=run_result.data,
        token_usage=run_result.token_usage,
        status=run_result.status,
        phase_artifacts=agent.phase_artifacts,
    )
