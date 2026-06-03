"""PhaseExecutor — Layer 2 of OSRuntime decomposition.

Extracted from OSRuntime (FP-0020 Component C). Owns driving one phase
to completion via act/decide loops with retry.

Responsibilities:
- Phase-budget wall-clock enforcement (_check_phase_budget) — moved UP from
  the Component B shim in runtime.py._call_llm_and_record so that
  LLMCallRecorder has no dependency on phase_started_at. The check now
  happens before each LLM call at this layer.
- Act-turn loop until the LLM emits a decide-turn (_run_act_loop).
- Decide-turn validation with retry (_run_decide_with_retry).
- Single-attempt output validation (_validate_phase_output).
- Entry point: execute() composes the three methods above.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pydantic

from reyn.kernel.normalizer import (
    ControlIRValidationError,
    NormalizationError,
    NormalizationResult,
    normalize,
)
from reyn.kernel.runtime_types import (
    PhaseBudgetExceededError,
    WorkflowAbortedError,
    _normalize_artifact,
    _validate_artifact_structure,
)
from reyn.kernel.validation import ValidationError, validate_output
from reyn.safety.limit_handler import LimitDecision, handle_limit_exceeded
from reyn.schemas.models import ActOutput, CandidateOutput, LLMOutput
from reyn.workspace.artifact_validator import validate_artifact_data

if TYPE_CHECKING:
    from reyn.config import PhaseActResultsCompactionConfig
    from reyn.kernel.llm_call_recorder import LLMCallRecorder
    from reyn.kernel.run_state import RunState
    from reyn.schemas.models import Skill
    from reyn.services.compaction.engine import CompactionEngine
    from reyn.user_intervention import RequestBus

_log = logging.getLogger(__name__)

# FP-0008 #1135(b): inline cap for the raw LLM output captured on a phase-output
# validation failure. Larger payloads are offloaded (size axis, non-history) so
# the P6 events audit log never balloons.
_RAW_OUTPUT_INLINE_CAP = 8192


class _ControlIRResultsHolder:
    """Mutable handle to the act loop's ``control_ir_results`` accumulator (#1176).

    An on-demand ``compact`` op reaches it via ``OpContext.compact_now`` to
    compact + replace the accumulated results mid-batch. get/set only — the raw
    list is never exposed by index, so the OpContext callback stays opaque to
    the accumulator's storage shape.
    """

    def __init__(self, initial: "list[dict]") -> None:
        self._results: list[dict] = list(initial)

    def get(self) -> "list[dict]":
        return self._results

    def set(self, results: "list[dict]") -> None:
        self._results = list(results)


def _make_phase_compact_now(holder, engine, cfg, events, phase):
    """Build the phase-axis ``compact_now`` callback (#1176 B1).

    On-demand counterpart to the act loop's automatic compaction: it compacts
    the SAME older split (keep last ``recent_act_turns_raw`` raw, summarise the
    rest) via the SAME ``compact_control_ir_results`` primitive — so the emitted
    compaction events are shape-identical to the auto path (replay-consistent,
    lead-coder's safety requirement). Returns the chat-byte-identical contract
    ``{freed_tokens, free_window_after, free_window_before}`` in exact tokens.
    """

    async def _compact_now() -> dict:
        from reyn.services.compaction.engine import (
            compact_control_ir_results,
            estimate_tokens,
        )

        model = engine._model  # noqa: SLF001 — resolved litellm string owned by the engine

        def _free_window(results: "list[dict]") -> "tuple[int, int]":
            budgets = getattr(engine, "budgets", None)
            trigger = budgets.effective_trigger if budgets is not None else 0
            used = estimate_tokens(json.dumps(results, ensure_ascii=False), model)
            return trigger, used

        trigger, before_used = _free_window(holder.get())
        n_recent = cfg.recent_act_turns_raw
        results = holder.get()
        older = results[:-n_recent] if n_recent > 0 else results
        recent = results[-n_recent:] if n_recent > 0 else []
        if older:
            older_compacted = await compact_control_ir_results(
                older, engine=engine, cfg=cfg, events=events, phase=phase,
            )
            holder.set(older_compacted + recent)
        # else: nothing older to compact — no-op (still report the live window).
        _, after_used = _free_window(holder.get())
        return {
            "freed_tokens": max(0, before_used - after_used),
            "free_window_after": max(0, trigger - after_used),
            "free_window_before": max(0, trigger - before_used),
        }

    return _compact_now


class PhaseExecutor:
    """Drives one phase to completion via act/decide loops with retry.

    Extracted from OSRuntime._execute_phase, _run_act_loop,
    _run_decide_with_retry, _validate_phase_output (FP-0020 Component C).

    Constructor dependencies:
      llm_caller         — LLMCallRecorder (Component B); called for each LLM turn.
      control_ir_executor — ControlIRExecutor; executes act-turn ops.
      events             — EventLog; all state-change events emitted here.
      skill              — Skill definition (phases, permissions, graph).
      safety             — SafetyConfig; phase_seconds budget + on_limit policy.
      intervention_bus   — RequestBus | None; for safety-limit checkpoints.

    Public surface: execute() → (NormalizationResult, LLMOutput, retry_count).
    """

    def __init__(
        self,
        *,
        llm_caller: "LLMCallRecorder",
        control_ir_executor,
        events,
        skill: "Skill",
        safety,
        intervention_bus: "RequestBus | None",
        run_id: str | None = None,
        strict: bool = False,
        build_frame_fn,
        phase_compaction_engine: "CompactionEngine | None" = None,
        phase_compaction_cfg: "PhaseActResultsCompactionConfig | None" = None,
        op_loop_enabled: bool = False,
    ) -> None:
        self._llm_caller = llm_caller
        self._control_ir_executor = control_ir_executor
        self._events = events
        self._skill = skill
        self._safety = safety
        self._intervention_bus = intervention_bus
        self._run_id = run_id
        self._strict = strict
        # build_frame is owned by OSRuntime (accesses skill graph + resolver +
        # event history). PhaseExecutor receives it as a callable to avoid
        # pulling the full OSRuntime dependency graph into this module.
        self._build_frame = build_frame_fn
        # PR-N5: phase axis compaction engine + config.  Both optional; when
        # absent the compaction hook is skipped (= legacy behavior).
        # Path (b): lazy construction — PhaseExecutor holds references directly
        # rather than threading through OSRuntime constructor, keeping all PR-N5
        # wiring within the strictly-listed ALLOWED files.
        self._phase_compaction_engine: "CompactionEngine | None" = phase_compaction_engine
        self._phase_compaction_cfg: "PhaseActResultsCompactionConfig | None" = phase_compaction_cfg
        # PR-N5: last phase's final control_ir_results, set at end of
        # _run_act_loop so RunOrchestrator can snapshot them at A → B
        # transition (= `rollback_state.snapshot_phase_history`).
        self._last_control_ir_results: list[dict] = []
        # When True, this skill is opted into the native-tools op-loop — the phase
        # act-loop drives the SHARED ``RouterLoop.run_loop`` (the converged op-loop,
        # #1092): op results thread as native tool-role history and dispatch / memo /
        # compaction reuse RouterLoop. The OS decides the mechanism (P3); the gate is
        # a config-held list of skill names (``tool_calls_op_loop_skills``) resolved
        # at PhaseExecutor construction. Default False = json-mode act loop,
        # byte-for-byte unchanged. (#1092 PR-C-3 retired the transitional #1212
        # frame-fed ``_run_op_loop`` + its separate ``routerloop_convergence_skills``
        # gate; ``tool_calls_op_loop_skills`` is now the single op-loop gate and the
        # converged path is its implementation.)
        self._op_loop_enabled = op_loop_enabled

    # ── #1135(b): raw-output capture on phase-output validation failure ───────

    def _emit_output_validation_failed(
        self, *, phase: str, attempt: int, failure_kind: str, error: str, raw: dict,
    ) -> None:
        """Emit the additive `phase_output_validation_failed` event (#1135b).

        Captures the model's raw emitted output that failed validation into the
        always-on P6 audit log — otherwise it survives only in the opt-in
        REYN_LLM_TRACE_DUMP. Emitted ALONGSIDE the existing kind-specific
        validation event (which is left unchanged for TUI/test back-compat).

        Per #1135 canonical contract: inline ``raw_output`` when ≤ cap, else a
        ``raw_output_ref`` = state_dir-RELATIVE offload handle (the offload value
        returns an absolute ref; converted to relative explicitly so the ref is
        portable in the long-lived audit log). Exactly one of raw_output /
        raw_output_ref is non-null. No hash field.
        """
        serialized = json.dumps(raw, ensure_ascii=False)
        raw_output: str | None = None
        raw_output_ref: str | None = None
        if len(serialized.encode("utf-8")) <= _RAW_OUTPUT_INLINE_CAP:
            raw_output = serialized
        else:
            from reyn.services.offload import offload_value
            state_dir = self._control_ir_executor.workspace.state_dir
            res = offload_value(serialized, store_dir=state_dir / "control_ir_offload")
            raw_output_ref = str(Path(res.path_ref).relative_to(state_dir))
        self._events.emit(
            "phase_output_validation_failed",
            phase=phase,
            attempt=attempt,
            failure_kind=failure_kind,
            error=error,
            raw_output=raw_output,
            raw_output_ref=raw_output_ref,
        )

    # ── Phase-budget enforcement (moved up from Component B shim) ─────────────

    async def _check_phase_budget(
        self,
        phase_name: str,
        state: "RunState",
    ) -> None:
        """Wall-clock budget check before each LLM call.

        Raises PhaseBudgetExceededError when over budget, unless the
        safety.on_limit policy approves continuation.

        Behavioral note (FP-0020-C): this method was previously called inside
        OSRuntime._call_llm_and_record (Component B shim). Moving it here means
        the check runs at the PhaseExecutor layer — BEFORE passing to
        LLMCallRecorder.call() — so LLMCallRecorder has no dependency on
        phase_started_at. Observable timing is identical: the check fires
        immediately before each LLM call.
        """
        budget = self._safety.timeout.phase_seconds
        if not budget or state.phase_started_at is None:
            return
        elapsed = state.elapsed_phase_seconds()
        effective_budget = state.effective_phase_budget(budget)
        if elapsed <= effective_budget:
            return

        decision: LimitDecision = await handle_limit_exceeded(
            bus=self._intervention_bus,
            on_limit=self._safety.on_limit,
            kind="phase_seconds",
            run_id=self._run_id or "",
            prompt=(
                f"Phase {phase_name!r} ran for {elapsed:.1f}s, exceeding "
                f"the {effective_budget:.1f}s budget. Allow longer?"
            ),
            detail=f"phase={phase_name} elapsed={elapsed:.2f} budget={effective_budget:.2f}",
            extension_amount=float(budget),
        )
        if decision.allow_continue:
            state.grant_extension("phase_seconds", decision.extension)
            state.reset_phase_clock()
            self._events.emit(
                "safety_limit_checkpoint",
                kind="phase_seconds",
                allow_continue=True,
                reason=decision.reason,
                extension=decision.extension,
            )
            return

        self._events.emit(
            "safety_limit_checkpoint",
            kind="phase_seconds",
            allow_continue=False,
            reason=decision.reason,
            extension=decision.extension,
        )
        self._events.emit(
            "phase_budget_exceeded",
            phase=phase_name, elapsed=elapsed, budget=effective_budget,
        )
        raise PhaseBudgetExceededError(phase_name, elapsed, effective_budget)

    # ── Single-attempt output validation ──────────────────────────────────────

    def _validate_phase_output(
        self,
        raw: dict,
        current_phase: str,
        candidates: list[CandidateOutput],
        allowed_next: list[str],
        state: "RunState",
        input_artifact: dict | None = None,
        attempt: int = 0,
    ) -> tuple[NormalizationResult, LLMOutput]:
        """Normalize and validate one LLM response.

        Returns (result, output) on success.
        Raises WorkflowAbortedError for abort (non-retryable).
        Raises ValueError for any retryable validation failure.

        Extracted from OSRuntime._validate_phase_output; state is now an
        explicit parameter (previously accessed via self._state).
        """
        candidate_map = {c.next_phase: c for c in candidates}

        try:
            result = normalize(raw, allowed_next)
        except ControlIRValidationError as exc:
            self._events.emit("control_ir_validation_error", phase=current_phase, error=str(exc))
            self._emit_output_validation_failed(
                phase=current_phase, attempt=attempt, failure_kind="control_ir",
                error=str(exc), raw=raw,
            )
            raise ValueError(str(exc)) from exc
        except NormalizationError as exc:
            self._events.emit("normalization_error", phase=current_phase, error=str(exc))
            self._emit_output_validation_failed(
                phase=current_phase, attempt=attempt, failure_kind="normalization",
                error=str(exc), raw=raw,
            )
            raise ValueError(str(exc)) from exc

        self._events.emit(
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
            self._events.emit("validation_error", phase=current_phase, error=str(exc))
            self._emit_output_validation_failed(
                phase=current_phase, attempt=attempt, failure_kind="artifact_structure",
                error=str(exc), raw=raw,
            )
            raise

        # P7-clean: the OS supplies the generic context dict; only the
        # skill's schema names specific keys.
        validation_context: dict | None = None
        if input_artifact is not None or state.skill_input is not None:
            validation_context = {}
            if input_artifact is not None:
                validation_context["input"] = input_artifact
            if state.skill_input is not None:
                validation_context["skill_input"] = state.skill_input

        norm_data, corrections, errors = validate_artifact_data(
            normalized,
            matched_candidate.artifact_schema,
            strict=self._strict,
            validation_context=validation_context,
        )
        self._events.emit(
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
            self._events.emit("validation_error", phase=current_phase, error=error_str)
            self._emit_output_validation_failed(
                phase=current_phase, attempt=attempt, failure_kind="artifact_data",
                error=error_str, raw=raw,
            )
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
            self._events.emit("validation_error", phase=current_phase, error=msg)
            self._emit_output_validation_failed(
                phase=current_phase, attempt=attempt, failure_kind="ops_structure",
                error=msg, raw=raw,
            )
            raise ValueError(msg) from exc

        try:
            validate_output(output, candidates)
        except ValidationError as exc:
            self._events.emit("validation_error", phase=current_phase, error=str(exc))
            self._emit_output_validation_failed(
                phase=current_phase, attempt=attempt, failure_kind="output_validation",
                error=str(exc), raw=raw,
            )
            raise ValueError(str(exc)) from exc

        return result, output

    # ── Converged op loop (#1092 PR-B, RouterLoop.run_loop) ──────────────────────

    async def _run_routerloop_op_loop(
        self,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        max_act_turns: int,
        max_phase_retries: int,
        artifact_path: str | None,
        state: "RunState",
        rollback_context: dict | None = None,
    ) -> tuple[dict, list[dict]]:
        """#1092 (FD1, ADR-0036): the CONVERGED op-loop — the phase act-loop drives
        the SHARED ``RouterLoop.run_loop`` (true convergence, ii). This is THE
        native-tools op-loop, reached for skills opted into ``tool_calls_op_loop_skills``
        (#1092 PR-C-3 retired the transitional #1212 phase-native frame-fed
        ``_run_op_loop`` + its separate ``routerloop_convergence_skills`` gate).

        It builds a ``RouterLoop`` with a ``PhaseRouterLoopHost`` and drives its
        extracted ``run_loop``, so op results thread as NATIVE ``{assistant,tool_calls}``
        + ``{tool,...}`` message-history, and dispatch / memo / compaction reuse the
        shared loop. Phase ops dispatch via RouterLoop's ``REGISTRY_DISPATCH_TOOLS``
        registry path (op-exec seam obviated by #1240, ADR-0036); the phase
        ``OpContext`` is provisioned by ``host.make_router_op_context`` (= ``_build_ctx``)
        so permission/sandbox gates enforce identically to ``control_ir_executor.execute``.

        FD2 (P1/P8): the transition decide is a SEPARATE structured-json ``call``
        AFTER ``run_loop`` returns at end_turn — it is NOT in the loop. Chat-specific
        terminals inside ``run_loop`` (put_outbox spawn-acks / text reply) go inert
        for the phase host (no-op ``put_outbox``, ``async_count == 0`` because phase
        op kinds are not async ``dispatch_kind``). Gated (``op_loop_enabled``, from
        ``tool_calls_op_loop_skills``); un-opted skills are byte unchanged.
        """
        import json as _json

        from reyn.chat.router_loop import RouterLoop
        from reyn.kernel.phase_router_host import PhaseRouterLoopHost

        phase_def = self._skill.phases.get(phase)
        allowed_ops = set(phase_def.allowed_ops) if phase_def is not None else set()
        decl = self._skill.permissions
        sandbox = phase_def.default_sandbox_policy if phase_def is not None else None
        cie = self._control_ir_executor

        host = PhaseRouterLoopHost(
            control_ir_executor=cie,
            events=self._events,
            phase=phase,
            decl=decl,
            allowed_ops=allowed_ops,
            default_sandbox_policy=sandbox,
            agent_name=self._skill.name,
            agent_role=(phase_def.role if phase_def is not None else phase),
            output_language=output_language,
            resolve_model_fn=lambda name: cie._resolver.resolve(name).model,
            # #1092 PR-C-4b: wire the phase compaction engine/cfg so the host's
            # per-turn ``maybe_compact_messages`` hook can proactively bound the
            # converged op-loop's in-loop message-history (json-mode parity).
            compaction_engine=self._phase_compaction_engine,
            compaction_cfg=self._phase_compaction_cfg,
            # #1092 PR-C-5 (2): wire per-turn phase wall-clock budget enforcement so
            # the converged op-loop limit-checks each turn like _run_act_loop (the
            # host's ``check_phase_budget`` hook → this bound _check_phase_budget).
            check_phase_budget_fn=lambda: self._check_phase_budget(phase, state),
        )
        tools = host.get_phase_op_catalog()
        # P6 audit + the distinguishing marker for the converged op-loop (vs the
        # json-mode act loop) — consumed by the full-path test and the dogfood
        # host-polymorphism trace.
        self._events.emit(
            "phase_routerloop_op_loop_started",
            phase=phase,
            tool_count=len(tools),
        )

        prior_results = (
            list(rollback_context["previous_control_ir_results"])
            if rollback_context and rollback_context.get("previous_control_ir_results")
            else []
        )
        seed_frame = self._build_frame(
            phase, artifact, candidates, output_language,
            control_ir_results=prior_results,
            artifact_path=artifact_path,
            remaining_act_turns=max_act_turns,
        )
        messages = self._llm_caller.build_phase_op_loop_messages(
            phase=phase, frame=seed_frame,
        )
        # #1092 PR-C-5 (3): surface the rollback REASON to the converged op-loop.
        # json-mode's _run_act_loop injects it into the act frame (via call_llm's
        # rollback_context, llm.py); the converged op-loop turns (call_llm_tools) do
        # NOT carry rollback_context — only the FD2 decide does. Append the rejection
        # feedback as a seed user turn (same wording as json-mode) so the op-loop
        # ADAPTS its op-gathering to the feedback, not just the final decide. The
        # restored prior_results (above) already prevent redo; this adds the "why".
        if rollback_context and rollback_context.get("reason"):
            messages.append({
                "role": "user",
                "content": (
                    f"Your previous output was rolled back by "
                    f"[{rollback_context.get('rollback_from', '?')}]: "
                    f"{rollback_context['reason']}\n"
                    "Please revise your output to address the feedback."
                ),
            })

        routerloop = RouterLoop(
            host=host,
            chain_id=self._run_id or phase,
            # #1092 PR-C-5 (4): use the EFFECTIVE act-turn cap (resume-adjusted), not
            # the raw max_act_turns, so the converged op-loop force-closes
            # json-mode-equally (mirrors _run_act_loop's state.effective_act_turn_cap).
            max_iterations=state.effective_act_turn_cap(phase, max_act_turns),
            # cosmetic: the phase llm_caller resolves the phase model itself.
            router_model=phase,
            budget=getattr(state, "budget", None),
            memo_provider=self._llm_caller.make_phase_memo_provider(
                phase=phase, state=state,
            ),
            llm_caller=self._llm_caller.make_phase_llm_caller(
                phase=phase, state=state,
            ),
        )
        # Drive the SHARED op-execution loop. Op results thread into ``messages``
        # as native tool-role turns; the model signals end_turn by emitting no
        # tool_calls, returning control here for the FD2 decide.
        await routerloop.run_loop(messages, tools, False)

        # FD2: build the SEPARATE json decide frame from the native turns so it is
        # INFORMATION-EQUIVALENT to the json-mode act loop's decide frame (P1/P8 —
        # transition post-pended here, never inside run_loop). Two structural pieces
        # the json-mode decide frame relies on (their absence is the #1092 dogfood
        # reliability regression: a context-inadequate decide frame, not a model-axis
        # limit — the weak model fumbles the decide because it sees LESS than json-mode):
        #   (a) act_turn_reasoning — the model's inline content from the native
        #       assistant turns (reasoning continuity across act turns), and
        #   (b) control_ir_results in the RAW op-result shape — unwrapping
        #       dispatch_tool's {"status": "ok", "data": <op result>} envelope so the
        #       outcomes render the same way the json-mode op-loop renders them.
        control_ir_results: list[dict] = list(prior_results)
        act_turn_reasoning: list[str] = []
        for _m in messages:
            _role = _m.get("role")
            if _role == "assistant":
                _ac = _m.get("content")
                if isinstance(_ac, str) and _ac:
                    act_turn_reasoning.append(_ac)
            elif _role == "tool":
                _content = _m.get("content")
                try:
                    _parsed = _json.loads(_content)
                except (TypeError, ValueError):
                    _parsed = {"result": _content}
                if (
                    isinstance(_parsed, dict)
                    and "data" in _parsed
                    and set(_parsed) <= {"status", "data", "error"}
                ):
                    _parsed = _parsed["data"]
                if not isinstance(_parsed, dict):
                    # #1092 PR-C-0: normalized op results are not always dicts — e.g.
                    # read_file is unwrapped to bare-content (a str) by
                    # _normalise_router_tool_result. control_ir_results is typed
                    # list[dict] (ContextFrame), so wrap non-dict outcomes. (Masked on
                    # main while the eager-discovery AttributeError killed dispatch
                    # before any real op result existed; the host fix unmasks it.)
                    _parsed = {"result": _parsed}
                control_ir_results.append(_parsed)
        if self._phase_compaction_cfg is not None:
            _keep = self._phase_compaction_cfg.recent_act_turns_raw
            act_turn_reasoning = act_turn_reasoning[-_keep:]
        # #1092 PR-C-4a: phase-axis compaction for the converged op-loop. Recovers
        # the AUTOMATIC control_ir_results compaction the json-mode _run_act_loop
        # does (and which the retired frame-fed _run_op_loop did — tested by the
        # now-deleted compaction_1212; this is the C-4a coverage-recovery). When the
        # accumulated op results exceed the recent-raw window, summarise the OLDER
        # results via the SHARED ``compact_control_ir_results`` (CompactionEngine)
        # before the FD2 decide frame — same older/recent split + best-effort
        # (never raises; LLM error → phase_act_results_compaction_failed + identity)
        # as _run_act_loop, emitting ``phase_act_results_compacted``. Phase-layer
        # only (RouterLoop untouched → chat byte-identical). NOTE: this bounds the
        # DECIDE-frame results; in-loop message-history bounding is the separate
        # (B) concern (sufficiency-verified against RouterLoop retry-shrink).
        if (
            self._phase_compaction_engine is not None
            and self._phase_compaction_cfg is not None
            and len(control_ir_results) > self._phase_compaction_cfg.recent_act_turns_raw
        ):
            from reyn.services.compaction.engine import compact_control_ir_results
            n_recent = self._phase_compaction_cfg.recent_act_turns_raw
            recent = control_ir_results[-n_recent:]
            older = control_ir_results[:-n_recent]
            older_compacted = await compact_control_ir_results(
                older,
                engine=self._phase_compaction_engine,
                cfg=self._phase_compaction_cfg,
                events=self._events,
                phase=phase,
            )
            control_ir_results = older_compacted + recent
        self._last_control_ir_results = control_ir_results

        decide_frame = self._build_frame(
            phase, artifact, candidates, output_language,
            control_ir_results=control_ir_results,
            artifact_path=artifact_path,
            remaining_act_turns=0,
            force_decide=True,
            act_turn_reasoning=act_turn_reasoning,
        )
        raw = await self._llm_caller.call(
            phase, decide_frame, None, rollback_context, state,
        )
        return raw, []

    # ── Act loop ──────────────────────────────────────────────────────────────

    async def _run_act_loop(
        self,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        max_act_turns: int,
        max_phase_retries: int,
        artifact_path: str | None,
        state: "RunState",
        rollback_context: dict | None = None,
    ) -> tuple[dict, list[dict]]:
        """Drive act turns until the LLM emits a decide turn.

        Returns (raw_decide_response, accumulated_prior_attempts).
        """
        # PR-N5 rollback history restore. When this phase is being re-entered
        # via rollback from a later phase, run_orchestrator populates
        # rollback_context["previous_control_ir_results"] with the snapshot
        # taken at the prior A → B transition. Restoring it lets the LLM resume
        # with its prior op observations + the rollback reason (= already in
        # rollback_context["reason"]) instead of re-running the same grep/file_read.
        if rollback_context and rollback_context.get("previous_control_ir_results"):
            control_ir_results: list[dict] = list(
                rollback_context["previous_control_ir_results"]
            )
        else:
            control_ir_results = []
        prior_attempts: list[dict[str, str]] = []
        act_turn_count = 0
        first_call = True

        while True:
            if prior_attempts:
                self._events.emit(
                    "phase_retry", phase=phase,
                    attempt=len(prior_attempts), max_retries=max_phase_retries,
                    error=prior_attempts[-1]["error"],
                )

            remaining = max_act_turns - act_turn_count if max_act_turns > 0 else None
            force_decide = remaining is not None and remaining <= 0

            # PR-N5: phase axis compaction. When accumulated control_ir_results
            # would push the next prompt over the model's effective context
            # budget, summarise the OLDER results (= keep last
            # cfg.recent_act_turns_raw raw, summarise the rest). Best-effort:
            # never raises; LLM error falls through to a
            # `phase_act_results_compaction_failed` event and the un-compacted
            # prompt.
            if (
                self._phase_compaction_engine is not None
                and self._phase_compaction_cfg is not None
                and len(control_ir_results) > self._phase_compaction_cfg.recent_act_turns_raw
            ):
                from reyn.services.compaction.engine import (
                    compact_control_ir_results,
                )
                n_recent = self._phase_compaction_cfg.recent_act_turns_raw
                recent = control_ir_results[-n_recent:]
                older = control_ir_results[:-n_recent]
                older_compacted = await compact_control_ir_results(
                    older,
                    engine=self._phase_compaction_engine,
                    cfg=self._phase_compaction_cfg,
                    events=self._events,
                    phase=phase,
                )
                control_ir_results = older_compacted + recent

            frame = self._build_frame(
                phase, artifact, candidates, output_language,
                control_ir_results=control_ir_results,
                artifact_path=artifact_path,
                remaining_act_turns=remaining,
                force_decide=force_decide,
            )

            # Phase-budget check before each LLM call (moved up from Component B shim)
            await self._check_phase_budget(phase, state)

            raw = await self._llm_caller.call(
                phase, frame, prior_attempts or None,
                rollback_context if first_call else None,
                state,
            )
            first_call = False

            if raw.get("type") != "act":
                # PR-N5: persist final control_ir_results so RunOrchestrator
                # can snapshot them via self._last_control_ir_results at the
                # A → B transition (= rollback_state.snapshot_phase_history).
                self._last_control_ir_results = control_ir_results
                return raw, prior_attempts

            act_turn_count += 1
            effective_max_act_turns = state.effective_act_turn_cap(phase, max_act_turns)
            if act_turn_count > effective_max_act_turns:
                if force_decide:
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
                        self._events.emit("phase_failed", phase=phase,
                                          attempts=len(prior_attempts), final_error=final_msg)
                        raise ValueError(final_msg)
                    continue

                # FP-0005: ask before raising. On approval, extension is
                # recorded on state and the loop continues.
                decision = await handle_limit_exceeded(
                    bus=self._intervention_bus,
                    on_limit=self._safety.on_limit,
                    kind=f"max_act_turns:{phase}",
                    run_id=self._run_id or "",
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
                    state.grant_extension(f"max_act_turns:{phase}", decision.extension)
                    self._events.emit(
                        "safety_limit_checkpoint",
                        kind=f"max_act_turns:{phase}",
                        allow_continue=True,
                        reason=decision.reason,
                        extension=decision.extension,
                    )
                    continue
                self._events.emit(
                    "safety_limit_checkpoint",
                    kind=f"max_act_turns:{phase}",
                    allow_continue=False,
                    reason=decision.reason,
                    extension=decision.extension,
                )
                msg = (
                    f"Phase '{phase}' exceeded max act turns ({effective_max_act_turns}). "
                    "The LLM kept emitting act turns without making a decide turn."
                )
                self._events.emit("phase_failed", phase=phase,
                                  attempts=act_turn_count, final_error=msg)
                raise ValueError(msg)

            # #1240 Wave 2b (A)-alias: the phase frame advertises chat names
            # ("invoke_skill" / "call_mcp_tool") as op kinds.  Rewrite those
            # names back to execution op kinds ("run_skill" / "mcp") BEFORE
            # ActOutput.model_validate so the ControlIROp discriminated union
            # resolves correctly.  The raw dict is shallow-copied so the
            # original is untouched (needed for error reporting below).
            from reyn.op_runtime.registry import _PHASE_TOOL_NAME_ALIAS
            if raw.get("type") == "act" and isinstance(raw.get("ops"), list):
                ops_list = raw["ops"]
                if any(
                    isinstance(op, dict)
                    and op.get("kind") in _PHASE_TOOL_NAME_ALIAS
                    for op in ops_list
                ):
                    raw = {
                        **raw,
                        "ops": [
                            {**op, "kind": _PHASE_TOOL_NAME_ALIAS[op["kind"]]}
                            if isinstance(op, dict) and op.get("kind") in _PHASE_TOOL_NAME_ALIAS
                            else op
                            for op in ops_list
                        ],
                    }
            try:
                act = ActOutput.model_validate(raw)
            except pydantic.ValidationError as exc:
                self._emit_output_validation_failed(
                    phase=phase, attempt=len(prior_attempts), failure_kind="act_ops",
                    error=str(exc), raw=raw,
                )
                prior_attempts.append({"raw": json.dumps(raw, ensure_ascii=False), "error": str(exc)})
                if len(prior_attempts) > max_phase_retries:
                    self._events.emit("phase_failed", phase=phase,
                                      attempts=len(prior_attempts), final_error=str(exc))
                    raise ValueError(
                        f"Phase '{phase}' failed after {len(prior_attempts)} attempt(s): {exc}"
                    ) from exc
                continue

            phase_def = self._skill.phases.get(phase)
            phase_decl = self._skill.permissions
            allowed_ops = set(phase_def.allowed_ops) if phase_def is not None else None
            # #1176 B1: expose on-demand voluntary compaction to the batch's ops.
            # The holder carries the accumulator across the execute() boundary so
            # a `compact` op can compact + replace the settled results mid-batch;
            # we read it back afterward. compact_now is None (→ compact op
            # fail-louds) when no phase compaction engine is wired.
            _holder = _ControlIRResultsHolder(control_ir_results)
            _compact_now = (
                _make_phase_compact_now(
                    _holder, self._phase_compaction_engine,
                    self._phase_compaction_cfg, self._events, phase,
                )
                if self._phase_compaction_engine is not None
                and self._phase_compaction_cfg is not None
                else None
            )
            ir_results = await self._control_ir_executor.execute(
                act.ops, phase=phase, decl=phase_decl, allowed_ops=allowed_ops,
                default_sandbox_policy=(
                    phase_def.default_sandbox_policy if phase_def is not None else None
                ),
                compact_now=_compact_now,
            )
            # _holder reflects any mid-batch on-demand compaction; the new
            # results append after the (possibly compacted) settled accumulator.
            control_ir_results = _holder.get() + ir_results
            prior_attempts = []
            self._events.emit(
                "act_executed",
                phase=phase,
                op_count=len(act.ops),
                op_kinds=[op.kind for op in act.ops],
                act_turn=act_turn_count,
                ops=[op.model_dump() for op in act.ops],
                results=ir_results,
            )

    # ── Decide loop with retry ────────────────────────────────────────────────

    async def _run_decide_with_retry(
        self,
        raw: dict,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        prior_attempts: list[dict[str, str]],
        max_phase_retries: int,
        state: "RunState",
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """Validate a decide-turn response, retrying on rejection.

        Returns (result, output, retry_count).
        """
        allowed_next = [c.next_phase for c in candidates]

        while True:
            try:
                result, output = self._validate_phase_output(
                    raw, phase, candidates, allowed_next, state, input_artifact=artifact,
                    attempt=len(prior_attempts),
                )
                return result, output, len(prior_attempts)
            except WorkflowAbortedError:
                raise
            except ValueError as exc:
                prior_attempts.append({"raw": json.dumps(raw, ensure_ascii=False), "error": str(exc)})
                if len(prior_attempts) > max_phase_retries:
                    self._events.emit(
                        "phase_failed", phase=phase,
                        attempts=len(prior_attempts), final_error=str(exc),
                    )
                    raise ValueError(
                        f"Phase '{phase}' failed after {len(prior_attempts)} attempt(s): {exc}"
                    ) from exc

                self._events.emit(
                    "phase_retry", phase=phase,
                    attempt=len(prior_attempts), max_retries=max_phase_retries,
                    error=prior_attempts[-1]["error"],
                )
                frame = self._build_frame(phase, artifact, candidates, output_language)
                # Phase-budget check before each retry LLM call
                await self._check_phase_budget(phase, state)
                raw = await self._llm_caller.call(phase, frame, prior_attempts, None, state)

    # ── Public entry point ────────────────────────────────────────────────────

    async def execute(
        self,
        phase: str,
        artifact: dict,
        candidates: list[CandidateOutput],
        output_language: str | None,
        max_phase_retries: int,
        state: "RunState",
        artifact_path: str | None = None,
        rollback_context: dict | None = None,
    ) -> tuple[NormalizationResult, LLMOutput, int]:
        """Drive one phase to completion via act/decide loops with retry.

        Act loop → decide loop → return (result, output, retry_count).
        Phase-budget is checked before each LLM call within act and decide loops.
        """
        phase_def = self._skill.phases[phase]
        max_act_turns = phase_def.max_act_turns if phase_def.max_act_turns > 0 else 10

        # Gated dispatch. Both return (raw_decide, prior) and feed the same
        # decide-with-retry path (transition stays json-mode = FD2).
        #   op-loop (gate ``tool_calls_op_loop_skills``): the converged op-loop —
        #     the phase drives the shared ``RouterLoop.run_loop`` (#1092).
        #   default: json-mode act loop, byte-for-byte unchanged.
        if self._op_loop_enabled:
            act_loop = self._run_routerloop_op_loop
        else:
            act_loop = self._run_act_loop
        raw, prior_attempts = await act_loop(
            phase, artifact, candidates, output_language,
            max_act_turns, max_phase_retries, artifact_path,
            state,
            rollback_context=rollback_context,
        )
        return await self._run_decide_with_retry(
            raw, phase, artifact, candidates, output_language,
            prior_attempts, max_phase_retries, state,
        )
