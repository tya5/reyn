"""
PreprocessorExecutor: OS-owned deterministic execution of Phase preprocessor chains.

Runs before the Phase's primary LLM call. Enriches the input artifact in place
(all steps use the enrich model — original data is preserved).

Step semantics:
  validate  — validates artifact["data"] against step.schema_; aborts on failure
  run_app   — invokes a pre-loaded sub-app; places final_output at step.into
  iterate   — maps run_app over each element of step.over; collects into step.into
"""
from __future__ import annotations
import copy
import jsonschema
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .pricing import TokenUsage
from .sub_app_runner import invoke_sub_app

if TYPE_CHECKING:
    from .models import App, Phase, PreprocessorStep
    from .events import EventLog
    from .model_resolver import ModelResolver


class PreprocessorError(RuntimeError):
    pass


def _get_at_path(obj: Any, path: str) -> Any:
    """Walk a runtime dict via dot-notation."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise PreprocessorError(
                f"Path '{path}': segment '{part}' not found "
                f"(available: {list(cur.keys()) if isinstance(cur, dict) else type(cur).__name__})"
            )
        cur = cur[part]
    return cur


def _set_at_path(obj: dict, path: str, value: Any) -> None:
    """Set a value at a dot-path in a runtime dict (mutates obj)."""
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        if part not in cur:
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


class PreprocessorExecutor:
    def __init__(
        self,
        app: "App",
        model: str,
        events: "EventLog",
        subscribers: list,
        resolver: "ModelResolver",
        state_dir: str | Path,
        max_phase_visits: int = 25,
    ) -> None:
        self._app = app
        self._model = model
        self._events = events
        self._subscribers = subscribers
        self._resolver = resolver
        self._state_dir = Path(state_dir)
        self._max_phase_visits = max_phase_visits

    def run(
        self, phase: "Phase", artifact: dict, output_language: str,
    ) -> tuple[dict, TokenUsage]:
        """Apply all preprocessor steps; return (enriched_artifact, accumulated_token_usage)."""
        if not phase.preprocessor:
            return artifact, TokenUsage()

        result = copy.deepcopy(artifact)
        total_usage = TokenUsage()

        for i, step in enumerate(phase.preprocessor):
            self._events.emit(
                "preprocessor_step_started",
                phase=phase.name, step_index=i, step_type=step.type,
            )
            try:
                result, step_usage = self._apply_step(
                    step, result, i, phase.name, output_language
                )
                total_usage += step_usage
            except PreprocessorError:
                self._events.emit(
                    "preprocessor_step_failed",
                    phase=phase.name, step_index=i, step_type=step.type,
                )
                raise
            except Exception as exc:
                self._events.emit(
                    "preprocessor_step_failed",
                    phase=phase.name, step_index=i, step_type=step.type, error=str(exc),
                )
                raise PreprocessorError(
                    f"Phase '{phase.name}' preprocessor step[{i}] ({step.type}): {exc}"
                ) from exc

            self._events.emit(
                "preprocessor_step_completed",
                phase=phase.name, step_index=i, step_type=step.type,
            )

        return result, total_usage

    # ── Step dispatch ─────────────────────────────────────────────────────────

    def _apply_step(
        self, step: "PreprocessorStep", artifact: dict, index: int,
        phase_name: str, output_language: str,
    ) -> tuple[dict, TokenUsage]:
        from .models import RunAppStep, IterateStep, ValidateStep
        if isinstance(step, ValidateStep):
            return self._apply_validate(step, artifact, index, phase_name)
        if isinstance(step, RunAppStep):
            return self._apply_run_app(step, artifact, index, phase_name, output_language)
        if isinstance(step, IterateStep):
            return self._apply_iterate(step, artifact, index, phase_name, output_language)
        raise PreprocessorError(f"Unknown step type: {type(step)}")

    # ── validate ──────────────────────────────────────────────────────────────

    def _apply_validate(
        self, step: Any, artifact: dict, index: int, phase_name: str,
    ) -> tuple[dict, TokenUsage]:
        data = artifact.get("data", {})
        validator = jsonschema.Draft7Validator(step.schema_)
        errors = sorted(validator.iter_errors(data), key=str)
        if errors:
            messages = [e.message for e in errors[:5]]
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] validate: "
                f"artifact data failed schema validation: {'; '.join(messages)}"
            )
        return artifact, TokenUsage()

    # ── run_app ───────────────────────────────────────────────────────────────

    def _apply_run_app(
        self, step: Any, artifact: dict, index: int,
        phase_name: str, output_language: str,
    ) -> tuple[dict, TokenUsage]:
        sub_app = self._app.preprocessor_sub_apps.get(step.app)
        if sub_app is None:
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}]: "
                f"sub-app '{step.app}' not in preprocessor_sub_apps"
            )
        state_dir = self._state_dir / "preprocessor" / phase_name / f"{index}_{step.app}"
        self._events.emit("run_app_started", app=step.app, state_dir=str(state_dir))
        result = invoke_sub_app(
            sub_app, artifact,
            model=self._model,
            state_dir=state_dir,
            subscribers=self._subscribers,
            resolver=self._resolver,
            output_language=output_language,
            max_phase_visits=self._max_phase_visits,
        )
        self._events.emit(
            "run_app_completed", app=step.app, status=result.status,
            prompt_tokens=result.token_usage.prompt_tokens if result.token_usage else None,
            completion_tokens=result.token_usage.completion_tokens if result.token_usage else None,
        )
        if not result.ok:
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] run_app '{step.app}': "
                f"sub-app finished with status '{result.status}'"
            )
        enriched = copy.deepcopy(artifact)
        _set_at_path(enriched, step.into, result.data)
        return enriched, result.token_usage or TokenUsage()

    # ── iterate ───────────────────────────────────────────────────────────────

    def _apply_iterate(
        self, step: Any, artifact: dict, index: int,
        phase_name: str, output_language: str,
    ) -> tuple[dict, TokenUsage]:
        from .models import RunAppStep
        if not isinstance(step.apply, RunAppStep):
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] iterate.apply: "
                "only run_app is supported"
            )

        items = _get_at_path(artifact, step.over)
        if not isinstance(items, list):
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] iterate: "
                f"'over' path '{step.over}' is not a list (got {type(items).__name__})"
            )

        sub_app = self._app.preprocessor_sub_apps.get(step.apply.app)
        if sub_app is None:
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] iterate.apply: "
                f"sub-app '{step.apply.app}' not in preprocessor_sub_apps"
            )

        entry_type = sub_app.phases[sub_app.entry_phase].input_schema_name
        collected: list[Any] = []
        total_usage = TokenUsage()

        for j, item in enumerate(items):
            # Heuristic: if the element already has {"type": ..., "data": ...} keys, treat it as a
            # pre-wrapped artifact and pass it through as-is. Otherwise wrap it using the sub-app's
            # entry schema name.
            # Limitation: domain dicts that happen to have both "type" and "data" keys will be
            # misidentified as pre-wrapped artifacts. For eval's phase_artifact shape this is safe.
            # Future: add `iterate.wrap: auto|explicit|never` to let the DSL author opt out.
            if isinstance(item, dict) and "type" in item and "data" in item:
                item_artifact = item
            else:
                item_artifact = {
                    "type": entry_type,
                    "data": item if isinstance(item, dict) else {"value": item},
                }

            state_dir = (
                self._state_dir / "preprocessor" / phase_name
                / f"{index}_{step.apply.app}" / str(j)
            )
            self._events.emit(
                "run_app_started", app=step.apply.app,
                state_dir=str(state_dir), iterate_index=j,
            )
            result = invoke_sub_app(
                sub_app, item_artifact,
                model=self._model,
                state_dir=state_dir,
                subscribers=self._subscribers,
                resolver=self._resolver,
                output_language=output_language,
                max_phase_visits=self._max_phase_visits,
            )
            if result.token_usage:
                total_usage += result.token_usage
            self._events.emit(
                "run_app_completed", app=step.apply.app, status=result.status,
                iterate_index=j,
                prompt_tokens=result.token_usage.prompt_tokens if result.token_usage else None,
                completion_tokens=result.token_usage.completion_tokens if result.token_usage else None,
            )

            if not result.ok:
                if step.on_error == "fail":
                    raise PreprocessorError(
                        f"Phase '{phase_name}' preprocessor step[{index}] iterate "
                        f"item[{j}]: sub-app '{step.apply.app}' failed with "
                        f"status '{result.status}'"
                    )
                self._events.emit(
                    "preprocessor_iterate_item_skipped",
                    phase=phase_name, step_index=index, item_index=j,
                    reason=f"status={result.status}",
                )
                continue

            collected.append(result.data)

        enriched = copy.deepcopy(artifact)
        _set_at_path(enriched, step.into, collected)
        return enriched, total_usage
