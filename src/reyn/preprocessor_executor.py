"""
PreprocessorExecutor: OS-owned deterministic execution of Phase preprocessor chains.

Runs before the Phase's primary LLM call. Enriches the input artifact in place
(all steps use the enrich model — original data is preserved).

Step semantics:
  validate   — validates artifact["data"] against step.schema_; aborts on failure
  run_skill  — invokes a pre-loaded sub-skill; places final_output at step.into
  iterate    — maps run_skill over each element of step.over; collects into step.into
  lint_plan  — runs deterministic structural checks on a plan dict at step.over;
               places list of issue strings at step.into (does NOT abort)
  python     — invokes a user Python function in a sandboxed subprocess
               (pure | trusted mode); places return value at step.into and
               validates it against the declared output_schema
"""
from __future__ import annotations
import asyncio
import copy
import jsonschema
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .pricing import TokenUsage
from .python_runner import PythonRunner, PythonStepError
from .sub_skill_runner import invoke_sub_skill

if TYPE_CHECKING:
    from .models import Skill, Phase, PreprocessorStep
    from .events import EventLog
    from .model_resolver import ModelResolver
    from .permissions import PermissionResolver


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
        skill: "Skill",
        model: str,
        events: "EventLog",
        subscribers: list,
        resolver: "ModelResolver",
        max_phase_visits: int = 25,
        permission_resolver: "PermissionResolver | None" = None,
        python_runner: PythonRunner | None = None,
        python_allowed_modules: list[str] | None = None,
    ) -> None:
        self._skill = skill
        self._model = model
        self._events = events
        self._subscribers = subscribers
        self._resolver = resolver
        self._state_dir = Path(".reyn")
        self._max_phase_visits = max_phase_visits
        self._perm = permission_resolver
        self._python_runner = python_runner or PythonRunner()
        self._python_allowed_modules = list(python_allowed_modules or [])

    async def run(
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
                result, step_usage = await self._apply_step(
                    step, result, i, phase, output_language
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

    async def _apply_step(
        self, step: "PreprocessorStep", artifact: dict, index: int,
        phase: "Phase", output_language: str,
    ) -> tuple[dict, TokenUsage]:
        from .models import RunSkillStep, IterateStep, ValidateStep, LintPlanStep, PythonStep, FileReadStep
        phase_name = phase.name
        if isinstance(step, ValidateStep):
            return self._apply_validate(step, artifact, index, phase_name)
        if isinstance(step, RunSkillStep):
            return await self._apply_run_skill(step, artifact, index, phase_name, output_language)
        if isinstance(step, IterateStep):
            return await self._apply_iterate(step, artifact, index, phase_name, output_language)
        if isinstance(step, LintPlanStep):
            return self._apply_lint_plan(step, artifact, index, phase_name)
        if isinstance(step, PythonStep):
            return await self._apply_python(step, artifact, index, phase)
        if isinstance(step, FileReadStep):
            return self._apply_file_read(step, artifact, index, phase_name)
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

    # ── run_skill ───────────────────────────────────────────────────────────────

    async def _apply_run_skill(
        self, step: Any, artifact: dict, index: int,
        phase_name: str, output_language: str,
    ) -> tuple[dict, TokenUsage]:
        sub_app = self._skill.preprocessor_sub_skills.get(step.skill)
        if sub_app is None:
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}]: "
                f"sub-skill '{step.skill}' not in preprocessor_sub_skills"
            )
        sub_state_dir = self._state_dir / "preprocessor" / phase_name / f"{index}_{step.skill}"
        self._events.emit("run_skill_started", app=step.skill, state_dir=str(sub_state_dir))
        result = await invoke_sub_skill(
            sub_app, artifact,
            model=self._model,
            subscribers=self._subscribers,
            resolver=self._resolver,
            output_language=output_language,
            max_phase_visits=self._max_phase_visits,
        )
        self._events.emit(
            "run_skill_completed", app=step.skill, status=result.status,
            prompt_tokens=result.token_usage.prompt_tokens if result.token_usage else None,
            completion_tokens=result.token_usage.completion_tokens if result.token_usage else None,
        )
        if not result.ok:
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] run_skill '{step.skill}': "
                f"sub-skill finished with status '{result.status}'"
            )
        enriched = copy.deepcopy(artifact)
        _set_at_path(enriched, step.into, result.data)
        return enriched, result.token_usage or TokenUsage()

    # ── iterate ───────────────────────────────────────────────────────────────

    async def _apply_iterate(
        self, step: Any, artifact: dict, index: int,
        phase_name: str, output_language: str,
    ) -> tuple[dict, TokenUsage]:
        from .models import RunSkillStep
        if not isinstance(step.apply, RunSkillStep):
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] iterate.apply: "
                "only run_skill is supported"
            )

        items = _get_at_path(artifact, step.over)
        if not isinstance(items, list):
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] iterate: "
                f"'over' path '{step.over}' is not a list (got {type(items).__name__})"
            )

        sub_app = self._skill.preprocessor_sub_skills.get(step.apply.skill)
        if sub_app is None:
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] iterate.apply: "
                f"sub-skill '{step.apply.skill}' not in preprocessor_sub_skills"
            )

        entry_type = sub_app.phases[sub_app.entry_phase].input_schema_name
        collected: list[Any] = []
        total_usage = TokenUsage()

        for j, item in enumerate(items):
            # Heuristic: if the element already has {"type": ..., "data": ...} keys, treat it as a
            # pre-wrapped artifact and pass it through as-is. Otherwise wrap it using the sub-skill's
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

            sub_state_dir = (
                self._state_dir / "preprocessor" / phase_name
                / f"{index}_{step.apply.skill}" / str(j)
            )
            self._events.emit(
                "run_skill_started", app=step.apply.skill,
                state_dir=str(sub_state_dir), iterate_index=j,
            )
            result = await invoke_sub_skill(
                sub_app, item_artifact,
                model=self._model,
                subscribers=self._subscribers,
                resolver=self._resolver,
                output_language=output_language,
                max_phase_visits=self._max_phase_visits,
            )
            if result.token_usage:
                total_usage += result.token_usage
            self._events.emit(
                "run_skill_completed", app=step.apply.skill, status=result.status,
                iterate_index=j,
                prompt_tokens=result.token_usage.prompt_tokens if result.token_usage else None,
                completion_tokens=result.token_usage.completion_tokens if result.token_usage else None,
            )

            if not result.ok:
                if step.on_error == "fail":
                    raise PreprocessorError(
                        f"Phase '{phase_name}' preprocessor step[{index}] iterate "
                        f"item[{j}]: sub-skill '{step.apply.skill}' failed with "
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

    # ── lint_plan ─────────────────────────────────────────────────────────────

    def _apply_lint_plan(
        self, step: Any, artifact: dict, index: int, phase_name: str,
    ) -> tuple[dict, TokenUsage]:
        from .compiler.linter import lint_plan
        plan = _get_at_path(artifact, step.over)
        if not isinstance(plan, dict):
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] lint_plan: "
                f"'over' path '{step.over}' must point to a dict (got {type(plan).__name__})"
            )
        issues = lint_plan(plan)
        self._events.emit(
            "lint_plan_completed",
            phase=phase_name, step_index=index, issue_count=len(issues),
        )
        enriched = copy.deepcopy(artifact)
        _set_at_path(enriched, step.into, issues)
        return enriched, TokenUsage()

    # ── file_read ─────────────────────────────────────────────────────────────

    def _apply_file_read(
        self, step: Any, artifact: dict, index: int, phase_name: str,
    ) -> tuple[dict, TokenUsage]:
        if step.bases is not None:
            bases = list(step.bases)
        else:
            bases = _get_at_path(artifact, step.bases_from)
            if not isinstance(bases, list):
                raise PreprocessorError(
                    f"Phase '{phase_name}' preprocessor step[{index}] file_read: "
                    f"bases_from path '{step.bases_from}' is not a list "
                    f"(got {type(bases).__name__})"
                )

        results: list[dict] = []
        for base in bases:
            base_path = Path(str(base)).expanduser()
            file_path = base_path / step.filename
            entry = {"base": str(base), "file": step.filename}
            try:
                raw = file_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                if step.on_error == "fail":
                    raise PreprocessorError(
                        f"Phase '{phase_name}' preprocessor step[{index}] file_read: "
                        f"file not found: {file_path}"
                    )
                if step.on_error == "skip":
                    continue
                # "empty"
                entry["content"] = ""
                results.append(entry)
                continue
            except OSError as exc:
                if step.on_error == "fail":
                    raise PreprocessorError(
                        f"Phase '{phase_name}' preprocessor step[{index}] file_read: "
                        f"read failed for {file_path}: {exc}"
                    ) from exc
                if step.on_error == "skip":
                    continue
                entry["content"] = ""
                results.append(entry)
                continue

            if step.format == "text":
                entry["content"] = raw
            elif step.format == "json":
                import json as _json
                try:
                    entry["content"] = _json.loads(raw) if raw.strip() else None
                except _json.JSONDecodeError as exc:
                    if step.on_error == "fail":
                        raise PreprocessorError(
                            f"Phase '{phase_name}' preprocessor step[{index}] file_read: "
                            f"JSON parse failed for {file_path}: {exc}"
                        ) from exc
                    entry["content"] = None
            elif step.format == "yaml":
                import yaml as _yaml
                try:
                    entry["content"] = _yaml.safe_load(raw) if raw.strip() else None
                except _yaml.YAMLError as exc:
                    if step.on_error == "fail":
                        raise PreprocessorError(
                            f"Phase '{phase_name}' preprocessor step[{index}] file_read: "
                            f"YAML parse failed for {file_path}: {exc}"
                        ) from exc
                    entry["content"] = None
            results.append(entry)

        self._events.emit(
            "file_read_completed",
            phase=phase_name, step_index=index,
            base_count=len(bases), hit_count=len(results),
        )
        enriched = copy.deepcopy(artifact)
        _set_at_path(enriched, step.into, results)
        return enriched, TokenUsage()

    # ── python ────────────────────────────────────────────────────────────────

    async def _apply_python(
        self, step: Any, artifact: dict, index: int, phase: "Phase",
    ) -> tuple[dict, TokenUsage]:
        phase_name = phase.name

        # Resolve permission for this (module, function). Without a resolver
        # (e.g. unit tests), default to a permissive pure-mode entry.
        if self._perm is not None:
            try:
                perm = self._perm.require_python(
                    phase.permissions, step.module, step.function,
                    skill_name=self._skill.name,
                )
            except PermissionError as exc:
                raise PreprocessorError(
                    f"Phase '{phase_name}' preprocessor step[{index}] python "
                    f"{step.module}:{step.function}: {exc}"
                ) from exc
        else:
            from .permissions import PythonPermission
            perm = PythonPermission(module=step.module, function=step.function)

        if not self._skill.skill_dir:
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] python: "
                f"skill_dir is unknown (skill was not loaded from disk); "
                f"cannot resolve {step.module!r}"
            )

        self._events.emit(
            "python_step_started",
            phase=phase_name, step_index=index,
            module=step.module, function=step.function, mode=perm.mode,
        )

        try:
            result = await asyncio.to_thread(
                self._python_runner.run,
                skill_dir=Path(self._skill.skill_dir),
                module=step.module,
                function=step.function,
                mode=perm.mode,
                artifact=artifact,
                timeout=perm.timeout,
                allowed_modules=self._python_allowed_modules,
            )
        except PythonStepError as exc:
            self._events.emit(
                "python_step_failed",
                phase=phase_name, step_index=index,
                module=step.module, function=step.function,
                kind=exc.kind, error=str(exc),
            )
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] python "
                f"{step.module}:{step.function}: {exc}"
            ) from exc

        # Validate the function's actual return value against the declared schema.
        try:
            jsonschema.Draft7Validator(step.output_schema).validate(result)
        except jsonschema.ValidationError as exc:
            self._events.emit(
                "python_step_failed",
                phase=phase_name, step_index=index,
                module=step.module, function=step.function,
                kind="OutputSchemaViolation", error=exc.message,
            )
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] python "
                f"{step.module}:{step.function} return value did not match "
                f"output_schema: {exc.message}"
            ) from exc

        self._events.emit(
            "python_step_completed",
            phase=phase_name, step_index=index,
            module=step.module, function=step.function,
        )

        enriched = copy.deepcopy(artifact)
        _set_at_path(enriched, step.into, result)
        return enriched, TokenUsage()
