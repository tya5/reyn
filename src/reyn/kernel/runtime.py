from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

import pydantic

from reyn.budget.budget import BudgetExceeded
from reyn.kernel.rollback_state import (
    RollbackState,  # noqa: F401 – re-exported for existing callers
)
from reyn.kernel.run_state import RunState
from reyn.schemas.models import ActOutput, CandidateOutput, ContextFrame, LLMOutput, Skill

if TYPE_CHECKING:
    from reyn.budget.budget import BudgetTracker
    from reyn.events.state_log import StateLog
    from reyn.skill.skill_registry import SkillRegistry
from reyn.config import SafetyConfig
from reyn.context_builder import build_frame
from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.kernel.llm_call_recorder import LLMCallRecorder
from reyn.kernel.normalizer import (
    ControlIRValidationError,
    NormalizationError,
    NormalizationResult,
    normalize,
)
from reyn.kernel.phase_executor import PhaseExecutor
from reyn.kernel.postprocessor_executor import PostprocessorExecutor
from reyn.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.kernel.runtime_types import (
    LoopLimitExceededError,
    PhaseBudgetExceededError,
    RunResult,
    WorkflowAbortedError,
    _normalize_artifact,
    _validate_artifact_structure,
)
from reyn.kernel.validation import ValidationError, validate_output
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.permissions.permissions import PermissionResolver
from reyn.safety.limit_handler import (
    handle_limit_exceeded,
    reset_run_extensions,
)
from reyn.skill.skill_node_runner import execute_skill_node
from reyn.user_intervention import InterventionBus
from reyn.workspace.artifact_validator import validate_artifact_data
from reyn.workspace.workspace import Workspace

# LoopLimitExceededError / PhaseBudgetExceededError / WorkflowAbortedError /
# RunResult / _normalize_artifact / _validate_artifact_structure moved to
# reyn.kernel.runtime_types (FP-0020 Component C follow-up — break circular
# imports between runtime.py and phase_executor.py). Re-exported above via
# `from reyn.kernel.runtime_types import (...)` for backward compatibility.
# RollbackState moved to reyn.kernel.rollback_state (FP-0020 Component A).


class OSRuntime:
    def __init__(
        self,
        skill: Skill,
        model: str,
        strict: bool = False,
        subscribers: list[Callable] | None = None,
        intervention_bus: "InterventionBus | None" = None,
        run_id: str | None = None,
        shell_allowed: bool = False,
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        safety: "SafetyConfig | None" = None,
        mcp_servers: dict | None = None,
        python_allowed_modules: list[str] | None = None,
        prompt_cache_enabled: bool = True,
        project_context: str = "",
        agent_role: str = "",
        caller: str = "direct",
        chain_id: str | None = None,
        budget_tracker: "BudgetTracker | None" = None,
        skill_name: str = "",
        state_log: "StateLog | None" = None,
        skill_registry: "SkillRegistry | None" = None,
        resume_plan: Any = None,
        parent_run_id: str | None = None,
    ) -> None:
        self.skill = skill
        self.model = model
        self._resolver = resolver or ModelResolver({})
        self.strict = strict
        self.run_id = run_id
        self._caller = caller
        self._chain_id = chain_id
        self._budget_tracker = budget_tracker
        self._budget_skill_name = skill_name or skill.name
        self.events = EventLog(subscribers=subscribers)
        self.workspace = Workspace(
            self.events,
            permission_resolver=permission_resolver,
            skill_name=skill.name,
        )
        # Populate internal limit attributes from SafetyConfig.
        _safety = safety or SafetyConfig()
        self._safety = _safety
        self._max_phase_visits = _safety.loop.max_phase_visits   # 0 = unlimited
        self._max_phase_wall_seconds = _safety.timeout.phase_seconds  # 0 = unlimited
        self._llm_timeout = _safety.timeout.llm_call_seconds
        self._llm_max_retries = _safety.timeout.llm_max_retries
        self._prompt_cache_enabled = prompt_cache_enabled
        # Public attributes — readable by tests / introspection. Treated as
        # immutable post-construction.
        self.project_context = project_context
        self.agent_role = agent_role
        # Private aliases retained so existing internal call sites stay stable.
        self._project_context = project_context
        self._agent_role = agent_role
        self._perm = permission_resolver
        self._intervention_bus = intervention_bus
        # FP-0005: per-run safety-limit checkpoint policy.
        self._on_limit = _safety.on_limit
        self._state_log = state_log
        self._skill_registry = skill_registry
        # PR-skill-resume D3b-3: optional ResumePlan for forward-replay
        # resume. When set, ``run()`` fast-forwards to the plan's
        # current_phase, restores visit_counts / history, and threads
        # the plan into ControlIRExecutor so dispatch_tool memoizes
        # against committed_steps. None means fresh start (default).
        self._resume_plan = resume_plan
        # R-D13: parent skill_run_id for nested skill spawned via
        # ``run_skill``. Recorded on the per-skill snapshot via
        # SkillRegistry.start so the parent / child tree survives crash.
        # ``None`` = top-level (user-invoked, or preprocessor sub-skill).
        self._parent_run_id = parent_run_id
        self.control_ir_executor = ControlIRExecutor(
            self.workspace, self.events,
            intervention_bus=intervention_bus,
            shell_allowed=shell_allowed,
            resolver=self._resolver,
            permission_resolver=permission_resolver,
            max_phase_visits=self._max_phase_visits,
            skill_name=skill.name,
            mcp_servers=mcp_servers,
            caller=caller,
            chain_id=chain_id,
            state_log=state_log,
            skill_run_id=run_id,
            resume_plan=resume_plan,
            run_id=run_id,
        )
        self._preprocessor = PreprocessorExecutor(
            skill=skill,
            workspace=self.workspace,
            model=self.model,
            events=self.events,
            subscribers=self.events.subscribers,
            resolver=self._resolver,
            max_phase_visits=self._max_phase_visits,
            permission_resolver=permission_resolver,
            intervention_bus=intervention_bus,
            python_allowed_modules=python_allowed_modules,
            caller=caller,
            run_id=run_id,
        )
        # FP-0020 Component A: all mutable run-scope state encapsulated in RunState.
        self._state = RunState()
        # FP-0020 Component B: LLM call / WAL recording / budget logic extracted to
        # LLMCallRecorder. OSRuntime._call_llm_and_record becomes a thin shim.
        self._llm_caller = LLMCallRecorder(
            resolver=self._resolver,
            state_log=state_log,
            run_id=run_id,
            skill_registry=skill_registry,
            budget_tracker=budget_tracker,
            caller=caller,
            chain_id=chain_id,
            skill_name=skill_name or skill.name,
            prompt_cache_enabled=prompt_cache_enabled,
            events=self.events,
            skill=skill,
            model=model,
            llm_timeout=_safety.timeout.llm_call_seconds,
            llm_max_retries=_safety.timeout.llm_max_retries,
            project_context=project_context,
            agent_role=agent_role,
            resume_plan=resume_plan,
        )
        # FP-0020 Component C: act/decide loops + phase-budget check extracted to
        # PhaseExecutor. build_frame is passed as a callable to avoid pulling the
        # full OSRuntime dependency tree into phase_executor.py.
        self._phase_executor = PhaseExecutor(
            llm_caller=self._llm_caller,
            control_ir_executor=self.control_ir_executor,
            events=self.events,
            skill=skill,
            safety=_safety,
            intervention_bus=intervention_bus,
            run_id=run_id,
            strict=strict,
            build_frame_fn=self.build_frame,
        )

    # ── Backward-compat properties (FP-0020 Component A) ───────────────────
    # Tests and subclasses that accessed the old private fields directly
    # can continue to do so via these thin pass-through properties.
    # Remove in a subsequent cleanup PR once callers migrate to _state.*

    @property
    def _visit_counts(self) -> dict[str, int]:
        return self._state.visit_counts

    @_visit_counts.setter
    def _visit_counts(self, value: dict[str, int]) -> None:
        self._state.visit_counts = value

    @property
    def _history(self) -> list[str]:
        return self._state.history

    @_history.setter
    def _history(self, value: list[str]) -> None:
        self._state.history = value

    # ── Phase setup ────────────────────────────────────────────────────────────

    async def _handle_limit_checkpoint(
        self,
        *,
        kind: str,
        prompt: str,
        detail: str,
        extension_amount: float,
    ) -> "LimitDecision":
        """FP-0005: dispatch a safety-limit checkpoint.

        Wraps ``handle_limit_exceeded`` with the runtime's bus / on_limit
        / run_id pre-bound, and emits a ``safety_limit_checkpoint``
        audit event so the decision (and reason) is visible in the
        events log. Each abort-path call site invokes this *before*
        raising; on ``allow_continue=True`` the site extends its
        counter and continues, otherwise it falls through to the
        legacy raise.
        """
        decision = await handle_limit_exceeded(
            bus=self._intervention_bus,
            on_limit=self._on_limit,
            kind=kind,
            run_id=self.run_id or "",
            prompt=prompt,
            detail=detail,
            extension_amount=extension_amount,
        )
        if decision.allow_continue:
            self._state.grant_extension(kind, decision.extension)
        self.events.emit(
            "safety_limit_checkpoint",
            kind=kind,
            allow_continue=decision.allow_continue,
            reason=decision.reason,
            extension=decision.extension,
        )
        return decision

    async def _enter_phase(self, phase_name: str, artifact: dict) -> None:
        max_visits = self._max_phase_visits
        # FP-0005: extensions granted by user approval / auto_extend
        # raise the effective cap. Tracked per-kind on the runtime so
        # repeated hits on the same limit can be re-extended.
        effective_max = self._state.effective_visit_cap(max_visits)
        count = self._state.visit_counts.get(phase_name, 0)
        if effective_max and count >= effective_max:
            # FP-0005: ask before raising. on_limit.mode controls the
            # behaviour; default 'unattended' preserves legacy abort.
            decision = await self._handle_limit_checkpoint(
                kind="max_phase_visits",
                prompt=(
                    f"Phase {phase_name!r} hit max_phase_visits "
                    f"({count}/{effective_max}). Allow more visits?"
                ),
                detail=f"phase={phase_name} count={count} cap={effective_max}",
                extension_amount=float(max_visits or 1),
            )
            if not decision.allow_continue:
                self.events.emit(
                    "loop_limit_exceeded",
                    phase=phase_name, visit_count=count, max=effective_max,
                )
                # FP-0004: hint at the config key the operator can raise.
                raise LoopLimitExceededError(
                    f"Phase '{phase_name}' reached max_phase_visits={effective_max}. "
                    f"→ Raise {LoopLimitExceededError.hint_config_key} to allow "
                    f"more iterations."
                )
            # Approved — fall through; effective_max has already been
            # bumped via _safety_extensions and will be picked up on
            # the next visit.
        # begin_phase() increments visit_counts, resets phase_started_at and
        # llm_call_idx_in_phase — mirrors the three-statement block at original L438/441/446.
        new_count = self._state.begin_phase(phase_name)
        self.events.emit(
            "phase_started", phase=phase_name,
            visit_count=new_count, input_artifact_type=artifact.get("type"),
        )

    def _build_candidates(self, current_phase: str) -> list[CandidateOutput]:
        skill = self.skill
        allowed = skill.graph.transitions.get(current_phase, [])
        can_finish = current_phase in skill.graph.can_finish_phases
        candidates: list[CandidateOutput] = []
        for phase_name in allowed:
            if phase_name in skill.graph.skill_nodes:
                node_spec = skill.graph.skill_nodes[phase_name]
                candidates.append(CandidateOutput(
                    next_phase=phase_name,
                    control_type="transition",
                    schema_name=node_spec.entry_input_schema_name,
                    artifact_schema=node_spec.entry_input_schema,
                    description=node_spec.entry_input_description,
                ))
            else:
                p = skill.phases[phase_name]
                candidates.append(CandidateOutput(
                    next_phase=phase_name,
                    control_type="transition",
                    schema_name=p.input_schema_name,
                    artifact_schema=p.input_schema,
                    description=p.input_description,
                ))
        if can_finish or not allowed:
            candidates.append(CandidateOutput(
                next_phase="end",
                control_type="finish",
                schema_name=skill.final_output_name or "final_output",
                artifact_schema=skill.final_output_schema,
                description=skill.final_output_description,
            ))
        if self._state.prev_phase is not None:
            candidates.append(CandidateOutput(
                next_phase="rollback",
                control_type="rollback",
                schema_name="rollback",
                artifact_schema={},
                description=(
                    f"Reject the output from '{self._state.prev_phase}' and send it back for revision. "
                    "Use when the current phase determines the preceding phase produced invalid output. "
                    "Put the rejection reason in control.reason.summary. "
                    "next_phase MUST be null. decision MUST be 'continue'."
                ),
            ))
        candidates.append(CandidateOutput(
            next_phase="abort",
            control_type="abort",
            schema_name="abort_reason",
            artifact_schema={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Reason for aborting (= why the skill cannot proceed)",
                    },
                },
                "required": ["reason"],
            },
            description=(
                "Abort the skill — used when external constraints (= cost limit, infeasibility, "
                "denial) prevent completion. Set control.type='abort', control.decision='abort', "
                "control.next_phase=null. Put the reason in control.reason.summary and the "
                "artifact's reason field."
            ),
        ))
        return candidates

    def _effective_model(self, phase_name: str) -> str:
        phase = self.skill.phases.get(phase_name)
        return phase.model_class if phase and phase.model_class else self.model

    def build_frame(
        self,
        current_phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        control_ir_results: list[dict] | None = None,
        artifact_path: str | None = None,
        remaining_act_turns: int | None = None,
        force_decide: bool = False,
    ) -> ContextFrame:
        effective_model = self._effective_model(current_phase)
        phase_def = self.skill.phases[current_phase]
        allowed = set(phase_def.allowed_ops)
        all_ops = self.control_ir_executor.available_ops()
        filtered_ops = [op for op in all_ops if op.kind in allowed]
        # When the act budget is exhausted, strip available ops so the LLM has
        # no ops to call and is structurally forced into a decide turn.
        effective_ops = [] if force_decide else filtered_ops
        return build_frame(
            phase_name=current_phase,
            phase=phase_def,
            artifact=artifact,
            candidates=candidates,
            output_language=output_language,
            history=self._state.history,
            visit_counts=self._state.visit_counts,
            finish_criteria=self.skill.finish_criteria,
            max_phase_visits=self._max_phase_visits or None,
            available_ops=effective_ops,
            op_catalog=all_ops,
            effective_model=effective_model,
            model_resolved=self._resolver.resolve(effective_model).model,
            events=self.events,
            control_ir_results=control_ir_results,
            artifact_path=artifact_path,
            remaining_act_turns=remaining_act_turns,
        )

    # ── Single-attempt validation ──────────────────────────────────────────────

    def _validate_phase_output(
        self,
        raw: dict,
        current_phase: str,
        candidates: list[CandidateOutput],
        allowed_next: list[str],
        input_artifact: dict | None = None,
    ) -> tuple[NormalizationResult, LLMOutput]:
        """
        Normalize and validate one LLM response.
        Returns (result, output) on success.
        Raises WorkflowAbortedError for abort (non-retryable).
        Raises ValueError for any retryable validation failure.
        """
        candidate_map = {c.next_phase: c for c in candidates}

        try:
            result = normalize(raw, allowed_next)
        except ControlIRValidationError as exc:
            self.events.emit("control_ir_validation_error", phase=current_phase, error=str(exc))
            raise ValueError(str(exc)) from exc
        except NormalizationError as exc:
            self.events.emit("normalization_error", phase=current_phase, error=str(exc))
            raise ValueError(str(exc)) from exc

        self.events.emit(
            "control_decided",
            phase=current_phase,
            control_type=result.control.type,
            decision=result.control.decision,
            next_phase=result.control.next_phase,
            confidence=result.control.confidence,
            reason=result.control.reason.model_dump(),
            was_normalized=result.was_normalized,
            was_inferred=result.was_inferred,
        )

        if result.control.type == "abort":
            raise WorkflowAbortedError(
                f"LLM aborted workflow at phase '{current_phase}': "
                f"{result.control.reason.summary}"
            )

        if result.control.type == "rollback":
            output = LLMOutput(
                control=result.control,
                artifact={"type": "rollback", "data": {}},
                ops=result.ops,
            )
            return result, output

        matched_candidate = candidate_map[result.control.effective_next_phase]
        normalized = _normalize_artifact(result.artifact, matched_candidate.schema_name)

        try:
            _validate_artifact_structure(normalized, current_phase)
        except ValueError as exc:
            self.events.emit("validation_error", phase=current_phase, error=str(exc))
            raise

        # Plumb input artifact as validation context so cross-field
        # constraints can resolve against the phase's input. Two slots:
        #  - ``input``        — the immediate phase input (LLM-authored
        #                       in chained phases; do NOT use for trust
        #                       checks).
        #  - ``skill_input``  — PR33. The skill's initial input pinned at
        #                       run() entry. Trusted: no LLM phase can
        #                       overwrite. Use this slot in
        #                       ``x-reyn-members-of`` paths whose
        #                       membership set the LLM must not be able
        #                       to fabricate.
        # P7-clean: the OS supplies the generic context dict; only the
        # skill's schema names specific keys.
        validation_context: dict | None = None
        if input_artifact is not None or self._state.skill_input is not None:
            validation_context = {}
            if input_artifact is not None:
                validation_context["input"] = input_artifact
            if self._state.skill_input is not None:
                validation_context["skill_input"] = self._state.skill_input
        norm_data, corrections, errors = validate_artifact_data(
            normalized,
            matched_candidate.artifact_schema,
            strict=self.strict,
            validation_context=validation_context,
        )
        self.events.emit(
            "artifact_validated",
            phase=current_phase,
            artifact_type=normalized.get("type"),
            next_phase=result.control.effective_next_phase,
            was_corrected=bool(corrections),
            corrections=corrections,
            errors=errors,
        )
        if errors:
            error_str = "; ".join(errors)
            self.events.emit("validation_error", phase=current_phase, error=error_str)
            raise ValueError(
                f"Artifact data validation failed for '{normalized.get('type')}': {error_str}"
            )

        try:
            output = LLMOutput(
                control=result.control,
                artifact={**normalized, "data": norm_data},
                ops=result.ops,
            )
        except pydantic.ValidationError as exc:
            msg = f"Invalid ops structure: {exc}"
            self.events.emit("validation_error", phase=current_phase, error=msg)
            raise ValueError(msg) from exc

        try:
            validate_output(output, candidates)
        except ValidationError as exc:
            self.events.emit("validation_error", phase=current_phase, error=str(exc))
            raise ValueError(str(exc)) from exc

        return result, output

    # ── Phase execution with act/decide loop and retry ─────────────────────────

    async def _call_llm_and_record(
        self,
        phase: str,
        frame: ContextFrame,
        prior_attempts: list[dict[str, str]] | None,
        rollback_context: dict | None = None,
    ) -> dict:
        """Shim: delegate to LLMCallRecorder.

        FP-0020 Component B: the 7 LLM/WAL/budget methods formerly on
        OSRuntime now live in LLMCallRecorder. This thin wrapper preserves
        the call signature so existing callers and tests are unaffected.
        FP-0020 Component C: ``_check_phase_budget`` moved to PhaseExecutor;
        callers going via self._execute_phase no longer need it here.
        """
        return await self._llm_caller.call(
            phase, frame, prior_attempts, rollback_context, self._state,
        )

    # ── Backward-compat shims for private LLMCallRecorder methods ─────────────
    # Tests and other callers that invoke _wal_step_completed_for_llm or
    # _extract_memoized_llm_result directly on OSRuntime continue to work.
    # Remove in a subsequent cleanup PR.

    async def _wal_step_completed_for_llm(self, **kwargs) -> None:
        await self._llm_caller._wal_step_completed_for_llm(**kwargs)

    def _extract_memoized_llm_result(self, memo, *, phase, op_invocation_id):
        return self._llm_caller._extract_memoized_llm_result(
            memo, phase=phase, op_invocation_id=op_invocation_id,
        )

    async def _run_act_loop(
        self,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        max_act_turns: int,
        max_phase_retries: int,
        artifact_path: str | None,
        rollback_context: dict | None = None,
    ) -> tuple[dict, list[dict]]:
        """
        Drive act turns until the LLM emits a decide turn.
        Returns (raw_decide_response, accumulated_prior_attempts).
        """
        control_ir_results: list[dict] = []
        prior_attempts: list[dict[str, str]] = []
        act_turn_count = 0
        first_call = True

        while True:
            if prior_attempts:
                self.events.emit(
                    "phase_retry", phase=phase,
                    attempt=len(prior_attempts), max_retries=max_phase_retries,
                    error=prior_attempts[-1]["error"],
                )

            remaining = max_act_turns - act_turn_count if max_act_turns > 0 else None
            # When act budget is exhausted, strip available ops from the frame so
            # the LLM structurally cannot emit another act turn — it has no ops
            # to call and must produce a decide turn.
            force_decide = remaining is not None and remaining <= 0
            frame = self.build_frame(
                phase, artifact, candidates, output_language,
                control_ir_results=control_ir_results,
                artifact_path=artifact_path,
                remaining_act_turns=remaining,
                force_decide=force_decide,
            )
            # Pass rollback_context only on the first LLM call; subsequent calls already have context
            raw = await self._call_llm_and_record(
                phase, frame, prior_attempts or None,
                rollback_context=rollback_context if first_call else None,
            )
            first_call = False

            if raw.get("type") != "act":
                return raw, prior_attempts

            act_turn_count += 1
            # FP-0005: extensions granted by the safety-limit helper raise
            # the effective act-turn cap for THIS phase instance.
            effective_max_act_turns = self._state.effective_act_turn_cap(phase, max_act_turns)
            if act_turn_count > effective_max_act_turns:
                if force_decide:
                    # LLM emitted ops despite being told no more are allowed.
                    # Don't execute the ops — feed back an explicit error and retry.
                    act_turn_count -= 1
                    prior_attempts.append({
                        "raw": json.dumps(raw, ensure_ascii=False),
                        "error": (
                            f"You emitted act-turn ops but your act budget is exhausted "
                            f"({effective_max_act_turns}/{effective_max_act_turns} act turns used). "
                            "Do NOT include any ops. Produce the final artifact and transition NOW."
                        ),
                    })
                    if len(prior_attempts) > max_phase_retries:
                        final_msg = (
                            f"Phase '{phase}' failed: LLM refused to produce a decide turn "
                            f"after {len(prior_attempts)} retries with force_decide=True."
                        )
                        self.events.emit("phase_failed", phase=phase,
                                         attempts=len(prior_attempts), final_error=final_msg)
                        raise ValueError(final_msg)
                    continue
                # FP-0005: ask before raising. Caller falls through on
                # refusal; on approval, the per-phase extension counter
                # is bumped and the loop continues.
                decision = await self._handle_limit_checkpoint(
                    kind=f"max_act_turns:{phase}",
                    prompt=(
                        f"Phase {phase!r} exceeded max_act_turns "
                        f"({effective_max_act_turns}). Allow more act turns?"
                    ),
                    detail=(
                        f"phase={phase} act_turn_count={act_turn_count} "
                        f"cap={effective_max_act_turns}"
                    ),
                    extension_amount=float(max_act_turns),
                )
                if decision.allow_continue:
                    # Approved — extension was already recorded on
                    # _safety_extensions, recompute next iteration.
                    continue
                msg = (
                    f"Phase '{phase}' exceeded max act turns ({effective_max_act_turns}). "
                    "The LLM kept emitting act turns without making a decide turn."
                )
                self.events.emit("phase_failed", phase=phase,
                                 attempts=act_turn_count, final_error=msg)
                raise ValueError(msg)

            try:
                act = ActOutput.model_validate(raw)
            except pydantic.ValidationError as exc:
                prior_attempts.append({"raw": json.dumps(raw, ensure_ascii=False), "error": str(exc)})
                if len(prior_attempts) > max_phase_retries:
                    self.events.emit("phase_failed", phase=phase,
                                     attempts=len(prior_attempts), final_error=str(exc))
                    raise ValueError(
                        f"Phase '{phase}' failed after {len(prior_attempts)} attempt(s): {exc}"
                    ) from exc
                continue

            phase_def = self.skill.phases.get(phase)
            phase_decl = self.skill.permissions
            allowed_ops = set(phase_def.allowed_ops) if phase_def is not None else None
            ir_results = await self.control_ir_executor.execute(
                act.ops, phase=phase, decl=phase_decl, allowed_ops=allowed_ops,
            )
            # Accumulate across act turns so the LLM can see all prior results
            # when deciding what to do next (e.g. glob→read→write sequences).
            control_ir_results = control_ir_results + ir_results
            prior_attempts = []
            self.events.emit(
                "act_executed",
                phase=phase,
                op_count=len(act.ops),
                act_turn=act_turn_count,
                ops=[op.model_dump() for op in act.ops],
                results=ir_results,
            )

    async def _run_decide_with_retry(
        self,
        raw: dict,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        prior_attempts: list[dict[str, str]],
        max_phase_retries: int,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """
        Validate a decide-turn response, retrying on rejection.
        Returns (result, output, retry_count).
        """
        allowed_next = [c.next_phase for c in candidates]

        while True:
            try:
                result, output = self._validate_phase_output(
                    raw, phase, candidates, allowed_next, input_artifact=artifact,
                )
                return result, output, len(prior_attempts)
            except WorkflowAbortedError:
                raise
            except ValueError as exc:
                prior_attempts.append({"raw": json.dumps(raw, ensure_ascii=False), "error": str(exc)})
                if len(prior_attempts) > max_phase_retries:
                    self.events.emit(
                        "phase_failed", phase=phase,
                        attempts=len(prior_attempts), final_error=str(exc),
                    )
                    raise ValueError(
                        f"Phase '{phase}' failed after {len(prior_attempts)} attempt(s): {exc}"
                    ) from exc

                self.events.emit(
                    "phase_retry", phase=phase,
                    attempt=len(prior_attempts), max_retries=max_phase_retries,
                    error=prior_attempts[-1]["error"],
                )
                frame = self.build_frame(phase, artifact, candidates, output_language)
                # rollback_context already injected in act loop's first call; retries don't repeat it
                raw = await self._call_llm_and_record(phase, frame, prior_attempts)

    async def _execute_phase(
        self,
        current_phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        max_phase_retries: int,
        artifact_path: str | None = None,
        rollback_context: dict | None = None,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """Drive one phase to completion via act/decide loops with retry."""
        phase_def = self.skill.phases[current_phase]
        max_act_turns = phase_def.max_act_turns if phase_def.max_act_turns > 0 else 10

        raw, prior_attempts = await self._run_act_loop(
            current_phase, artifact, candidates, output_language,
            max_act_turns, max_phase_retries, artifact_path,
            rollback_context=rollback_context,
        )
        return await self._run_decide_with_retry(
            raw, current_phase, artifact, candidates, output_language, prior_attempts, max_phase_retries,
        )

    # ── Fallback ───────────────────────────────────────────────────────────────

    def _fallback_final_output(self) -> dict:
        for entry in reversed(self.workspace.artifacts):
            art = entry["artifact"]
            if art.get("type") == self.skill.final_output_name:
                return art.get("data", {})
        if self.workspace.artifacts:
            return self.workspace.artifacts[-1]["artifact"].get("data", {})
        return {}

    # ── App-node dispatch ──────────────────────────────────────────────────────

    async def _run_skill_node(
        self,
        node_id: str,
        input_artifact: dict,
        target_schema: dict,
        target_type: str,
        output_language: str | None,
    ) -> dict:
        node_spec = self.skill.graph.skill_nodes[node_id]
        adapted, usage = await execute_skill_node(
            node_id=node_id,
            node_spec=node_spec,
            input_artifact=input_artifact,
            target_schema=target_schema,
            target_type=target_type,
            output_language=output_language,
            model=self.model,
            strict=self.strict,
            subscribers=self.events.subscribers,
            resolver=self._resolver,
            events=self.events,
            safety=self._safety,
        )
        self._state.add_usage(usage, None)
        return adapted

    # ── Rollback dispatch ──────────────────────────────────────────────────────

    def _handle_rollback(
        self, current_phase: str, reason_summary: str,
    ) -> tuple[str, dict, str | None]:
        """Process a rollback decision.

        Returns (target_phase, target_input_artifact, target_predecessor).
        Raises WorkflowAbortedError if there is no previous phase to roll
        back to (e.g. the very first phase emitted rollback).
        """
        target = self._state.prev_phase
        if target is None:
            raise WorkflowAbortedError(
                f"Phase '{current_phase}' emitted rollback but there is no previous phase."
            )
        self.events.emit(
            "phase_rollback",
            rollback_from=current_phase,
            rollback_to=target,
            reason=reason_summary,
        )
        self._state.rollback.begin_rollback(current_phase, target, reason_summary)
        self._state.history.append(f"{current_phase} → rollback → {target}")
        return target, self._state.rollback.get_input(target), self._state.rollback.get_predecessor(target)

    # ── Workflow termination ──────────────────────────────────────────────────

    async def _finish_workflow(
        self,
        phase: str,
        data: dict,
        reason: str,
        confidence: float,
        finish_artifact: dict | None = None,
        output_language: str | None = None,
        resume_plan: object = None,
    ) -> RunResult:
        """Single source of truth for "the workflow ended cleanly".

        Both the normal end-of-graph path and the skill_node terminal path
        go through here so observers see consistent event shape and the
        RunResult is constructed identically.

        When skill.postprocessor is set, the LLM's finish artifact is passed
        through the postprocessor chain before the caller receives it.
        ``finish_artifact`` is the full {type, data} artifact; ``data`` is
        the pre-postprocessor payload (used as fallback when no postprocessor
        runs). On postprocessor success ``data`` is replaced with the
        postprocessor output's "data" field.

        ``resume_plan``: when mid-postprocessor resume is detected in
        ``run()``, this is forwarded so PostprocessorExecutor can replay
        already-committed steps via memo without re-executing.

        Crash-recovery protocol (piece 1):
          1. Persist the LLM finish artifact to workspace so it is durable.
          2. Advance the per-skill snapshot to ``current_phase="__post__"``
             so a crash mid-postprocessor is detectable on next startup.
          3. Run the postprocessor (with resume_plan for memo on restart).
        """
        if self.skill.postprocessor is not None and finish_artifact is not None:
            # ── Step 1: persist finish artifact before postprocessor starts ────
            # This makes the postprocessor input durable so a crash between
            # postprocessor steps leaves the artifact recoverable.
            artifact_path: str | None = None
            if not resume_plan:
                # Only persist on a fresh (non-resumed) run. On resume we
                # already have last_phase_artifact_path from the snapshot.
                artifact_path = self.workspace.store_artifact(
                    "__post__", finish_artifact,
                    skill_name=self.skill.name, visit=1,
                )
                # ── Step 2: advance snapshot to __post__ ──────────────────────
                if self._skill_registry:
                    await self._skill_registry.advance_phase(
                        run_id=self.run_id,
                        next_phase="__post__",
                        last_phase_artifact_path=artifact_path,
                    )

            post_executor = PostprocessorExecutor(
                skill=self.skill,
                workspace=self.workspace,
                events=self.events,
                model=self.model,
                resolver=self._resolver,
                subscribers=self.events.subscribers,
                permission_resolver=self._perm,
                intervention_bus=self._intervention_bus,
                max_phase_visits=self._max_phase_visits,
                caller=self._caller,
                state_log=self._state_log,
                skill_run_id=self.run_id,
            )
            post_artifact, post_usage = await post_executor.run(
                finish_artifact, output_language, resume_plan=resume_plan,
            )
            self._state.add_usage(post_usage, None)
            data = post_artifact.get("data", {})

        self.events.emit(
            "workflow_finished",
            run_id=self.run_id,
            skill=self.skill.name,
            phase=phase,
            reason=reason,
            confidence=confidence,
            total_phase_count=sum(self._state.visit_counts.values()),
            final_output_keys=list(data.keys()),
        )
        return RunResult(
            data=data, status="finished",
            token_usage=self._state.token_usage,
            cost_usd=self._state.total_cost_usd or None,
        )

    # ── Skill-node dispatch (transition to a sub-skill node) ───────────────────

    async def _apply_skill_node(
        self,
        node_id: str,
        current_phase: str,
        output_artifact: dict,
        output_language: str | None,
    ) -> "RunResult | tuple[str, dict]":
        """Run a skill_node and decide whether the workflow ends here.

        Returns either:
          - a RunResult, when this node is terminal (no post-nodes); the
            caller should propagate it as the workflow's result, or
          - (next_after, adapted_artifact), when execution should continue
            into `next_after` with the LLM-adapted artifact as input.
        """
        post_nodes = self.skill.graph.transitions.get(node_id, [])
        if not post_nodes:
            adapted = await self._run_skill_node(
                node_id, output_artifact,
                self.skill.final_output_schema, self.skill.final_output_name,
                output_language,
            )
            data = adapted.get("data", {})
            self._state.history.append(f"{current_phase} → {node_id} → END")
            return await self._finish_workflow(
                phase=node_id,
                data=data,
                reason="app node produced final output",
                confidence=1.0,
                finish_artifact=adapted,
                output_language=output_language,
            )
        next_after = post_nodes[0]
        next_phase_obj = self.skill.phases[next_after]
        adapted = await self._run_skill_node(
            node_id, output_artifact,
            next_phase_obj.input_schema, next_phase_obj.input_schema_name,
            output_language,
        )
        self._state.history.append(f"{current_phase} → {node_id} → {next_after}")
        return next_after, adapted

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run(
        self,
        initial_input: dict,
        output_language: str | None = None,
        max_phase_retries: int = 2,
    ) -> RunResult:
        """
        Execute the workflow from entry_phase to completion.

        max_phase_retries: retries per phase on validation failure (default 2 = 3 total attempts).
        Returns RunResult with status="finished" or status="loop_limit_exceeded".
        Raises WorkflowAbortedError on unrecoverable LLM abort.
        """
        if self._perm:
            if self._intervention_bus is None:
                raise RuntimeError(
                    "permission_resolver requires intervention_bus on OSRuntime; "
                    "wire one via Agent(intervention_bus=...)"
                )
            await self._perm.startup_guard(
                self.skill, self.skill.name, self._intervention_bus,
            )

        # FP-0005: reset auto_extend bookkeeping for this run, so the
        # ``auto_extend_times`` budget is fresh per-run (not per-process).
        if self.run_id:
            reset_run_extensions(self.run_id)

        current_phase = self.skill.entry_phase
        artifact = initial_input
        # PR33: pin the trusted input for cross-field validation across all
        # phases. Schemas downstream can reference fields here that no LLM
        # phase can tamper with.
        self._state.skill_input = initial_input

        self.events.emit(
            "workflow_started",
            run_id=self.run_id,
            skill=self.skill.name,
            entry_phase=self.skill.entry_phase,
            input_type=artifact.get("type"),
            default_model=self._resolver.resolve(self.model).model,
        )

        # PR-skill-resume D3b-3: forward-replay fast-forward.
        # When a ResumePlan is supplied, jump straight to the
        # plan's current_phase (the phase that was in flight at crash
        # time) and restore visit_counts + history so loop-limit checks
        # and transition logging continue from the prior run's state.
        # The plan's last_phase_artifact_path is used as the input
        # artifact when present so the new current_phase sees the same
        # input it would have seen had the prior run not crashed.
        if self._resume_plan is not None:
            if self._resume_plan.current_phase:
                current_phase = self._resume_plan.current_phase
            # R-D2: restore visit_counts / history and pre-decrement current phase
            # so the upcoming begin_phase() increment lands on the SAME count the
            # original run had (memo correctness). See RunState.restore_from_resume.
            self._state.restore_from_resume(self._resume_plan, current_phase)
            # Restore the last completed phase's artifact as the input
            # to current_phase. Falls back to initial_input when the
            # plan has no recorded artifact path (e.g. the entry phase
            # was the one in flight).
            artifact_path = getattr(
                self._resume_plan, "last_phase_artifact_path", None,
            )
            if artifact_path:
                try:
                    import json as _json
                    p = Path(artifact_path)
                    if p.is_file():
                        artifact = _json.loads(p.read_text(encoding="utf-8"))
                except Exception as e:  # noqa: BLE001 — defensive
                    import logging
                    logging.getLogger(__name__).warning(
                        "resume: cannot load last_phase_artifact_path %s: %s",
                        artifact_path, e,
                    )
            self.events.emit(
                "skill_resumed",
                run_id=self.run_id,
                resume_phase=current_phase,
                visit_counts=dict(self._state.visit_counts),
            )

        if self._skill_registry:
            await self._skill_registry.start(
                run_id=self.run_id,
                skill_name=self.skill.name,
                skill_input=initial_input,
                parent_run_id=self._parent_run_id,
            )

        # ── __post__ resume entry (piece 3) ───────────────────────────────────
        # When a crash happened mid-postprocessor the snapshot's current_phase
        # is "__post__". On resume the phase loop would try to enter a real
        # phase named "__post__" (which doesn't exist) — instead we detect
        # this sentinel, load the persisted finish artifact from
        # last_phase_artifact_path, and jump straight to _finish_workflow with
        # the resume_plan so completed steps are memoized.
        if self._resume_plan is not None and current_phase == "__post__":
            artifact_path_post = getattr(
                self._resume_plan, "last_phase_artifact_path", None,
            )
            finish_artifact_post: dict | None = None
            if artifact_path_post:
                try:
                    import json as _json
                    p = Path(artifact_path_post)
                    if p.is_file():
                        finish_artifact_post = _json.loads(
                            p.read_text(encoding="utf-8")
                        )
                except Exception as _e:  # noqa: BLE001 — defensive
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "__post__ resume: cannot load finish artifact %s: %s",
                        artifact_path_post, _e,
                    )

            if finish_artifact_post is None:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "__post__ resume: no finish artifact found; "
                    "using empty artifact — postprocessor will re-execute all steps",
                )
                finish_artifact_post = {"type": "unknown", "data": {}}

            # _finish_workflow calls skill_registry.complete via the finally
            # block so the snapshot is removed on success.
            try:
                return await self._finish_workflow(
                    phase="__post__",
                    data=finish_artifact_post.get("data", {}),
                    reason="resumed from __post__ state",
                    confidence=1.0,
                    finish_artifact=finish_artifact_post,
                    output_language=output_language,
                    resume_plan=self._resume_plan,
                )
            finally:
                if self._skill_registry:
                    import sys as _sys
                    exc_type, _, _ = _sys.exc_info()
                    if exc_type is None or issubclass(exc_type, WorkflowAbortedError):
                        await self._skill_registry.complete(run_id=self.run_id)
                    else:
                        self.events.emit(
                            "skill_run_interrupted",
                            run_id=self.run_id,
                            exc_type=exc_type.__name__ if exc_type else "unknown",
                            will_resume=True,
                        )

        artifact_path: str | None = self.workspace.store_artifact(
            "_input", artifact, skill_name=self.skill.name, visit=1
        )

        try:
            await self._enter_phase(current_phase, artifact)
            if self._skill_registry:
                await self._skill_registry.advance_phase(
                    run_id=self.run_id,
                    next_phase=current_phase,
                    last_phase_artifact_path=artifact_path,
                )

            while True:
                rollback_context = self._state.rollback.take_pending_ctx()

                # Store the pre-preprocessor artifact for rollback.
                # On rollback, the preprocessor re-runs deterministically from this snapshot —
                # semantically correct, but costly for heavy chains (iterate × run_app).
                # If eval rollback causes N-item re-evaluation, revisit caching here (Phase 5+).
                self._state.rollback.record_input(current_phase, artifact)

                candidates = self._build_candidates(current_phase)

                # Run preprocessor (deterministic enrichment) before handing artifact to LLM
                phase_def = self.skill.phases[current_phase]
                if phase_def.preprocessor:
                    enriched_artifact, pre_usage = await self._preprocessor.run(
                        phase_def, artifact, output_language
                    )
                    self._state.add_usage(pre_usage, None)
                    # Update artifact_path to the enriched file so maybe_ref_artifact
                    # references the correct (post-preprocessor) artifact when it is large.
                    artifact_path = self.workspace.store_artifact(
                        current_phase + "_preprocessed", enriched_artifact,
                        skill_name=self.skill.name,
                        visit=self._state.visit_counts.get(current_phase, 1),
                    )
                else:
                    enriched_artifact = artifact

                result, output, retry_count = await self._execute_phase(
                    current_phase, enriched_artifact, candidates, output_language, max_phase_retries,
                    artifact_path=artifact_path,
                    rollback_context=rollback_context,
                )

                current_def = self.skill.phases.get(current_phase)
                current_decl = self.skill.permissions
                current_allowed = set(current_def.allowed_ops) if current_def is not None else None
                decide_results = await self.control_ir_executor.execute(
                    output.ops, phase=current_phase, decl=current_decl,
                    allowed_ops=current_allowed,
                )
                if decide_results:
                    self.events.emit(
                        "decide_ops_executed",
                        phase=current_phase,
                        op_count=len(decide_results),
                        ops=[op.model_dump() for op in output.ops],
                        results=decide_results,
                    )

                # Handle rollback before storing artifact or emitting phase_completed
                if result.control.type == "rollback":
                    current_phase, artifact, self._state.prev_phase = self._handle_rollback(
                        current_phase, result.control.reason.summary,
                    )
                    artifact_path = None
                    await self._enter_phase(current_phase, artifact)
                    if self._skill_registry:
                        await self._skill_registry.advance_phase(
                            run_id=self.run_id,
                            next_phase=current_phase,
                            last_phase_artifact_path=artifact_path,
                        )
                    continue

                # No-progress detection: if this phase was just re-run after a rollback
                # and produced an output structurally identical to the rejected one, abort.
                rollback_from = self._state.rollback.consume_no_progress(
                    current_phase, output.artifact.get("data"),
                )
                if rollback_from is not None:
                    self.events.emit(
                        "phase_no_progress",
                        phase=current_phase,
                        rollback_from=rollback_from,
                    )
                    raise WorkflowAbortedError(
                        f"Phase '{current_phase}' produced an output identical to the one "
                        f"rejected by '{rollback_from}'. The rollback feedback did not lead "
                        f"to any change — aborting to prevent a wasteful loop."
                    )

                self._state.rollback.record_output(current_phase, output.artifact)

                artifact_path = self.workspace.store_artifact(
                    current_phase, output.artifact,
                    skill_name=self.skill.name,
                    visit=self._state.visit_counts.get(current_phase, 1),
                )

                self.events.emit(
                    "phase_completed",
                    phase=current_phase,
                    next=output.next_phase,
                    was_normalized=result.was_normalized,
                    was_inferred=result.was_inferred,
                    retries=retry_count,
                    reason=result.control.reason.summary,
                    confidence=result.control.confidence,
                    artifact_path=artifact_path,
                )

                if output.next_phase == "end":
                    data = output.artifact.get("data", {})
                    self._state.history.append(f"{current_phase} → END")
                    return await self._finish_workflow(
                        phase=current_phase,
                        data=data,
                        reason=result.control.reason.summary,
                        confidence=result.control.confidence,
                        finish_artifact=output.artifact,
                        output_language=output_language,
                    )

                next_node = output.next_phase
                if next_node in self.skill.graph.skill_nodes:
                    outcome = await self._apply_skill_node(
                        next_node, current_phase, output.artifact, output_language,
                    )
                    if isinstance(outcome, RunResult):
                        return outcome
                    next_after, adapted = outcome
                    self._state.prev_phase = current_phase
                    self._state.rollback.record_predecessor(next_after, current_phase)
                    current_phase = next_after
                    artifact = adapted
                else:
                    self._state.history.append(f"{current_phase} → {next_node}")
                    self._state.prev_phase = current_phase
                    self._state.rollback.record_predecessor(next_node, current_phase)
                    current_phase = next_node
                    artifact = output.artifact
                await self._enter_phase(current_phase, artifact)
                if self._skill_registry:
                    await self._skill_registry.advance_phase(
                        run_id=self.run_id,
                        next_phase=current_phase,
                        last_phase_artifact_path=artifact_path,
                    )

        except LoopLimitExceededError as exc:
            # FP-0005: surface the last completed artifact via partial_data
            # so callers can render "here's what we have so far" UX. data
            # is also populated for backward compat with legacy callers.
            final_output = self._fallback_final_output()
            self.events.emit(
                "workflow_terminated",
                reason=str(exc),
                total_phase_count=sum(self._state.visit_counts.values()),
                final_output_keys=list(final_output.keys()),
            )
            return RunResult(
                data=final_output,
                status="loop_limit_exceeded",
                token_usage=self._state.token_usage,
                cost_usd=self._state.total_cost_usd or None,
                partial_data=final_output or None,
            )

        except PhaseBudgetExceededError as exc:
            # FP-0005: same partial_data treatment as LoopLimitExceededError.
            final_output = self._fallback_final_output()
            self.events.emit(
                "workflow_terminated",
                reason=str(exc),
                total_phase_count=sum(self._state.visit_counts.values()),
                final_output_keys=list(final_output.keys()),
            )
            return RunResult(
                data=final_output,
                status="phase_budget_exceeded",
                token_usage=self._state.token_usage,
                cost_usd=self._state.total_cost_usd or None,
                partial_data=final_output or None,
            )

        except BudgetExceeded as exc:
            # PR22: hard budget cap hit — surface the user-facing message
            # via the result's `error` (let the caller route to outbox).
            # FP-0005: also expose partial_data for parity with the loop /
            # phase-budget paths.
            final_output = self._fallback_final_output()
            self.events.emit(
                "workflow_terminated",
                reason=f"budget_exceeded: {exc.dimension}",
                total_phase_count=sum(self._state.visit_counts.values()),
                final_output_keys=list(final_output.keys()),
            )
            return RunResult(
                data=final_output,
                status="budget_exceeded",
                token_usage=self._state.token_usage,
                cost_usd=self._state.total_cost_usd or None,
                error=str(exc),
                partial_data=final_output or None,
            )

        except WorkflowAbortedError as exc:
            self.events.emit(
                "workflow_aborted",
                reason=str(exc),
                total_phase_count=sum(self._state.visit_counts.values()),
            )
            raise

        finally:
            # G11 fix (hypothesis A+B): close MCP clients in the same asyncio
            # task that opened them.  Deferring to GC lets the AsyncExitStack
            # be finalised from an unrelated context, which causes anyio
            # cancel-scope task-affinity RuntimeErrors in stderr.
            await self.control_ir_executor.teardown_mcp_clients()

            # R-D1: exception-aware completion. The finally clause must
            # distinguish between "this run is finished" and "this run was
            # interrupted and may need to resume on the next startup".
            #
            # complete() is called when the run reached its end state:
            #   - normal return (success / loop_limit / phase_budget /
            #     budget_exceeded — all caught above and returned as
            #     RunResult, so exc_type is None at this point)
            #   - WorkflowAbortedError — the skill itself decided to abort.
            #     Resume would just re-decide-to-abort.
            #
            # complete() is SKIPPED so the snapshot survives for resume on:
            #   - asyncio.CancelledError (Ctrl-C, /skill discard, parent
            #     task cancelled)
            #   - KeyboardInterrupt
            #   - generic Exception (transient blip / bug — auto-resume
            #     can retry; user can ``/skill discard <id>`` to give up)
            if self._skill_registry:
                import sys as _sys
                exc_type, _exc_val, _exc_tb = _sys.exc_info()
                if exc_type is None or issubclass(exc_type, WorkflowAbortedError):
                    await self._skill_registry.complete(run_id=self.run_id)
                else:
                    self.events.emit(
                        "skill_run_interrupted",
                        run_id=self.run_id,
                        exc_type=exc_type.__name__,
                        will_resume=True,
                    )
