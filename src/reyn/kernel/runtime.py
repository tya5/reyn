from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from reyn.kernel.rollback_state import (
    RollbackState,  # noqa: F401 – re-exported for existing callers
)
from reyn.kernel.run_state import RunState
from reyn.schemas.models import CandidateOutput, ContextFrame, Skill

if TYPE_CHECKING:
    from reyn.budget.budget import BudgetTracker
    from reyn.chat.services.chat_compaction_engine import ChatCompactionEngine
    from reyn.config import MultimodalConfig, PhaseActResultsCompactionConfig, SandboxConfig
    from reyn.events.state_log import StateLog
    from reyn.secrets.store import ScopedSecretStore
    from reyn.skill.skill_registry import SkillRegistry
    from reyn.workspace.media_store import MediaStore
from reyn.config import SafetyConfig
from reyn.context_builder import build_frame
from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.kernel.llm_call_recorder import LLMCallRecorder
from reyn.kernel.phase_executor import PhaseExecutor
from reyn.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.kernel.run_orchestrator import RunOrchestrator
from reyn.kernel.runtime_types import (
    LoopLimitExceededError,
    PhaseBudgetExceededError,
    RunResult,
    WorkflowAbortedError,
    _normalize_artifact,
    _validate_artifact_structure,
)
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.permissions.permissions import PermissionResolver
from reyn.user_intervention import RequestBus
from reyn.workspace.workspace import Workspace

# LoopLimitExceededError / PhaseBudgetExceededError / WorkflowAbortedError /
# RunResult / _normalize_artifact / _validate_artifact_structure moved to
# reyn.kernel.runtime_types (FP-0020 Component C follow-up — break circular
# imports between runtime.py and phase_executor.py). Re-exported above via
# `from reyn.kernel.runtime_types import (...)` for backward compatibility.
# RollbackState moved to reyn.kernel.rollback_state (FP-0020 Component A).
# RunOrchestrator extracted from OSRuntime.run() body (FP-0020 Component D).
# OSRuntime is now a wiring layer; run() delegates to RunOrchestrator.run().


class OSRuntime:
    def __init__(
        self,
        skill: Skill,
        model: str,
        strict: bool = False,
        subscribers: list[Callable] | None = None,
        intervention_bus: "RequestBus | None" = None,
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
        sandbox_config: "SandboxConfig | None" = None,
        multimodal_config: "MultimodalConfig | None" = None,
        media_store: "MediaStore | None" = None,
        secret_store: "ScopedSecretStore | None" = None,
        plan_step: dict | None = None,
        workspace_base_dir: "Path | None" = None,
        phase_compaction_engine: "ChatCompactionEngine | None" = None,
        phase_compaction_cfg: "PhaseActResultsCompactionConfig | None" = None,
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
        self.events = EventLog(
            subscribers=subscribers, run_id=run_id, plan_step=plan_step,
        )
        self.workspace = Workspace(
            self.events,
            permission_resolver=permission_resolver,
            skill_name=skill.name,
            base_dir=workspace_base_dir,
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
        # FP-0017 follow-up: thread reyn.yaml `sandbox:` config into the
        # executor so sandboxed_exec backend selection honors the operator's
        # declared backend / on_unsupported policy. None → platform default.
        self._sandbox_config = sandbox_config
        # Issue #364 multi-modal cluster: media-size gate config.
        self._multimodal_config = multimodal_config
        # Issue #383 PR-C: media + tool-result file storage.
        self._media_store = media_store
        # FP-0016 D: per-skill credential scoping. None = unrestricted
        # (= preserves backward compat for callers that don't supply a store).
        self._secret_store = secret_store
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
            sandbox_config=sandbox_config,
            multimodal_config=multimodal_config,
            media_store=media_store,
            secret_store=secret_store,
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
            secret_store=secret_store,
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
        # PR-N8: phase axis compaction wiring.  Engine + cfg are optional kwargs
        # so tests can inject real instances; production constructs them lazily
        # here (= Path b, same pattern as planner.execute_plan).  When no
        # injection is provided, a default ChatCompactionEngine is constructed
        # using this OSRuntime's model + events.  T_SP=0 is the conservative
        # non-chat default (= no session SP measured; main_pool = T_max, same
        # as planner step axis Path b).  The cfg default fires at
        # recent_act_turns_raw=5 which is higher than planner's 3, matching
        # the phase-axis policy (phase ops carry denser structured data).
        # Both attrs are set unconditionally so PhaseExecutor always gets them.
        if phase_compaction_engine is None:
            try:
                from reyn.chat.services.chat_compaction_engine import (
                    ChatCompactionEngine as _CCE,
                )
                phase_compaction_engine = _CCE(
                    model=model,
                    events=self.events,
                    T_SP=0,
                    cfg=None,  # default CompactionConfig for budget derivation
                )
            except Exception:  # noqa: BLE001 — best-effort; skip if unavailable
                phase_compaction_engine = None
        if phase_compaction_cfg is None:
            from reyn.config import PhaseActResultsCompactionConfig as _PARCC
            phase_compaction_cfg = _PARCC()
        self._phase_compaction_engine: "ChatCompactionEngine | None" = phase_compaction_engine
        self._phase_compaction_cfg: "PhaseActResultsCompactionConfig | None" = phase_compaction_cfg
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
            phase_compaction_engine=self._phase_compaction_engine,  # PR-N8
            phase_compaction_cfg=self._phase_compaction_cfg,        # PR-N8
        )
        # FP-0020 Component D: phase sequence + transitions + rollback + skill-node
        # dispatch + resume + SkillRegistry lifecycle extracted to RunOrchestrator.
        # OSRuntime.run() becomes a thin delegation wrapper.
        self._orchestrator = RunOrchestrator(
            phase_executor=self._phase_executor,
            skill=skill,
            workspace=self.workspace,
            events=self.events,
            skill_registry=skill_registry,
            preprocessor=self._preprocessor,
            state=self._state,
            safety=_safety,
            intervention_bus=intervention_bus,
            resume_plan=resume_plan,
            run_id=run_id,
            parent_run_id=parent_run_id,
            build_candidates_fn=self._build_candidates,
            enter_phase_fn=self._enter_phase,
            execute_phase_fn=self._execute_phase,
            perm=permission_resolver,
            resolver_model_fn=self._resolver.resolve,
            resolver=self._resolver,
            model=model,
            strict=strict,
            subscribers=subscribers,
            state_log=state_log,
            caller=caller,
            max_phase_visits=self._max_phase_visits,
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

    # ── Public read-only accessors (FP-0016 D wiring verification) ─────────
    # Tests verify dependency-injection identity ("the store handed in is the
    # exact object threaded down to executors"). Exposing read-only accessors
    # lets tests assert that invariant through the public surface instead of
    # reaching into ``_secret_store`` / ``_preprocessor``.

    @property
    def secret_store(self):
        return self._secret_store

    @property
    def preprocessor(self):
        return self._preprocessor

    @property
    def phase_compaction_engine(self) -> "ChatCompactionEngine | None":
        """PR-N8: read-only accessor for wiring-verification tests.

        Callers (tests) can assert that the injected engine is the exact object
        threaded into PhaseExecutor without reaching into private state.
        """
        return self._phase_compaction_engine

    @property
    def phase_compaction_cfg(self) -> "PhaseActResultsCompactionConfig | None":
        """PR-N8: read-only accessor for wiring-verification tests."""
        return self._phase_compaction_cfg

    # ── Phase setup ────────────────────────────────────────────────────────────

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

    # ── Phase entry + phase execution ─────────────────────────────────────────
    # These methods are defined on OSRuntime (not directly on RunOrchestrator)
    # so that OSRuntime subclasses can override them and have the override take
    # effect inside RunOrchestrator.run() (same pattern as build_frame_fn).
    # RunOrchestrator receives them as callables (enter_phase_fn / execute_phase_fn)
    # and calls them via the stored references, so Python's MRO applies.

    async def _enter_phase(self, phase_name: str, artifact: dict) -> None:
        """Phase entry: visit-count check, limit checkpoint, and phase_started event.

        Defined on OSRuntime so subclasses can override it and have the override
        take effect inside RunOrchestrator.run() (which calls this via the
        enter_phase_fn callable passed at construction). Uses self._max_phase_visits
        so post-construction mutations to that attribute (e.g. in tests) are
        picked up at each call.

        FP-0020 Component D: original implementation from OSRuntime.run() pre-extraction,
        now centralised here so it's the single authoritative definition reachable
        from both the orchestrator callback path and direct test calls.
        """
        max_visits = self._max_phase_visits
        # FP-0005: extensions granted by user approval / auto_extend
        # raise the effective cap. Tracked per-kind on the state so
        # repeated hits on the same limit can be re-extended.
        effective_max = self._state.effective_visit_cap(max_visits)
        count = self._state.visit_counts.get(phase_name, 0)
        if effective_max and count >= effective_max:
            # FP-0005: ask before raising. on_limit.mode controls the
            # behaviour; default 'unattended' preserves legacy abort.
            decision = await self._orchestrator._handle_limit_checkpoint(
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
            # bumped via safety_extensions and will be picked up on
            # the next visit.
        new_count = self._state.begin_phase(phase_name)
        self.events.emit(
            "phase_started", phase=phase_name,
            visit_count=new_count, input_artifact_type=artifact.get("type"),
        )

    async def _execute_phase(
        self,
        current_phase: str,
        artifact: dict,
        candidates,
        output_language,
        max_phase_retries: int,
        artifact_path=None,
        rollback_context=None,
    ):
        """Delegate to PhaseExecutor.execute via RunOrchestrator.

        OSRuntime subclasses may override this method; RunOrchestrator.run()
        calls it via the stored execute_phase_fn callable so overrides take
        effect (same subclass-override contract as pre-FP-0020 Component D).
        """
        return await self._phase_executor.execute(
            current_phase, artifact, candidates, output_language, max_phase_retries,
            self._state,
            artifact_path=artifact_path,
            rollback_context=rollback_context,
        )

    # ── Backward-compat shims for private LLMCallRecorder methods ─────────────
    # Tests and other callers that invoke these methods directly on OSRuntime
    # continue to work. Remove in a subsequent cleanup PR.

    async def _call_llm_and_record(
        self,
        phase: str,
        frame: "ContextFrame",
        prior_attempts: list | None,
        rollback_context: dict | None = None,
    ) -> dict:
        """Shim: delegate to LLMCallRecorder.

        FP-0020 Component B: the 7 LLM/WAL/budget methods formerly on
        OSRuntime now live in LLMCallRecorder. This thin wrapper preserves
        the call signature so existing callers and tests are unaffected.
        """
        return await self._llm_caller.call(
            phase, frame, prior_attempts, rollback_context, self._state,
        )

    async def _wal_step_completed_for_llm(self, **kwargs) -> None:
        await self._llm_caller._wal_step_completed_for_llm(**kwargs)

    def _extract_memoized_llm_result(self, memo, *, phase, op_invocation_id):
        return self._llm_caller._extract_memoized_llm_result(
            memo, phase=phase, op_invocation_id=op_invocation_id,
        )

    # ── Main entry point ───────────────────────────────────────────────────────

    async def run(
        self,
        initial_input: dict,
        output_language: str | None = None,
        max_phase_retries: int = 2,
    ) -> RunResult:
        """Thin delegation to RunOrchestrator.run() (FP-0020 Component D).

        Phase sequence, transitions, rollback, skill-node dispatch, resume
        fast-forward, SkillRegistry lifecycle, and exception handling all live
        in RunOrchestrator. OSRuntime is now a wiring layer.

        max_phase_retries: retries per phase on validation failure (default 2 = 3 total attempts).
        Returns RunResult with status="finished" or status="loop_limit_exceeded".
        Raises WorkflowAbortedError on unrecoverable LLM abort.
        """
        return await self._orchestrator.run(
            initial_input=initial_input,
            output_language=output_language,
            max_phase_retries=max_phase_retries,
        )

    # ── _validate_phase_output — kept as backward-compat shim ─────────────────
    # Tests that call _validate_phase_output directly on OSRuntime continue to
    # work. The real implementation now lives in PhaseExecutor. Remove in a
    # subsequent cleanup PR.

    def _validate_phase_output(
        self,
        raw: dict,
        current_phase: str,
        candidates: list[CandidateOutput],
        allowed_next: list[str],
        input_artifact: dict | None = None,
    ):
        """Backward-compat shim: delegate to PhaseExecutor._validate_phase_output.

        FP-0020 Component C moved the real implementation to PhaseExecutor.
        Remove in a subsequent cleanup PR.
        """
        return self._phase_executor._validate_phase_output(
            raw, current_phase, candidates, allowed_next, self._state, input_artifact=input_artifact,
        )

