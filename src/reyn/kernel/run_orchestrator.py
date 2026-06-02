"""RunOrchestrator — Layer 1 of OSRuntime decomposition.

Extracted from OSRuntime (FP-0020 Component D, final extraction).
Owns phase sequence + transitions + rollback dispatch + skill-node
dispatch + resume setup + SkillRegistry lifecycle + exception handling.

This is the final layer in the 4-component decomposition:
  RunOrchestrator (D, this file) — phase sequence + lifecycle
    ↓
  PhaseExecutor (C, phase_executor.py) — act/decide loop
    ↓
  LLMCallRecorder (B, llm_call_recorder.py) — LLM call + WAL + budget
    ↓
  RunState (A, run_state.py) — mutable run-scope state
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from reyn.budget.budget import BudgetExceeded
from reyn.kernel.runtime_types import (
    LoopLimitExceededError,
    PhaseBudgetExceededError,
    RunResult,
    WorkflowAbortedError,
)
from reyn.safety.limit_handler import (
    handle_limit_exceeded,
    reset_run_extensions,
)
from reyn.skill.skill_node_runner import execute_skill_node

if TYPE_CHECKING:
    from reyn.config import SafetyConfig
    from reyn.events.events import EventLog
    from reyn.kernel.phase_executor import PhaseExecutor
    from reyn.kernel.preprocessor_executor import PreprocessorExecutor
    from reyn.kernel.run_state import RunState
    from reyn.permissions.permissions import PermissionResolver
    from reyn.schemas.models import CandidateOutput, Skill
    from reyn.skill.skill_registry import SkillRegistry
    from reyn.user_intervention import RequestBus
    from reyn.workspace.workspace import Workspace

_log = logging.getLogger(__name__)


class RunOrchestrator:
    """Phase sequence, transitions, rollback dispatch, skill-node dispatch,
    resume setup, SkillRegistry lifecycle, and exception handling.

    Extracted from OSRuntime.run() and its supporting methods (FP-0020
    Component D). OSRuntime becomes a wiring layer that constructs and
    delegates to this class.

    Constructor dependencies:
      phase_executor     — PhaseExecutor (Component C); drives one phase.
      skill              — Skill definition (phases, permissions, graph).
      workspace          — Workspace; artifact storage + workspace I/O.
      events             — EventLog; all state-change events emitted here.
      skill_registry     — SkillRegistry | None; crash-recovery snapshots.
      preprocessor       — PreprocessorExecutor; deterministic enrichment.
      state              — RunState (Component A); mutable run-scope state.
      safety             — SafetyConfig; limit policies.
      intervention_bus   — RequestBus | None; safety-limit checkpoints.
      resume_plan        — ResumePlan | None; for forward-replay on resume.
      run_id             — str | None; identifies this run.
      parent_run_id      — str | None; for nested skill runs.
      build_candidates_fn — callable: (current_phase) → list[CandidateOutput].
      enter_phase_fn     — callable: (phase_name, artifact) → None (async); used
                           for _enter_phase. Passed from OSRuntime so that
                           subclasses can override _enter_phase on OSRuntime and
                           have it take effect here (same pattern as build_frame_fn).
      execute_phase_fn   — callable: (phase, artifact, candidates, output_language,
                           max_phase_retries, state, artifact_path, rollback_context)
                           → (result, output, retry_count) (async); used for
                           _execute_phase. Passed from OSRuntime so subclasses can
                           override _execute_phase on OSRuntime.
      perm               — PermissionResolver | None; permission enforcement.
      resolver_model_fn  — callable: (model) → resolved; for workflow_started.
      model              — default model name (for skill-node dispatch).
      strict             — strict validation flag.
      subscribers        — event subscribers (for PostprocessorExecutor).
      state_log          — StateLog | None.
      caller             — str; caller identifier.
      max_phase_visits   — int; safety loop limit.

    Public surface: run() → RunResult.
    """

    def __init__(
        self,
        *,
        phase_executor: "PhaseExecutor",
        skill: "Skill",
        workspace: "Workspace",
        events: "EventLog",
        skill_registry: "SkillRegistry | None",
        preprocessor: "PreprocessorExecutor",
        state: "RunState",
        safety: "SafetyConfig",
        intervention_bus: "RequestBus | None",
        resume_plan: Any,
        run_id: str | None,
        parent_run_id: str | None,
        build_candidates_fn: Callable,
        enter_phase_fn: Callable,
        execute_phase_fn: Callable,
        perm: "PermissionResolver | None",
        resolver_model_fn: Callable,
        resolver,
        model: str,
        strict: bool,
        subscribers: list | None,
        state_log: Any,
        caller: str,
        max_phase_visits: int,
        budget_tracker: object | None = None,  # #1190 stage (ii): skill_node_adapt cost recording
        tool_calls_op_loop_skills: list[str] | None = None,  # #1212: gate, propagated to sub-skills
    ) -> None:
        self._budget_tracker = budget_tracker
        self._tool_calls_op_loop_skills = list(tool_calls_op_loop_skills or [])
        self._phase_executor = phase_executor
        self._skill = skill
        self._workspace = workspace
        self._events = events
        self._skill_registry = skill_registry
        self._preprocessor = preprocessor
        self._state = state
        self._safety = safety
        self._intervention_bus = intervention_bus
        self._resume_plan = resume_plan
        self._run_id = run_id
        self._parent_run_id = parent_run_id
        self._build_candidates = build_candidates_fn
        self._enter_phase_fn = enter_phase_fn
        self._execute_phase_fn = execute_phase_fn
        self._perm = perm
        self._resolver_model_fn = resolver_model_fn
        self._resolver = resolver
        self._model = model
        self._strict = strict
        self._subscribers = subscribers
        self._state_log = state_log
        self._caller = caller
        self._max_phase_visits = max_phase_visits
        self._on_limit = safety.on_limit

    # ── Safety-limit checkpoint ────────────────────────────────────────────────

    async def _handle_limit_checkpoint(
        self,
        *,
        kind: str,
        prompt: str,
        detail: str,
        extension_amount: float,
    ) -> Any:
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
            run_id=self._run_id or "",
            prompt=prompt,
            detail=detail,
            extension_amount=extension_amount,
        )
        if decision.allow_continue:
            self._state.grant_extension(kind, decision.extension)
        self._events.emit(
            "safety_limit_checkpoint",
            kind=kind,
            allow_continue=decision.allow_continue,
            reason=decision.reason,
            extension=decision.extension,
        )
        return decision

    # ── Phase entry ────────────────────────────────────────────────────────────

    async def _enter_phase(self, phase_name: str, artifact: dict) -> None:
        max_visits = self._max_phase_visits
        effective_max = self._state.effective_visit_cap(max_visits)
        count = self._state.visit_counts.get(phase_name, 0)
        if effective_max and count >= effective_max:
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
                self._events.emit(
                    "loop_limit_exceeded",
                    phase=phase_name, visit_count=count, max=effective_max,
                )
                raise LoopLimitExceededError(
                    f"Phase '{phase_name}' reached max_phase_visits={effective_max}. "
                    f"→ Raise {LoopLimitExceededError.hint_config_key} to allow "
                    f"more iterations."
                )
            # Approved — fall through; effective_max has already been
            # bumped via safety_extensions and will be picked up on
            # the next visit.
        new_count = self._state.begin_phase(phase_name)
        self._events.emit(
            "phase_started", phase=phase_name,
            visit_count=new_count, input_artifact_type=artifact.get("type"),
        )

    # ── Fallback ───────────────────────────────────────────────────────────────

    def _fallback_final_output(self) -> dict:
        for entry in reversed(self._workspace.artifacts):
            art = entry["artifact"]
            if art.get("type") == self._skill.final_output_name:
                return art.get("data", {})
        if self._workspace.artifacts:
            return self._workspace.artifacts[-1]["artifact"].get("data", {})
        return {}

    # ── Skill-node dispatch ────────────────────────────────────────────────────

    async def _run_skill_node(
        self,
        node_id: str,
        input_artifact: dict,
        target_schema: dict,
        target_type: str,
        output_language: str | None,
    ) -> dict:
        node_spec = self._skill.graph.skill_nodes[node_id]
        adapted, usage = await execute_skill_node(
            node_id=node_id,
            node_spec=node_spec,
            input_artifact=input_artifact,
            target_schema=target_schema,
            target_type=target_type,
            output_language=output_language,
            model=self._model,
            strict=self._strict,
            subscribers=self._events.subscribers,
            resolver=self._resolver,
            events=self._events,
            safety=self._safety,
            recorder=self._budget_tracker,
            tool_calls_op_loop_skills=self._tool_calls_op_loop_skills,  # #1212 sub-skill gate
            # #1190 stage (iii) Part 4: attribute skill_node adaptation to the
            # run's agent (caller "agents/<name>" → "<name>").
            recorder_agent=(
                self._caller.split("/", 1)[1]
                if self._caller and self._caller.startswith("agents/")
                else None
            ),
        )
        self._state.add_usage(usage, None)
        return adapted

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
        post_nodes = self._skill.graph.transitions.get(node_id, [])
        if not post_nodes:
            adapted = await self._run_skill_node(
                node_id, output_artifact,
                self._skill.final_output_schema, self._skill.final_output_name,
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
        next_phase_obj = self._skill.phases[next_after]
        adapted = await self._run_skill_node(
            node_id, output_artifact,
            next_phase_obj.input_schema, next_phase_obj.input_schema_name,
            output_language,
        )
        self._state.history.append(f"{current_phase} → {node_id} → {next_after}")
        return next_after, adapted

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
        self._events.emit(
            "phase_rollback",
            rollback_from=current_phase,
            rollback_to=target,
            reason=reason_summary,
        )
        ctx = self._state.rollback.begin_rollback(current_phase, target, reason_summary)
        # PR-N5: inject the target phase's prior control_ir_results snapshot
        # into the rollback context so PhaseExecutor can restore them at the
        # top of _run_act_loop.  Falls through to empty list (= current
        # behavior) when no snapshot exists (first rollback to this phase).
        snapshot = self._state.rollback.get_snapshot(target)
        if snapshot is not None:
            ctx["previous_control_ir_results"] = snapshot
        self._state.history.append(f"{current_phase} → rollback → {target}")
        return target, self._state.rollback.get_input(target), self._state.rollback.get_predecessor(target)

    # ── Workflow termination ───────────────────────────────────────────────────

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
        if self._skill.postprocessor is not None and finish_artifact is not None:
            # ── Step 1: persist finish artifact before postprocessor starts ────
            artifact_path: str | None = None
            if not resume_plan:
                artifact_path = self._workspace.store_artifact(
                    "__post__", finish_artifact,
                    skill_name=self._skill.name, visit=1,
                )
                # ── Step 2: advance snapshot to __post__ ──────────────────────
                if self._skill_registry:
                    await self._skill_registry.advance_phase(
                        run_id=self._run_id,
                        next_phase="__post__",
                        last_phase_artifact_path=artifact_path,
                    )

            from reyn.kernel.postprocessor_executor import PostprocessorExecutor
            post_executor = PostprocessorExecutor(
                skill=self._skill,
                workspace=self._workspace,
                events=self._events,
                model=self._model,
                resolver=self._resolver_model_fn,
                subscribers=self._events.subscribers,
                permission_resolver=self._perm,
                intervention_bus=self._intervention_bus,
                max_phase_visits=self._max_phase_visits,
                caller=self._caller,
                state_log=self._state_log,
                skill_run_id=self._run_id,
            )
            post_artifact, post_usage = await post_executor.run(
                finish_artifact, output_language, resume_plan=resume_plan,
            )
            self._state.add_usage(post_usage, None)
            data = post_artifact.get("data", {})

        self._events.emit(
            "workflow_finished",
            run_id=self._run_id,
            skill=self._skill.name,
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

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(
        self,
        initial_input: dict,
        output_language: str | None,
        max_phase_retries: int,
    ) -> RunResult:
        """Top-level entry — phase loop + resume fast-forward + lifecycle.

        Execute the workflow from entry_phase to completion.

        max_phase_retries: retries per phase on validation failure (default 2 = 3 total attempts).
        Returns RunResult with status="finished" or status="loop_limit_exceeded".
        Raises WorkflowAbortedError on unrecoverable LLM abort.
        """
        if self._perm:
            # B49 W2-S5 fix (2026-05-22): pass intervention_bus as-is; it
            # may be None in non-interactive contexts (= preprocessor
            # sub-skill runs invoked via ``run_skill`` op from inside
            # ``iterate`` / ``run_op``). ``startup_guard`` handles the
            # None case: if all permissions are already approved it
            # returns early without using the bus; if unapproved
            # permissions are found it raises ``RuntimeError`` with a
            # clear message. This removes the pre-check that blocked
            # sub-skills invoked from preprocessors whenever
            # permission_resolver was set.
            await self._perm.startup_guard(
                self._skill, self._skill.name, self._intervention_bus,
            )

        # FP-0005: reset auto_extend bookkeeping for this run, so the
        # ``auto_extend_times`` budget is fresh per-run (not per-process).
        if self._run_id:
            reset_run_extensions(self._run_id)

        current_phase = self._skill.entry_phase
        artifact = initial_input
        # PR33: pin the trusted input for cross-field validation across all
        # phases. Schemas downstream can reference fields here that no LLM
        # phase can tamper with.
        self._state.skill_input = initial_input

        self._events.emit(
            "workflow_started",
            run_id=self._run_id,
            skill=self._skill.name,
            entry_phase=self._skill.entry_phase,
            input_type=artifact.get("type"),
            default_model=self._resolver_model_fn(self._model).model,
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
                    # FP-0008 #1115 Stage 0: last_phase_artifact_path is a
                    # state_dir-relative handle; resolve it via the OS so resume
                    # works regardless of base_dir/state_dir coupling.
                    p = self._workspace.resolve_artifact_handle(artifact_path)
                    if p.is_file():
                        artifact = _json.loads(p.read_text(encoding="utf-8"))
                except Exception as e:  # noqa: BLE001 — defensive
                    _log.warning(
                        "resume: cannot load last_phase_artifact_path %s: %s",
                        artifact_path, e,
                    )
            self._events.emit(
                "skill_resumed",
                run_id=self._run_id,
                resume_phase=current_phase,
                visit_counts=dict(self._state.visit_counts),
            )

        if self._skill_registry:
            await self._skill_registry.start(
                run_id=self._run_id,
                skill_name=self._skill.name,
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
                    # FP-0008 #1115 Stage 0: resolve the state_dir-relative
                    # handle via the OS (see the in-flight resume branch above).
                    p = self._workspace.resolve_artifact_handle(artifact_path_post)
                    if p.is_file():
                        finish_artifact_post = _json.loads(
                            p.read_text(encoding="utf-8")
                        )
                except Exception as _e:  # noqa: BLE001 — defensive
                    _log.warning(
                        "__post__ resume: cannot load finish artifact %s: %s",
                        artifact_path_post, _e,
                    )

            if finish_artifact_post is None:
                _log.warning(
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
                        await self._skill_registry.complete(run_id=self._run_id)
                    else:
                        self._events.emit(
                            "skill_run_interrupted",
                            run_id=self._run_id,
                            exc_type=exc_type.__name__ if exc_type else "unknown",
                            will_resume=True,
                        )

        artifact_path: str | None = self._workspace.store_artifact(
            "_input", artifact, skill_name=self._skill.name, visit=1
        )

        try:
            await self._enter_phase_fn(current_phase, artifact)
            if self._skill_registry:
                await self._skill_registry.advance_phase(
                    run_id=self._run_id,
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
                phase_def = self._skill.phases[current_phase]
                if phase_def.preprocessor:
                    enriched_artifact, pre_usage = await self._preprocessor.run(
                        phase_def, artifact, output_language,
                        skill_input=self._state.skill_input,
                    )
                    self._state.add_usage(pre_usage, None)
                    # Update artifact_path to the enriched file so maybe_ref_artifact
                    # references the correct (post-preprocessor) artifact when it is large.
                    artifact_path = self._workspace.store_artifact(
                        current_phase + "_preprocessed", enriched_artifact,
                        skill_name=self._skill.name,
                        visit=self._state.visit_counts.get(current_phase, 1),
                    )
                else:
                    enriched_artifact = artifact

                result, output, retry_count = await self._execute_phase_fn(
                    current_phase, enriched_artifact, candidates, output_language, max_phase_retries,
                    artifact_path=artifact_path,
                    rollback_context=rollback_context,
                )

                current_def = self._skill.phases.get(current_phase)
                current_decl = self._skill.permissions
                current_allowed = set(current_def.allowed_ops) if current_def is not None else None
                # control_ir_executor is accessed via phase_executor's control_ir_executor
                decide_results = await self._phase_executor._control_ir_executor.execute(
                    output.ops, phase=current_phase, decl=current_decl,
                    allowed_ops=current_allowed,
                    default_sandbox_policy=(
                        current_def.default_sandbox_policy if current_def is not None else None
                    ),
                )
                if decide_results:
                    self._events.emit(
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
                    await self._enter_phase_fn(current_phase, artifact)
                    if self._skill_registry:
                        await self._skill_registry.advance_phase(
                            run_id=self._run_id,
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
                    self._events.emit(
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

                # PR-N5: snapshot the current phase's final control_ir_results
                # so that a future rollback back to this phase can restore the
                # LLM's prior op observations (grep matches, file_read content,
                # etc.) instead of re-running those ops from scratch.
                self._state.rollback.snapshot_phase_history(
                    current_phase,
                    self._phase_executor._last_control_ir_results,  # noqa: SLF001
                )

                artifact_path = self._workspace.store_artifact(
                    current_phase, output.artifact,
                    skill_name=self._skill.name,
                    visit=self._state.visit_counts.get(current_phase, 1),
                )

                self._events.emit(
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
                if next_node in self._skill.graph.skill_nodes:
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
                await self._enter_phase_fn(current_phase, artifact)
                if self._skill_registry:
                    await self._skill_registry.advance_phase(
                        run_id=self._run_id,
                        next_phase=current_phase,
                        last_phase_artifact_path=artifact_path,
                    )

        except LoopLimitExceededError as exc:
            # FP-0005: surface the last completed artifact via partial_data
            # so callers can render "here's what we have so far" UX. data
            # is also populated for backward compat with legacy callers.
            final_output = self._fallback_final_output()
            self._events.emit(
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
            self._events.emit(
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
            self._events.emit(
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
            self._events.emit(
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
            await self._phase_executor._control_ir_executor.teardown_mcp_clients()

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
                    await self._skill_registry.complete(run_id=self._run_id)
                else:
                    self._events.emit(
                        "skill_run_interrupted",
                        run_id=self._run_id,
                        exc_type=exc_type.__name__,
                        will_resume=True,
                    )
