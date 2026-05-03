"""
PostprocessorExecutor: OS-owned deterministic execution of Skill postprocessor chains.

Runs once at skill finish, after the LLM's finish artifact has been captured
and before the caller receives the result. Transforms the LLM-contract artifact
(conforming to skill.final_output_schema) into the caller-contract artifact
(conforming to skill.postprocessor.output_schema).

Step semantics are identical to PreprocessorExecutor — the step set, on_error
policy, op dispatch, and permission gate are shared via delegation. The only
differences are the fire position (skill finish, not phase entry) and the
schema sources (skill-level, not phase-level).
"""
from __future__ import annotations

import copy
import jsonschema
from dataclasses import dataclass
from typing import TYPE_CHECKING

from reyn.llm.pricing import TokenUsage
from reyn.kernel.preprocessor_executor import PreprocessorExecutor, PreprocessorError

if TYPE_CHECKING:
    from reyn.schemas.models import Skill, PreprocessorStep
    from reyn.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.permissions.permissions import PermissionResolver
    from reyn.python_runner import PythonRunner
    from reyn.user_intervention import InterventionBus
    from reyn.workspace.workspace import Workspace


class PostprocessorError(RuntimeError):
    pass


# ── Synthetic phase-like scope ────────────────────────────────────────────────

@dataclass
class _PostprocessorScope:
    """Minimal Phase-like object satisfying PreprocessorExecutor's phase parameter.

    PreprocessorExecutor._build_op_ctx pulls:
      - phase.name           → used as `preprocessor_phase_name` and in event fields
      - phase.preprocessor   → used to check non-empty and iterate steps

    _apply_step also uses phase.name (via phase_name = phase.name local) for
    error messages. We expose exactly those fields.
    """
    name: str
    preprocessor: "list[PreprocessorStep]"


_POST_PHASE_NAME = "__post__"


class PostprocessorExecutor:
    """Runs a Skill.postprocessor block at skill finish.

    Mirrors PreprocessorExecutor but at skill scope: takes the LLM's finish
    artifact (conforming to skill.final_output_schema) and produces the
    caller-facing artifact (conforming to skill.postprocessor.output_schema).

    The step-application logic is shared with PreprocessorExecutor via
    delegation — step semantics (validate/run_op/iterate/lint_plan/python),
    on_error policy, and op dispatch context are identical.

    Events emitted:
      postprocessor_step_started   — before each step
      postprocessor_step_completed — after each successful step
      postprocessor_step_failed    — on step failure
    """

    def __init__(
        self,
        *,
        skill: "Skill",
        workspace: "Workspace",
        events: "EventLog",
        model: str,
        resolver: "ModelResolver",
        subscribers: list,
        permission_resolver: "PermissionResolver | None" = None,
        intervention_bus: "InterventionBus | None" = None,
        python_runner: "PythonRunner | None" = None,
        python_allowed_modules: list[str] | None = None,
        max_phase_visits: int = 0,
        caller: str = "direct",
    ) -> None:
        self._skill = skill
        self._events = events
        # Delegate all step execution to a PreprocessorExecutor instance.
        # It is constructed identically to the runtime's preprocessor — the
        # synthetic scope below adapts the call surface.
        self._delegate = PreprocessorExecutor(
            skill=skill,
            workspace=workspace,
            model=model,
            events=events,
            subscribers=subscribers,
            resolver=resolver,
            max_phase_visits=max_phase_visits,
            permission_resolver=permission_resolver,
            intervention_bus=intervention_bus,
            python_runner=python_runner,
            python_allowed_modules=python_allowed_modules,
            caller=caller,
        )

    async def run(
        self, finish_artifact: dict, output_language: str,
    ) -> tuple[dict, TokenUsage]:
        """Apply skill.postprocessor.steps; return (caller_artifact, usage).

        If skill.postprocessor is None, returns finish_artifact unchanged.

        On success the returned artifact conforms to
        skill.postprocessor.output_schema; if the final validation fails a
        PostprocessorError is raised.
        """
        postprocessor = self._skill.postprocessor
        if postprocessor is None:
            return finish_artifact, TokenUsage()

        # Build a synthetic Phase-like scope so the PreprocessorExecutor
        # delegate can execute steps without knowing it's in postprocessor
        # context. The reserved name "__post__" appears in events / error
        # messages and is documented as a pseudo-phase sentinel.
        scope = _PostprocessorScope(
            name=_POST_PHASE_NAME,
            preprocessor=postprocessor.steps,
        )

        result = copy.deepcopy(finish_artifact)
        total_usage = TokenUsage()

        for i, step in enumerate(postprocessor.steps):
            self._events.emit(
                "postprocessor_step_started",
                step_index=i, step_type=step.type,
            )
            try:
                result, step_usage = await self._delegate._apply_step(
                    step, result, i, scope, output_language,  # type: ignore[arg-type]
                )
                total_usage += step_usage
            except (PreprocessorError, Exception) as exc:
                self._events.emit(
                    "postprocessor_step_failed",
                    step_index=i, step_type=step.type, error=str(exc),
                )
                raise PostprocessorError(
                    f"Postprocessor step[{i}] ({step.type}): {exc}"
                ) from exc

            self._events.emit(
                "postprocessor_step_completed",
                step_index=i, step_type=step.type,
            )

        # Final output validation against postprocessor.output_schema.
        data = result.get("data", {})
        validator = jsonschema.Draft7Validator(postprocessor.output_schema)
        errors = sorted(validator.iter_errors(data), key=str)
        if errors:
            messages = [e.message for e in errors[:5]]
            raise PostprocessorError(
                f"Postprocessor output failed schema validation "
                f"(output_schema): {'; '.join(messages)}"
            )

        return result, total_usage
