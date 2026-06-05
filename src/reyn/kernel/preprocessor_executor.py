"""
PreprocessorExecutor: OS-owned deterministic execution of Phase preprocessor chains.

Runs once at phase entry (and on rollback) before the LLM call. Enriches the
input artifact via a chain of steps; the LLM sees the resulting object.

Step semantics:
  run_op     — invoke any ControlIROp via op_runtime (file, run_skill, web_*,
               shell, lint, mcp). The generic op-execution primitive.
               `args_from` resolves dot-paths in the input artifact at execution
               time; an empty path ("") binds the whole artifact (useful for
               `run_skill.input` when the calling artifact passes through verbatim).
  iterate    — fans the inner `run_op` over each element of step.over; collects
               into step.into. The current item is exposed under `_iter.item`
               for `args_from` resolution.
  validate   — validates artifact["data"] against step.schema_; aborts on failure
  lint_plan  — runs deterministic structural checks on a plan dict at step.over;
               places list of issue strings at step.into (does NOT abort)
  python     — invokes a user Python function in a sandboxed subprocess
               (pure | trusted mode); places return value at step.into and
               validates it against the declared output_schema
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jsonschema

from reyn.llm.pricing import TokenUsage
from reyn.python_runner import PythonRunner, PythonStepError

if TYPE_CHECKING:
    from reyn.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.permissions.permissions import PermissionResolver
    from reyn.sandbox.backend import SandboxBackend
    from reyn.schemas.models import Phase, PreprocessorStep, Skill
    from reyn.secrets.store import ScopedSecretStore
    from reyn.user_intervention import RequestBus
    from reyn.workspace.workspace import Workspace


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
        next_val = cur[part]
        if not isinstance(next_val, dict):
            raise PreprocessorError(
                f"into: path '{path}': segment '{part}' is "
                f"{type(next_val).__name__!r}, not a dict — "
                f"if your python step returns the full payload, "
                f"use 'into: data' instead of a sub-path like 'into: {path}'"
            )
        cur = next_val
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
        intervention_bus: "RequestBus | None" = None,
        python_runner: PythonRunner | None = None,
        python_allowed_modules: list[str] | None = None,
        caller: str = "direct",
        run_id: str | None = None,
        secret_store: "ScopedSecretStore | None" = None,
        sandbox_backend: "SandboxBackend | None" = None,
        agent_sandbox_policy: dict | None = None,
    ) -> None:
        self._skill = skill
        self._workspace = workspace
        self._model = model
        self._events = events
        self._subscribers = subscribers
        self._resolver = resolver
        self._max_phase_visits = max_phase_visits
        self._perm = permission_resolver
        self._intervention_bus = intervention_bus
        self._python_runner = python_runner or PythonRunner()
        self._python_allowed_modules = list(python_allowed_modules or [])
        self._caller = caller
        # FP-0021: run_id of the currently-executing OSRuntime run.
        # Propagated into OpContext so event emit helpers can stamp run scope.
        self._run_id = run_id
        # FP-0016 D: per-skill credential scoping. None = unrestricted
        # (= preserves backward compat for callers that don't supply a store).
        self._secret_store = secret_store
        # FP-0008 #1115 Stage 2: per-run injected exec backend instance, so a
        # preprocessor sandboxed_exec runs in the same (container) target as the
        # rest of the run. None → platform auto-detect (unchanged host behavior).
        self._sandbox_backend = sandbox_backend
        # #1326: agent-level (operator) sandbox policy (reyn.yaml sandbox.policy).
        # The deterministic policy for this run's sandboxed ops + the SandboxLayer
        # of the permission ∩. It is also the source for the postprocessor path
        # (whose _PostprocessorScope is skill-level, not a phase), which is what
        # makes the index write-gate fire there (#1321). None → no agent policy.
        self._agent_sandbox_policy = agent_sandbox_policy

    @property
    def secret_store(self):
        """Read-only accessor for the injected ScopedSecretStore (or None)."""
        return self._secret_store

    def _build_op_ctx(self, phase: "Phase", step_index: int):
        """Construct an OpContext for an op_runtime call from this preprocessor."""
        from reyn.op_runtime.context import OpContext
        return OpContext(
            workspace=self._workspace,
            events=self._events,
            permission_decl=self._skill.permissions,
            permission_resolver=self._perm,
            skill_name=self._skill.name,
            skill=self._skill,
            model=self._model,
            resolver=self._resolver,
            subscribers=self._subscribers,
            output_language=None,  # set by caller-supplied param when needed
            max_phase_visits=self._max_phase_visits,
            sub_state_dir_override=None,
            state_dir_strategy="preprocessor",
            preprocessor_phase_name=phase.name,
            preprocessor_step_index=step_index,
            mcp_servers={},
            mcp_clients={},
            intervention_bus=None,  # ask_user is forbidden in preprocessor
            current_phase=phase.name,
            caller=self._caller,
            # FP-0021: thread the OSRuntime run_id into every OpContext
            # so event emit helpers can stamp the correct run scope.
            run_id=self._run_id,
            # FP-0016 E: pick up agent_id from the EventLog (= populated
            # at session level) so X-Reyn-Agent-Id is added to outgoing
            # MCP HTTP calls dispatched from preprocessor ops.
            agent_id=getattr(self._events, "agent_id", None),
            # FP-0016 D: per-skill credential scoping.
            secret_store=self._secret_store,
            # FP-0008 #1115 Stage 2: per-run injected exec backend instance.
            sandbox_backend=self._sandbox_backend,
            # #1326: the agent-level (operator) sandbox policy governs a
            # preprocessor sandboxed_exec (deterministic; WINS over op fields).
            default_sandbox_policy=self._agent_sandbox_policy,
        )

    async def run(
        self, phase: "Phase", artifact: dict, output_language: str | None,
        skill_input: dict | None = None,
    ) -> tuple[dict, TokenUsage]:
        """Apply all preprocessor steps; return (enriched_artifact, accumulated_token_usage).

        FP-0008 #1115 Stage 0: when ``skill_input`` (the skill's entry-phase
        input artifact, held by the OS at ``run_state.skill_input``) is supplied,
        it is injected at the reserved OS key ``_skill_input`` for the duration
        of the preprocessor chain, then stripped before the enriched artifact is
        returned. This gives deterministic preprocessor steps an OS-served,
        P5-correct view of the entry input (= never LLM-mutated) without a
        workspace ``file.read`` of a base_dir-coupled artifact path. The key is
        generic — every skill has an entry input — so it is P7-clean, analogous
        to the ``_iter`` binding injected by ``_apply_iterate``. Stripping on
        return keeps it out of the LLM-facing frame and the stored
        ``*_preprocessed`` artifact (no bloat for skills that don't consume it).
        """
        if not phase.preprocessor:
            return artifact, TokenUsage()

        result = copy.deepcopy(artifact)
        if skill_input is not None:
            result["_skill_input"] = copy.deepcopy(skill_input)
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

        # FP-0008 #1115 Stage 0: strip the OS-injected entry-input binding so it
        # never leaks into the LLM-facing frame or the stored artifact. Steps
        # that needed it have already derived their values into `data.*`.
        result.pop("_skill_input", None)
        return result, total_usage

    # ── Step dispatch ─────────────────────────────────────────────────────────

    async def _apply_step(
        self, step: "PreprocessorStep", artifact: dict, index: int,
        phase: "Phase", output_language: str | None,
    ) -> tuple[dict, TokenUsage]:
        from reyn.schemas.models import (
            IterateStep,
            LintPlanStep,
            PythonStep,
            RunOpStep,
            ValidateStep,
        )
        phase_name = phase.name
        if isinstance(step, ValidateStep):
            return self._apply_validate(step, artifact, index, phase_name)
        if isinstance(step, RunOpStep):
            return await self._apply_run_op(step, artifact, index, phase, output_language)
        if isinstance(step, IterateStep):
            return await self._apply_iterate(step, artifact, index, phase, output_language)
        if isinstance(step, LintPlanStep):
            return self._apply_lint_plan(step, artifact, index, phase_name)
        if isinstance(step, PythonStep):
            return await self._apply_python(step, artifact, index, phase)
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
        phase: "Phase", output_language: str | None,
    ) -> tuple[dict, TokenUsage]:
        from reyn.op_runtime import execute_op
        ctx = self._build_op_ctx(phase, index)
        ctx.output_language = output_language
        try:
            op = self._materialize_op(step.op, step.args_from, artifact)
        except PreprocessorError as exc:
            # args_from path resolution failed — apply on_error the same way
            # as a runtime op error so optional fields don't crash the preprocessor.
            if step.on_error == "fail":
                raise PreprocessorError(
                    f"Phase '{phase.name}' preprocessor step[{index}] run_op: {exc}"
                ) from exc
            if step.on_error == "skip":
                return artifact, TokenUsage()
            if step.into:
                enriched = copy.deepcopy(artifact)
                _set_at_path(enriched, step.into, None)
                return enriched, TokenUsage()
            return artifact, TokenUsage()
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
        returns the op unchanged. An empty dot-path ("") binds the whole
        artifact — useful for `run_skill.input` when the calling artifact
        should be passed through verbatim.
        """
        if not args_from:
            return op
        overrides: dict = {}
        for field, path in args_from.items():
            if path == "":
                overrides[field] = artifact
            else:
                overrides[field] = _get_at_path(artifact, path)
        return op.model_copy(update=overrides)

    # ── iterate ───────────────────────────────────────────────────────────────

    async def _apply_iterate(
        self, step: Any, artifact: dict, index: int,
        phase: "Phase", output_language: str | None,
    ) -> tuple[dict, TokenUsage]:
        from reyn.op_runtime import execute_op
        from reyn.schemas.models import RunOpStep

        phase_name = phase.name
        if not isinstance(step.apply, RunOpStep):
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] iterate.apply: "
                "only run_op is supported"
            )

        items = _get_at_path(artifact, step.over)
        if not isinstance(items, list):
            raise PreprocessorError(
                f"Phase '{phase_name}' preprocessor step[{index}] iterate: "
                f"'over' path '{step.over}' is not a list (got {type(items).__name__})"
            )

        collected: list[Any] = []
        total_usage = TokenUsage()
        ctx = self._build_op_ctx(phase, index)
        ctx.output_language = output_language

        for j, item in enumerate(items):
            # Expose the current item under `_iter` so the inner op's
            # args_from can reference `_iter.item` (or sub-paths thereof).
            iter_artifact = copy.deepcopy(artifact)
            iter_artifact["_iter"] = {"item": item, "index": j}
            op_inst = self._materialize_op(step.apply.op, step.apply.args_from, iter_artifact)

            # When iterating a run_skill op, give each iteration a distinct
            # sub-state-dir so artifacts don't collide.
            if op_inst.kind == "run_skill":
                ctx.sub_state_dir_override = str(
                    self._workspace.state_dir / "preprocessor" / phase_name
                    / f"{index}_{op_inst.skill}" / str(j)
                )
            self._events.emit(
                "preprocessor_iterate_item_started",
                phase=phase_name, step_index=index, item_index=j,
            )
            result = await execute_op(op_inst, ctx, caller="preprocessor")
            ctx.sub_state_dir_override = None

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
        from reyn.compiler.linter import lint_plan
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

    # ── python ────────────────────────────────────────────────────────────────

    async def _apply_python(
        self, step: Any, artifact: dict, index: int, phase: "Phase",
    ) -> tuple[dict, TokenUsage]:
        phase_name = phase.name

        # Resolve permission for this (module, function). Without a resolver
        # (e.g. unit tests), default to a permissive pure-mode entry.
        if self._perm is not None:
            # B49 W2-S5 fix (2026-05-22): pass intervention_bus as-is; it
            # may be None in non-interactive contexts (= preprocessor /
            # postprocessor python step invoked from a sub-skill run).
            # ``PermissionResolver._approve`` already returns True early
            # when the permission is config-approved (= reyn.yaml
            # ``python.safe: allow``) or session/saved-approved, without
            # touching the bus. The pre-check that raised unconditionally
            # blocked the config-approved path too. ``require_python``'s
            # PermissionError still raises when interactive prompt is
            # genuinely required but the bus is missing — see
            # PermissionResolver._approve / _prompt for the routing.
            try:
                perm = await self._perm.require_python(
                    self._skill.permissions, step.module, step.function,
                    self._intervention_bus,
                    skill_name=self._skill.name,
                )
            except PermissionError as exc:
                raise PreprocessorError(
                    f"Phase '{phase_name}' preprocessor step[{index}] python "
                    f"{step.module}:{step.function}: {exc}"
                ) from exc
        else:
            from reyn.permissions.permissions import PythonPermission
            # When no resolver is configured (e.g. unit tests), look up the
            # declared mode from the skill's permissions block so that skills
            # declaring `mode: unsafe` are not silently downgraded to safe.
            declared_mode = "safe"
            for decl in self._skill.permissions.python:
                if decl.module == step.module and decl.function == step.function:
                    declared_mode = decl.mode
                    break
            perm = PythonPermission(
                module=step.module, function=step.function, mode=declared_mode
            )

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

        # FP-0042: forward the skill's declared file-read / file-write
        # paths into the subprocess so ``reyn.safe.file.*`` calls inside
        # the user step can gate against them. The subprocess-side check
        # is decl-membership, not the full 4-layer ask flow (= those
        # layers already fired at startup_guard time before this step
        # ran).
        #
        # Mirror the parent-side ``_in_default_read_zone`` / ``_in_default_write_zone``
        # behaviour by pre-pending the implicit default zones (CWD for
        # read; ``.reyn/`` + ``reyn/`` under CWD for write). Without this,
        # a safe-mode step that reads a workspace-local file would have
        # to declare every path explicitly, even though the equivalent
        # ``invoke_action(file__read)`` route would be granted by default.
        # Keeping the contract symmetric with the existing permission
        # model.
        #
        # #571 collapse arc Phase 2: ``reyn.safe.file._check_write``
        # additionally enforces a canonical-protected-paths denylist
        # (= ``.reyn/mcp.yaml`` / ``.reyn/cron.yaml`` /
        # ``.reyn/index/sources.yaml``). Those paths are allowed through
        # the broad ``.reyn/`` default zone here, but rejected by the
        # safe.file gate unless the skill ALSO listed the path
        # explicitly in ``permissions.file_write`` (which the bool-axis
        # compat shim does automatically when ``mcp_install`` /
        # ``cron_register`` / ``index_drop`` / ``mcp_drop_server`` is
        # set). The narrow exception preserves the broad ``.reyn/``
        # default for chunkers, cursors, and other workspace scratch
        # state.
        import os as _os
        _cwd = _os.getcwd()
        file_read_paths = [_cwd] + [
            entry["path"] for entry in self._skill.permissions.file_read
            if isinstance(entry, dict) and entry.get("path")
        ]
        file_write_paths = [
            _os.path.join(_cwd, ".reyn"),
            _os.path.join(_cwd, "reyn"),
        ] + [
            entry["path"] for entry in self._skill.permissions.file_write
            if isinstance(entry, dict) and entry.get("path")
        ]
        # #571 collapse arc Phase 3 / Phase 7: forward the skill's
        # declared http.get host allowlist into the subprocess so
        # ``reyn.safe.http.*`` calls gate against it. Wildcard ``"*"``
        # entries are stripped here — the subprocess cannot prompt
        # the operator, so wildcard hosts can only be reached via the
        # ``web_fetch`` op handler (main-process async path). A skill
        # that needs runtime-chosen hosts from a safe-mode python step
        # must instead emit a ``web_fetch`` Control IR op so the prompt
        # can fire.
        http_hosts = [
            entry["host"] for entry in self._skill.permissions.http_get
            if isinstance(entry, dict)
            and entry.get("host")
            and entry.get("host") != "*"
        ]

        # #1199 S3.4 Part1: forward the sandbox write_paths cap so a safe-mode
        # step's host-direct index write (reyn.safe.embed_index →
        # SqliteIndexBackend) self-gates against it. None when no policy (or no
        # write_paths) → no cap (unchanged).
        # #1326: the source is the agent-level (operator) policy. The index write
        # runs in the POSTPROCESSOR path, whose _PostprocessorScope is skill-level
        # (not a phase) — so the agent-level source is what makes this write-gate
        # fire end-to-end (#1321).
        _sbx = self._agent_sandbox_policy
        sandbox_write_paths = (
            list(_sbx.get("write_paths") or [])
            if isinstance(_sbx, dict) and _sbx.get("write_paths") is not None
            else None
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
                file_read_paths=file_read_paths,
                file_write_paths=file_write_paths,
                http_hosts=http_hosts,
                sandbox_write_paths=sandbox_write_paths,
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
