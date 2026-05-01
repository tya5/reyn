"""
PreprocessorExecutor: OS-owned deterministic execution of Phase preprocessor chains.

Runs once at phase entry (and on rollback) before the LLM call. Enriches the
input artifact via a chain of steps; the LLM sees the resulting object.

Step semantics:
  run_op     — invoke any ControlIROp via op_runtime (file, run_skill, web_*,
               shell, lint, mcp). The generic preprocessor primitive.
  validate   — validates artifact["data"] against step.schema_; aborts on failure
  iterate    — maps a sub-step (RunSkillStep | RunOpStep) over each element of
               step.over; collects into step.into
  lint_plan  — runs deterministic structural checks on a plan dict at step.over;
               places list of issue strings at step.into (does NOT abort)
  python     — invokes a user Python function in a sandboxed subprocess
               (pure | trusted mode); places return value at step.into and
               validates it against the declared output_schema
  run_skill  — sugar for run_op{op=run_skill_iro_op}; passes the calling
               artifact as input. Retained for back-compat.
  file_read  — sugar for iterate(bases) × run_op{op=file/read} with format
               parsing. Retained for back-compat.
"""
from __future__ import annotations
import asyncio
import copy
import jsonschema
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .pricing import TokenUsage
from .python_runner import PythonRunner, PythonStepError

if TYPE_CHECKING:
    from .models import Skill, Phase, PreprocessorStep
    from .events import EventLog
    from .model_resolver import ModelResolver
    from .permissions import PermissionResolver
    from .workspace import Workspace


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


def _extract_usage(op_result: dict) -> TokenUsage:
    """Pull token usage out of a run_skill op_runtime result if present."""
    usage = op_result.get("_token_usage")
    return usage if usage else TokenUsage()


_INTERNAL_FIELDS = ("_token_usage",)


def _strip_internal(op_result: dict) -> dict:
    """Drop op_runtime-internal fields before binding to enriched artifact."""
    return {k: v for k, v in op_result.items() if k not in _INTERNAL_FIELDS}


class PreprocessorExecutor:
    def __init__(
        self,
        skill: "Skill",
        workspace: "Workspace",
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
        self._workspace = workspace
        self._model = model
        self._events = events
        self._subscribers = subscribers
        self._resolver = resolver
        self._max_phase_visits = max_phase_visits
        self._perm = permission_resolver
        self._python_runner = python_runner or PythonRunner()
        self._python_allowed_modules = list(python_allowed_modules or [])

    def _build_op_ctx(self, phase: "Phase", step_index: int):
        """Construct an OpContext for an op_runtime call from this preprocessor."""
        from .op_runtime.context import OpContext
        return OpContext(
            workspace=self._workspace,
            events=self._events,
            permission_decl=phase.permissions,
            permission_resolver=self._perm,
            skill_name=self._skill.name,
            skill=self._skill,
            model=self._model,
            resolver=self._resolver,
            subscribers=self._subscribers,
            output_language="ja",  # set by caller-supplied param when needed
            max_phase_visits=self._max_phase_visits,
            sub_state_dir_override=None,
            state_dir_strategy="preprocessor",
            preprocessor_phase_name=phase.name,
            preprocessor_step_index=step_index,
            shell_allowed=True,  # gating handled by permission_resolver.require_shell
            mcp_servers={},
            mcp_clients={},
            user_input_fn=None,
            current_phase=phase.name,
        )

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
        from .models import RunSkillStep, IterateStep, ValidateStep, LintPlanStep, PythonStep, FileReadStep, RunOpStep
        phase_name = phase.name
        if isinstance(step, ValidateStep):
            return self._apply_validate(step, artifact, index, phase_name)
        if isinstance(step, RunOpStep):
            return await self._apply_run_op(step, artifact, index, phase, output_language)
        if isinstance(step, RunSkillStep):
            return await self._apply_run_skill(step, artifact, index, phase, output_language)
        if isinstance(step, IterateStep):
            return await self._apply_iterate(step, artifact, index, phase, output_language)
        if isinstance(step, LintPlanStep):
            return self._apply_lint_plan(step, artifact, index, phase_name)
        if isinstance(step, PythonStep):
            return await self._apply_python(step, artifact, index, phase)
        if isinstance(step, FileReadStep):
            return await self._apply_file_read(step, artifact, index, phase, output_language)
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

    # ── run_op (generic op_runtime delegate) ──────────────────────────────────

    async def _apply_run_op(
        self, step: Any, artifact: dict, index: int,
        phase: "Phase", output_language: str,
    ) -> tuple[dict, TokenUsage]:
        from .op_runtime import execute_op
        ctx = self._build_op_ctx(phase, index)
        ctx.output_language = output_language
        op = self._materialize_op(step.op, step.args_from, artifact)
        result = await execute_op(op, ctx, caller="preprocessor")

        status = result.get("status")
        if status in ("error", "denied"):
            if step.on_error == "fail":
                raise PreprocessorError(
                    f"Phase '{phase.name}' preprocessor step[{index}] run_op "
                    f"({op.kind}): {result.get('error') or status}"
                )
            if step.on_error == "skip":
                return artifact, _extract_usage(result)
            # "empty" — bind empty value at into and continue
            if step.into:
                enriched = copy.deepcopy(artifact)
                _set_at_path(enriched, step.into, None)
                return enriched, _extract_usage(result)
            return artifact, _extract_usage(result)

        if step.into:
            enriched = copy.deepcopy(artifact)
            _set_at_path(enriched, step.into, _strip_internal(result))
            return enriched, _extract_usage(result)
        return artifact, _extract_usage(result)

    @staticmethod
    def _materialize_op(op: Any, args_from: dict, artifact: dict):
        """Apply args_from dot-path overrides to a literal op.

        Returns a new op instance with overridden fields. Empty args_from
        returns the op unchanged.
        """
        if not args_from:
            return op
        overrides: dict = {}
        for field, path in args_from.items():
            overrides[field] = _get_at_path(artifact, path)
        return op.model_copy(update=overrides)

    # ── run_skill (sugar — delegates to op_runtime via RunSkillIROp) ──────────

    async def _apply_run_skill(
        self, step: Any, artifact: dict, index: int,
        phase: "Phase", output_language: str,
    ) -> tuple[dict, TokenUsage]:
        from .models import RunSkillIROp
        from .op_runtime import execute_op

        # Preprocessor's run_skill semantics: pass the calling artifact as input.
        op = RunSkillIROp(
            kind="run_skill",
            skill=step.skill,
            input=artifact,
            model=self._model,
            workspace="isolated",
            output_language=output_language,
        )
        ctx = self._build_op_ctx(phase, index)
        ctx.output_language = output_language
        result = await execute_op(op, ctx, caller="preprocessor")

        status = result.get("status")
        if status in ("error", "denied") or not result.get("success", False):
            raise PreprocessorError(
                f"Phase '{phase.name}' preprocessor step[{index}] run_skill '{step.skill}': "
                f"sub-skill finished with status '{status}'"
            )

        enriched = copy.deepcopy(artifact)
        _set_at_path(enriched, step.into, result.get("final_output"))
        return enriched, _extract_usage(result)

    # ── iterate ───────────────────────────────────────────────────────────────

    async def _apply_iterate(
        self, step: Any, artifact: dict, index: int,
        phase: "Phase", output_language: str,
    ) -> tuple[dict, TokenUsage]:
        from .models import RunSkillStep, RunOpStep
        from .op_runtime import execute_op

        phase_name = phase.name
        if not isinstance(step.apply, (RunSkillStep, RunOpStep)):
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] iterate.apply: "
                "only run_skill and run_op are supported"
            )

        items = _get_at_path(artifact, step.over)
        if not isinstance(items, list):
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] iterate: "
                f"'over' path '{step.over}' is not a list (got {type(items).__name__})"
            )

        # For RunSkillStep we wrap items into the sub-skill's entry artifact.
        # For RunOpStep we expose the current item under `_iter` so args_from
        # can reference `_iter.item` (or any sub-path).
        is_run_skill = isinstance(step.apply, RunSkillStep)

        sub_app = None
        entry_type = None
        if is_run_skill:
            sub_app = self._skill.preprocessor_sub_skills.get(step.apply.skill)
            if sub_app is None:
                raise PreprocessorError(
                    f"Phase '{phase_name}' preprocessor step[{index}] iterate.apply: "
                    f"sub-skill '{step.apply.skill}' not in preprocessor_sub_skills"
                )
            entry_type = sub_app.phases[sub_app.entry_phase].input_schema_name

        collected: list[Any] = []
        total_usage = TokenUsage()
        ctx = self._build_op_ctx(phase, index)
        ctx.output_language = output_language

        for j, item in enumerate(items):
            if is_run_skill:
                # Pre-wrap heuristic: domain dicts with both "type" and "data"
                # are passed through; otherwise wrap with the sub-skill's entry type.
                if isinstance(item, dict) and "type" in item and "data" in item:
                    item_artifact = item
                else:
                    item_artifact = {
                        "type": entry_type,
                        "data": item if isinstance(item, dict) else {"value": item},
                    }

                from .models import RunSkillIROp
                op = RunSkillIROp(
                    kind="run_skill",
                    skill=step.apply.skill,
                    input=item_artifact,
                    model=self._model,
                    workspace="isolated",
                    output_language=output_language,
                )
                # Distinct sub-state-dir per iteration
                ctx.preprocessor_step_index = index
                ctx.preprocessor_phase_name = phase_name
                ctx.sub_state_dir_override = str(
                    self._workspace.state_dir / "preprocessor" / phase_name
                    / f"{index}_{step.apply.skill}" / str(j)
                )
                self._events.emit(
                    "preprocessor_iterate_item_started",
                    phase=phase_name, step_index=index, item_index=j,
                )
                result = await execute_op(op, ctx, caller="preprocessor")
                ctx.sub_state_dir_override = None

                if result.get("status") in ("error", "denied") or not result.get("success", False):
                    if step.on_error == "fail":
                        raise PreprocessorError(
                            f"Phase '{phase_name}' preprocessor step[{index}] iterate "
                            f"item[{j}]: sub-skill '{step.apply.skill}' failed with "
                            f"status '{result.get('status')}'"
                        )
                    self._events.emit(
                        "preprocessor_iterate_item_skipped",
                        phase=phase_name, step_index=index, item_index=j,
                        reason=f"status={result.get('status')}",
                    )
                    total_usage += _extract_usage(result)
                    continue

                total_usage += _extract_usage(result)
                collected.append(result.get("final_output"))
            else:
                # RunOpStep apply: build a synthetic artifact that exposes the
                # current item under `_iter`, then materialize the inner op via
                # args_from against this view.
                iter_artifact = copy.deepcopy(artifact)
                iter_artifact["_iter"] = {"item": item, "index": j}
                op_inst = self._materialize_op(step.apply.op, step.apply.args_from, iter_artifact)
                result = await execute_op(op_inst, ctx, caller="preprocessor")

                status = result.get("status")
                if status in ("error", "denied"):
                    if step.on_error == "fail":
                        raise PreprocessorError(
                            f"Phase '{phase_name}' preprocessor step[{index}] iterate "
                            f"item[{j}] run_op ({op_inst.kind}): "
                            f"{result.get('error') or status}"
                        )
                    self._events.emit(
                        "preprocessor_iterate_item_skipped",
                        phase=phase_name, step_index=index, item_index=j,
                        reason=f"status={status}",
                    )
                    total_usage += _extract_usage(result)
                    continue

                total_usage += _extract_usage(result)
                collected.append(_strip_internal(result))

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

    # ── file_read (sugar for `iterate(bases) × file/read` with format parsing) ─

    async def _apply_file_read(
        self, step: Any, artifact: dict, index: int,
        phase: "Phase", output_language: str,
    ) -> tuple[dict, TokenUsage]:
        from .models import FileIROp
        from .op_runtime import execute_op

        phase_name = phase.name
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

        ctx = self._build_op_ctx(phase, index)
        ctx.output_language = output_language
        results: list[dict] = []
        for base in bases:
            base_str = str(Path(str(base)).expanduser())
            file_path = str(Path(base_str) / step.filename)
            entry = {"base": str(base), "file": step.filename}

            op = FileIROp(kind="file", op="read", path=file_path)
            op_result = await execute_op(op, ctx, caller="preprocessor")

            if op_result.get("status") == "not_found":
                if step.on_error == "fail":
                    raise PreprocessorError(
                        f"Phase '{phase_name}' preprocessor step[{index}] file_read: "
                        f"file not found: {file_path}"
                    )
                if step.on_error == "skip":
                    continue
                entry["content"] = ""
                results.append(entry)
                continue

            if op_result.get("status") in ("error", "denied"):
                if step.on_error == "fail":
                    raise PreprocessorError(
                        f"Phase '{phase_name}' preprocessor step[{index}] file_read: "
                        f"read failed for {file_path}: {op_result.get('error')}"
                    )
                if step.on_error == "skip":
                    continue
                entry["content"] = ""
                results.append(entry)
                continue

            raw = op_result.get("content", "")

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
