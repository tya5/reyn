from __future__ import annotations
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, TYPE_CHECKING
import pydantic
from reyn.schemas.models import ActOutput, Skill, CandidateOutput, ContextFrame, LLMOutput
from reyn.budget.budget import BudgetExceeded, format_refusal_message, format_warn_message
if TYPE_CHECKING:
    from reyn.budget.budget import BudgetTracker
    from reyn.events.state_log import StateLog
    from reyn.skill.skill_registry import SkillRegistry
from reyn.events.events import EventLog
from reyn.workspace.workspace import Workspace
from reyn.config import LimitsConfig
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.kernel.validation import validate_output, ValidationError
from reyn.dispatch.dispatcher import _compute_llm_args_hash, _lookup_memoized_step
from reyn.llm.llm import call_llm
from reyn.llm.pricing import TokenUsage, estimate_cost
from reyn.kernel.normalizer import normalize, NormalizationError, NormalizationResult, ControlIRValidationError
from reyn.workspace.artifact_validator import validate_artifact_data
from reyn.llm.model_resolver import ModelResolver
from reyn.permissions.permissions import PermissionResolver
from reyn.context_builder import build_frame
from reyn.skill.skill_node_runner import execute_skill_node
from reyn.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.kernel.postprocessor_executor import PostprocessorExecutor, PostprocessorError
from reyn.user_intervention import InterventionBus


class LoopLimitExceededError(Exception):
    pass


class PhaseBudgetExceededError(Exception):
    """Raised when a phase exceeds its wall-clock budget (limits.phase.max_wall_seconds)."""
    def __init__(self, phase: str, elapsed: float, budget: float) -> None:
        super().__init__(
            f"Phase '{phase}' exceeded wall-clock budget: {elapsed:.2f}s > {budget:.3g}s"
        )
        self.phase = phase
        self.elapsed = elapsed
        self.budget = budget


class WorkflowAbortedError(Exception):
    pass


@dataclass
class RunResult:
    """Typed return value of OSRuntime.run() and Agent.run()."""
    data: dict[str, Any]
    status: Literal["finished", "loop_limit_exceeded", "phase_budget_exceeded", "budget_exceeded"]
    token_usage: TokenUsage | None = None
    cost_usd: float | None = None
    error: str | None = None

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


@dataclass
class RollbackState:
    """All rollback-specific bookkeeping for a single OSRuntime run.

    OSRuntime owns the run, this owns the rollback machinery — kept here so
    the four-or-five fields that exist purely to support rollback don't
    pollute OSRuntime's instance namespace.

    Field semantics:
      phase_inputs[phase]  — the artifact the phase was entered with
                             (used to restore on rollback into that phase)
      phase_outputs[phase] — the artifact the phase last produced
                             (used as `rejected_artifact` for the next iteration)
      phase_prev[phase]    — the phase that was the predecessor when this phase
                             was last entered (used to walk back on rollback)
      pending_ctx          — single-shot rollback_ctx for the next _execute_phase
      no_progress_check    — single-shot sentinel: if the rolled-back-into phase
                             re-produces the rejected output, abort
    """
    phase_inputs: dict[str, dict] = field(default_factory=dict)
    phase_outputs: dict[str, dict] = field(default_factory=dict)
    phase_prev: dict[str, str | None] = field(default_factory=dict)
    pending_ctx: dict | None = None
    no_progress_check: dict | None = None

    # ── recording (called by OSRuntime as it advances) ──

    def record_input(self, phase: str, artifact: dict) -> None:
        self.phase_inputs[phase] = artifact

    def record_output(self, phase: str, artifact: dict) -> None:
        self.phase_outputs[phase] = artifact

    def record_predecessor(self, phase: str, prev: str | None) -> None:
        self.phase_prev[phase] = prev

    # ── reading (used to restore on rollback) ──

    def get_input(self, phase: str) -> dict:
        return self.phase_inputs[phase]

    def get_predecessor(self, phase: str) -> str | None:
        return self.phase_prev.get(phase)

    # ── rollback transition ──

    def begin_rollback(self, from_phase: str, to_phase: str, reason: str) -> dict:
        """Set up state for the upcoming re-run of `to_phase`.

        Captures the rollback context (rejected output + reason + caller phase)
        and arms the no-progress sentinel. Returns the rollback context for
        callers that want to log or inspect it; OSRuntime normally consumes it
        via `take_pending_ctx()` on the next iteration.
        """
        rejected = self.phase_outputs.get(to_phase, {})
        ctx = {
            "rejected_artifact": rejected,
            "reason": reason,
            "rollback_from": from_phase,
        }
        self.pending_ctx = ctx
        self.no_progress_check = {
            "phase": to_phase,
            "prev_output_data": rejected.get("data"),
            "rollback_from": from_phase,
        }
        return ctx

    def take_pending_ctx(self) -> dict | None:
        """One-shot read+clear of the rollback context."""
        ctx = self.pending_ctx
        self.pending_ctx = None
        return ctx

    def consume_no_progress(self, phase: str, output_data: Any) -> str | None:
        """Check & clear the no-progress sentinel for `phase`.

        If `phase` is the one we just rolled into and `output_data` matches the
        previously-rejected output, returns the original rollback_from (the
        caller should abort with a no-progress error).

        If `phase` doesn't match the sentinel, leaves it alone — a different
        phase may yet visit this check. If `phase` matches but the output
        differs, clears the sentinel (rollback succeeded; the check has
        served its purpose).
        """
        if self.no_progress_check is None:
            return None
        if self.no_progress_check["phase"] != phase:
            return None
        if output_data == self.no_progress_check["prev_output_data"]:
            rollback_from = self.no_progress_check.get("rollback_from", "?")
            self.no_progress_check = None
            return rollback_from
        self.no_progress_check = None
        return None


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
        limits: LimitsConfig | None = None,
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
        self._limits = limits or LimitsConfig()
        self._max_phase_visits = self._limits.phase.max_visits  # 0 = unlimited
        self._max_phase_wall_seconds = self._limits.phase.max_wall_seconds  # 0 = unlimited
        self._llm_timeout = self._limits.llm.timeout
        self._llm_max_retries = self._limits.llm.max_retries
        self._prompt_cache_enabled = prompt_cache_enabled
        # Public attributes — readable by tests / introspection. Treated as
        # immutable post-construction.
        self.project_context = project_context
        self.agent_role = agent_role
        # Private aliases retained so existing internal call sites stay stable.
        self._project_context = project_context
        self._agent_role = agent_role
        self._phase_started_at: float | None = None
        # PR33: trusted source for cross-field validation. Set in run() to
        # the initial input artifact and never overwritten — phases that
        # are LLM-authored cannot tamper with this. Validators reference it
        # via x-reyn-members-of: skill_input.data.X paths to enforce that
        # an LLM-emitted field's value is in a list it cannot fabricate.
        self._skill_input: dict | None = None
        self._perm = permission_resolver
        self._intervention_bus = intervention_bus
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
        )
        self._history: list[str] = []
        self._visit_counts: dict[str, int] = {}
        self._token_usage: TokenUsage = TokenUsage()
        self._total_cost_usd: float = 0.0
        self._prev_phase: str | None = None          # phase that transitioned to current
        self._rollback = RollbackState()
        # R-D2: per-phase counter for LLM op_invocation_id. Each `_call_llm_and_record`
        # uses the current value as `{phase}.llm.{idx}` then increments. Resets on
        # `_enter_phase` so resume reproduces the original sequence.
        self._llm_call_idx_in_phase: int = 0

    # ── Phase setup ────────────────────────────────────────────────────────────

    def _enter_phase(self, phase_name: str, artifact: dict) -> None:
        max_visits = self._max_phase_visits
        count = self._visit_counts.get(phase_name, 0)
        if max_visits and count >= max_visits:
            self.events.emit("loop_limit_exceeded", phase=phase_name, visit_count=count, max=max_visits)
            raise LoopLimitExceededError(
                f"Phase '{phase_name}' reached max_phase_visits={max_visits}"
            )
        self._visit_counts[phase_name] = count + 1
        # Reset wall-clock timer for this visit. Soft budget checks at retry/turn boundaries
        # compare elapsed against limits.phase.max_wall_seconds (0 = unlimited).
        self._phase_started_at = time.monotonic()
        # R-D2: reset the per-phase LLM invocation counter. Resume relies on
        # this resetting deterministically — the in-flight phase is re-entered
        # from the start, so its first LLM call must look up `{phase}.llm.0`
        # (matching what the original run recorded).
        self._llm_call_idx_in_phase = 0
        self.events.emit(
            "phase_started", phase=phase_name,
            visit_count=count + 1, input_artifact_type=artifact.get("type"),
        )

    def _check_phase_budget(self, phase_name: str) -> None:
        """Soft wall-clock check. Raises PhaseBudgetExceededError when over budget."""
        budget = self._max_phase_wall_seconds
        if not budget or self._phase_started_at is None:
            return
        elapsed = time.monotonic() - self._phase_started_at
        if elapsed > budget:
            self.events.emit(
                "phase_budget_exceeded",
                phase=phase_name, elapsed=elapsed, budget=budget,
            )
            raise PhaseBudgetExceededError(phase_name, elapsed, budget)

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
        if self._prev_phase is not None:
            candidates.append(CandidateOutput(
                next_phase="rollback",
                control_type="rollback",
                schema_name="rollback",
                artifact_schema={},
                description=(
                    f"Reject the output from '{self._prev_phase}' and send it back for revision. "
                    "Use when the current phase determines the preceding phase produced invalid output. "
                    "Put the rejection reason in control.reason.summary. "
                    "next_phase MUST be null. decision MUST be 'continue'."
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
        output_language: str,
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
            history=self._history,
            visit_counts=self._visit_counts,
            finish_criteria=self.skill.finish_criteria,
            max_phase_visits=self._max_phase_visits or None,
            available_ops=effective_ops,
            op_catalog=all_ops,
            effective_model=effective_model,
            model_resolved=self._resolver.resolve(effective_model),
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
        if input_artifact is not None or self._skill_input is not None:
            validation_context = {}
            if input_artifact is not None:
                validation_context["input"] = input_artifact
            if self._skill_input is not None:
                validation_context["skill_input"] = self._skill_input
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
        self._check_phase_budget(phase)
        resolved_model = self._resolver.resolve(self._effective_model(phase))
        phase_def = self.skill.phases.get(phase)

        # R-D2: per-phase LLM op_invocation_id + memoization. The counter is
        # per-phase-visit (reset in `_enter_phase`) so resume reproduces the
        # original call sequence deterministically.
        op_invocation_id = f"{phase}.llm.{self._llm_call_idx_in_phase}"
        self._llm_call_idx_in_phase += 1

        # Compute args_hash regardless of resume_plan presence — when state_log
        # is configured, every call writes a step_completed so a future resume
        # can hit. The hash is also the memo key on resume.
        args_hash = _compute_llm_args_hash(
            model=resolved_model,
            frame=frame.model_dump(mode="json"),
            prior_attempts=prior_attempts,
            rollback_context=rollback_context,
            system_inputs={
                "skill_name": self.skill.name,
                "skill_description": self.skill.description,
                "phase_role": phase_def.role if phase_def else None,
                "project_context": self._project_context,
                "agent_role": self._agent_role,
            },
        )

        # Memo lookup (resume only). On hit, return the recorded LLM result
        # without invoking call_llm — saves cost + preserves determinism.
        if self._resume_plan is not None:
            memo = _lookup_memoized_step(
                self._resume_plan, op_invocation_id, phase, args_hash,
            )
            if memo is not None:
                memoized = self._extract_memoized_llm_result(
                    memo, phase=phase, op_invocation_id=op_invocation_id,
                )
                if memoized is not None:
                    # R-D8 L3: forward-calc the budget. Memo hit means the
                    # LLM was NOT actually called (cost = $0), but for cap
                    # enforcement to track total intended spend across crash
                    # boundaries, we still credit the recorded usage to the
                    # tracker. No pre-check (memo hits don't refuse — they
                    # already happened in the original run).
                    self._credit_budget_from_memo(
                        memo,
                        resolved_model=resolved_model,
                        phase=phase,
                        op_invocation_id=op_invocation_id,
                    )
                    self.events.emit(
                        "step_memoized",
                        run_id=self.run_id,
                        phase=phase,
                        op_invocation_id=op_invocation_id,
                        op_kind="llm",
                        args_hash=args_hash,
                    )
                    return memoized
                # else: corrupt memo result → fall through to fresh call

        # Normal call path
        self._check_budget_pre_llm(resolved_model)
        self.events.emit("llm_called", phase=phase, model=resolved_model)
        llm_result = await call_llm(
            resolved_model, frame,
            prior_attempts=prior_attempts or None,
            rollback_context=rollback_context,
            timeout=self._llm_timeout,
            max_retries=self._llm_max_retries,
            prompt_cache_enabled=self._prompt_cache_enabled,
            skill_name=self.skill.name,
            skill_description=self.skill.description,
            phase_role=phase_def.role if phase_def else None,
            project_context=self._project_context,
            agent_role=self._agent_role,
        )
        raw = llm_result.data
        cost_usd: float | None = None
        pricing_snapshot: dict | None = None
        if llm_result.usage:
            self._token_usage += llm_result.usage
            cost_usd, pricing_snapshot = estimate_cost(resolved_model, llm_result.usage)
            if cost_usd is not None:
                self._total_cost_usd += cost_usd
            self._record_budget_post_llm(resolved_model, llm_result.usage)
        self.events.emit(
            "llm_response_received",
            phase=phase,
            response_type=raw.get("type"),
            raw=raw,
            prompt_tokens=llm_result.usage.prompt_tokens if llm_result.usage else None,
            completion_tokens=llm_result.usage.completion_tokens if llm_result.usage else None,
            cost_usd=cost_usd,
            pricing_snapshot=pricing_snapshot,
        )

        # R-D2: emit step_completed so a future resume can memo-hit. Defensive
        # try/except — never fail the dispatch on a WAL emit failure (parallel
        # to dispatch_tool's _wal_step_completed pattern).
        # R-D8 L2: record usage so future resume's memo hit can re-credit budget.
        await self._wal_step_completed_for_llm(
            phase=phase,
            op_invocation_id=op_invocation_id,
            args_hash=args_hash,
            result=raw,
            usage=llm_result.usage.to_dict() if llm_result.usage else None,
        )

        return raw

    def _credit_budget_from_memo(
        self,
        memo: object,
        *,
        resolved_model: str,
        phase: str,
        op_invocation_id: str,
    ) -> None:
        """R-D8 L3: re-credit the budget tracker from a memoized LLM step.

        On memo hit the LLM was not actually called, but cap enforcement
        across crash needs the tracker to reflect what the original run
        spent.

        **Suppressed when the BudgetTracker has loaded its persisted state**
        (R-D8 L4 + L5): the loaded state already includes every committed
        step's usage, so re-crediting here would double-count. In
        production both paths run (state load + forward calc); the flag
        check resolves the overlap in favor of the persisted state.

        ``memo.usage`` (a dict from TokenUsage.to_dict) provides the
        recorded counts when forward calc is taken. None usage
        (pre-R-D8 step) → log + skip (graceful undercount, no error).
        """
        if self._budget_tracker is None:
            return
        # Skip when persisted state is the source of truth
        if getattr(self._budget_tracker, "_state_loaded", False):
            return
        usage_dict = getattr(memo, "usage", None)
        if not usage_dict:
            import logging
            logging.getLogger(__name__).debug(
                "memo hit (run=%s phase=%s id=%s) has no usage data; "
                "skipping budget credit (pre-R-D8 step or LLM returned "
                "no usage)",
                self.run_id, phase, op_invocation_id,
            )
            return
        usage = TokenUsage.from_dict(usage_dict)
        # Update local accumulators (mirror the fresh-call path)
        self._token_usage += usage
        cost_usd, _ = estimate_cost(resolved_model, usage)
        if cost_usd is not None:
            self._total_cost_usd += cost_usd
        # Credit the shared BudgetTracker
        self._record_budget_post_llm(resolved_model, usage)

    def _extract_memoized_llm_result(
        self,
        memo: object,
        *,
        phase: str,
        op_invocation_id: str,
    ) -> dict | None:
        """Return the recorded LLM response dict, or None on schema mismatch.

        Schema versioning gate: a CommittedStep.result that's not a dict is
        treated as corrupt (e.g. version skew where the format changed). We
        log a warning and let the caller fall through to a fresh call_llm.

        R-D10: when the recorded result is a ``{"_ref": "<file>"}``
        placeholder, transparently resolve it by reading the file under
        ``<agent_state_dir>/skills/<run_id>_llm_results/``. A missing
        or malformed ref returns None, which triggers fall-through to
        a fresh LLM call (= memo unavailable, retry).
        """
        result = getattr(memo, "result", None)
        if not isinstance(result, dict):
            import logging
            logging.getLogger(__name__).warning(
                "LLM memo result is not a dict (run=%s phase=%s id=%s); "
                "falling through to fresh call",
                self.run_id, phase, op_invocation_id,
            )
            return None
        # R-D10: resolve ref placeholders to their full payload.
        if (self._skill_registry is not None and self.run_id is not None
                and list(result.keys()) == ["_ref"]):
            from reyn.skill import llm_result_ref
            resolved = llm_result_ref.resolve(
                agent_state_dir=self._skill_registry.state_dir,
                run_id=self.run_id,
                value=result,
            )
            if resolved is None:
                # ref file missing / malformed → treat as memo miss
                return None
            if not isinstance(resolved, dict):
                import logging
                logging.getLogger(__name__).warning(
                    "LLM memo ref resolved to non-dict (run=%s phase=%s id=%s)",
                    self.run_id, phase, op_invocation_id,
                )
                return None
            return resolved
        return result

    async def _wal_step_completed_for_llm(
        self,
        *,
        phase: str,
        op_invocation_id: str,
        args_hash: str,
        result: dict,
        usage: dict | None = None,
    ) -> None:
        """Append step_completed for an LLM call. Defensive: log + swallow.

        ``usage`` (R-D8 L2) records the TokenUsage so a future resume's
        memo hit can re-credit the budget tracker via record_llm. None
        when the LLM call returned no usage info (rare; most providers
        always include tokens).

        R-D10: large results (> 32 KB serialized) are off-loaded to
        ``<agent_state_dir>/skills/<run_id>_llm_results/<args_hash>.json``
        and the WAL stores ``{"_ref": "<args_hash>.json"}``. Memo
        lookup transparently resolves the ref. Avoids MB-class inline
        payloads from accumulating in the WAL between phase truncations.
        """
        if self._state_log is None or self.run_id is None:
            return
        # R-D10: maybe off-load large result to a workspace ref file.
        wal_result = result
        if self._skill_registry is not None:
            from reyn.skill import llm_result_ref
            wal_result = llm_result_ref.write_if_large(
                agent_state_dir=self._skill_registry.state_dir,
                run_id=self.run_id,
                args_hash=args_hash,
                result=result,
            )
        try:
            await self._state_log.append(
                "step_completed",
                run_id=self.run_id,
                phase=phase,
                op_invocation_id=op_invocation_id,
                op_kind="llm",
                args_hash=args_hash,
                result=wal_result,
                usage=usage,
            )
        except Exception as e:  # noqa: BLE001 — never fail the dispatch
            import logging
            logging.getLogger(__name__).warning(
                "WAL step_completed (llm) emission failed (run=%s phase=%s id=%s): %s",
                self.run_id, phase, op_invocation_id, e,
            )

    # ── Budget hooks (PR22) ─────────────────────────────────────────────

    def _budget_agent_name(self) -> str | None:
        """Extract the agent name from caller (`agents/<name>` → `<name>`).

        Returns None when caller is `direct` (no agent context, per-agent
        budget noop). The chain_id key is unchanged either way.
        """
        if self._caller and self._caller.startswith("agents/"):
            return self._caller.split("/", 1)[1]
        return None

    def _check_budget_pre_llm(self, model: str) -> None:
        if self._budget_tracker is None:
            return
        agent = self._budget_agent_name()
        check = self._budget_tracker.check_pre_llm(model=model, agent=agent)
        if not check.allowed:
            self.events.emit(
                "budget_exceeded",
                dimension=check.hard_dimension,
                detail=check.detail,
                agent=agent,
                chain_id=self._chain_id,
            )
            raise BudgetExceeded(
                check.hard_dimension or "budget",
                format_refusal_message(check, agent=agent),
            )
        for dim in check.warn_dimensions:
            self.events.emit(
                "budget_warn",
                dimension=dim,
                agent=agent,
                chain_id=self._chain_id,
                **check.context,
            )

    def _record_budget_post_llm(self, model: str, usage: TokenUsage) -> None:
        if self._budget_tracker is None:
            return
        agent = self._budget_agent_name()
        check = self._budget_tracker.record_llm(
            model=model, agent=agent, usage=usage,
            chain_id=self._chain_id, skill=self._budget_skill_name,
        )
        for dim in check.warn_dimensions:
            self.events.emit(
                "budget_warn",
                dimension=dim,
                agent=agent,
                chain_id=self._chain_id,
                **check.context,
            )

    async def _run_act_loop(
        self,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str,
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
            if act_turn_count > max_act_turns:
                if force_decide:
                    # LLM emitted ops despite being told no more are allowed.
                    # Don't execute the ops — feed back an explicit error and retry.
                    act_turn_count -= 1
                    prior_attempts.append({
                        "raw": json.dumps(raw, ensure_ascii=False),
                        "error": (
                            f"You emitted act-turn ops but your act budget is exhausted "
                            f"({max_act_turns}/{max_act_turns} act turns used). "
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
                msg = (
                    f"Phase '{phase}' exceeded max act turns ({max_act_turns}). "
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
        output_language: str,
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
        output_language: str,
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
        output_language: str,
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
            limits=self._limits,
        )
        self._token_usage += usage
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
        target = self._prev_phase
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
        self._rollback.begin_rollback(current_phase, target, reason_summary)
        self._history.append(f"{current_phase} → rollback → {target}")
        return target, self._rollback.get_input(target), self._rollback.get_predecessor(target)

    # ── Workflow termination ──────────────────────────────────────────────────

    async def _finish_workflow(
        self,
        phase: str,
        data: dict,
        reason: str,
        confidence: float,
        finish_artifact: dict | None = None,
        output_language: str = "ja",
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
            self._token_usage += post_usage
            data = post_artifact.get("data", {})

        self.events.emit(
            "workflow_finished",
            phase=phase,
            reason=reason,
            confidence=confidence,
            total_phase_count=sum(self._visit_counts.values()),
            final_output_keys=list(data.keys()),
        )
        return RunResult(
            data=data, status="finished",
            token_usage=self._token_usage,
            cost_usd=self._total_cost_usd or None,
        )

    # ── Skill-node dispatch (transition to a sub-skill node) ───────────────────

    async def _apply_skill_node(
        self,
        node_id: str,
        current_phase: str,
        output_artifact: dict,
        output_language: str,
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
            self._history.append(f"{current_phase} → {node_id} → END")
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
        self._history.append(f"{current_phase} → {node_id} → {next_after}")
        return next_after, adapted

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run(
        self,
        initial_input: dict,
        output_language: str = "ja",
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

        current_phase = self.skill.entry_phase
        artifact = initial_input
        # PR33: pin the trusted input for cross-field validation across all
        # phases. Schemas downstream can reference fields here that no LLM
        # phase can tamper with.
        self._skill_input = initial_input

        self.events.emit(
            "workflow_started",
            run_id=self.run_id,
            skill=self.skill.name,
            entry_phase=self.skill.entry_phase,
            input_type=artifact.get("type"),
            default_model=self._resolver.resolve(self.model),
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
            self._visit_counts = dict(self._resume_plan.visit_counts)
            self._history = list(self._resume_plan.phases_visited)
            # R-D2: pre-decrement visit_count for the resumed phase so that
            # the upcoming `_enter_phase` increment lands on the SAME count
            # the original run had at the time the LLM was called. Without
            # this, the in-flight phase's first LLM call sees visit_count =
            # recorded + 1, the args_hash differs from what was recorded,
            # and memo lookup misses every time (silent cost duplication).
            if current_phase in self._visit_counts and \
                    self._visit_counts[current_phase] > 0:
                self._visit_counts[current_phase] -= 1
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
                visit_counts=dict(self._visit_counts),
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
            self._enter_phase(current_phase, artifact)
            if self._skill_registry:
                await self._skill_registry.advance_phase(
                    run_id=self.run_id,
                    next_phase=current_phase,
                    last_phase_artifact_path=artifact_path,
                )

            while True:
                rollback_context = self._rollback.take_pending_ctx()

                # Store the pre-preprocessor artifact for rollback.
                # On rollback, the preprocessor re-runs deterministically from this snapshot —
                # semantically correct, but costly for heavy chains (iterate × run_app).
                # If eval rollback causes N-item re-evaluation, revisit caching here (Phase 5+).
                self._rollback.record_input(current_phase, artifact)

                candidates = self._build_candidates(current_phase)

                # Run preprocessor (deterministic enrichment) before handing artifact to LLM
                phase_def = self.skill.phases[current_phase]
                if phase_def.preprocessor:
                    enriched_artifact, pre_usage = await self._preprocessor.run(
                        phase_def, artifact, output_language
                    )
                    self._token_usage += pre_usage
                    # Update artifact_path to the enriched file so maybe_ref_artifact
                    # references the correct (post-preprocessor) artifact when it is large.
                    artifact_path = self.workspace.store_artifact(
                        current_phase + "_preprocessed", enriched_artifact,
                        skill_name=self.skill.name,
                        visit=self._visit_counts.get(current_phase, 1),
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
                    current_phase, artifact, self._prev_phase = self._handle_rollback(
                        current_phase, result.control.reason.summary,
                    )
                    artifact_path = None
                    self._enter_phase(current_phase, artifact)
                    if self._skill_registry:
                        await self._skill_registry.advance_phase(
                            run_id=self.run_id,
                            next_phase=current_phase,
                            last_phase_artifact_path=artifact_path,
                        )
                    continue

                # No-progress detection: if this phase was just re-run after a rollback
                # and produced an output structurally identical to the rejected one, abort.
                rollback_from = self._rollback.consume_no_progress(
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

                self._rollback.record_output(current_phase, output.artifact)

                artifact_path = self.workspace.store_artifact(
                    current_phase, output.artifact,
                    skill_name=self.skill.name,
                    visit=self._visit_counts.get(current_phase, 1),
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
                    self._history.append(f"{current_phase} → END")
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
                    self._prev_phase = current_phase
                    self._rollback.record_predecessor(next_after, current_phase)
                    current_phase = next_after
                    artifact = adapted
                else:
                    self._history.append(f"{current_phase} → {next_node}")
                    self._prev_phase = current_phase
                    self._rollback.record_predecessor(next_node, current_phase)
                    current_phase = next_node
                    artifact = output.artifact
                self._enter_phase(current_phase, artifact)
                if self._skill_registry:
                    await self._skill_registry.advance_phase(
                        run_id=self.run_id,
                        next_phase=current_phase,
                        last_phase_artifact_path=artifact_path,
                    )

        except LoopLimitExceededError as exc:
            final_output = self._fallback_final_output()
            self.events.emit(
                "workflow_terminated",
                reason=str(exc),
                total_phase_count=sum(self._visit_counts.values()),
                final_output_keys=list(final_output.keys()),
            )
            return RunResult(data=final_output, status="loop_limit_exceeded", token_usage=self._token_usage, cost_usd=self._total_cost_usd or None)

        except PhaseBudgetExceededError as exc:
            final_output = self._fallback_final_output()
            self.events.emit(
                "workflow_terminated",
                reason=str(exc),
                total_phase_count=sum(self._visit_counts.values()),
                final_output_keys=list(final_output.keys()),
            )
            return RunResult(data=final_output, status="phase_budget_exceeded", token_usage=self._token_usage, cost_usd=self._total_cost_usd or None)

        except BudgetExceeded as exc:
            # PR22: hard budget cap hit — surface the user-facing message
            # via the result's `error` (let the caller route to outbox).
            final_output = self._fallback_final_output()
            self.events.emit(
                "workflow_terminated",
                reason=f"budget_exceeded: {exc.dimension}",
                total_phase_count=sum(self._visit_counts.values()),
                final_output_keys=list(final_output.keys()),
            )
            return RunResult(
                data=final_output,
                status="budget_exceeded",
                token_usage=self._token_usage,
                cost_usd=self._total_cost_usd or None,
                error=str(exc),
            )

        except WorkflowAbortedError as exc:
            self.events.emit(
                "workflow_aborted",
                reason=str(exc),
                total_phase_count=sum(self._visit_counts.values()),
            )
            raise

        finally:
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
