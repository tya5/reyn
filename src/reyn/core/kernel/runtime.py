from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from reyn.core.kernel.rollback_state import (
    RollbackState,  # noqa: F401 – re-exported for existing callers
)
from reyn.core.kernel.run_state import RunState
from reyn.schemas.models import CandidateOutput, ContextFrame, Skill

if TYPE_CHECKING:
    from reyn.config import MultimodalConfig, PhaseActResultsCompactionConfig, SandboxConfig
    from reyn.core.events.state_log import StateLog
    from reyn.data.workspace.media_store import MediaStore
    from reyn.environment.backend import EnvironmentBackend
    from reyn.runtime.budget.budget import BudgetTracker
    from reyn.security.sandbox.backend import SandboxBackend
    from reyn.security.secrets.store import ScopedSecretStore
    from reyn.services.compaction.engine import CompactionEngine
    from reyn.skill.skill_registry import SkillRegistry
from reyn.config import SafetyConfig
from reyn.core.context_builder import build_frame
from reyn.core.events.events import EventLog
from reyn.core.kernel.control_ir_executor import ControlIRExecutor
from reyn.core.kernel.llm_call_recorder import LLMCallRecorder
from reyn.core.kernel.phase_executor import PhaseExecutor
from reyn.core.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.core.kernel.run_orchestrator import RunOrchestrator
from reyn.core.kernel.runtime_types import (
    LoopLimitExceededError,
    PhaseBudgetExceededError,
    RunResult,
    WorkflowAbortedError,
    _normalize_artifact,
    _validate_artifact_structure,
)
from reyn.data.workspace.workspace import Workspace
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.security.permissions.permissions import PermissionResolver
from reyn.user_intervention import RequestBus

# LoopLimitExceededError / PhaseBudgetExceededError / WorkflowAbortedError /
# RunResult / _normalize_artifact / _validate_artifact_structure moved to
# reyn.core.kernel.runtime_types (FP-0020 Component C follow-up — break circular
# imports between runtime.py and phase_executor.py). Re-exported above via
# `from reyn.core.kernel.runtime_types import (...)` for backward compatibility.
# RollbackState moved to reyn.core.kernel.rollback_state (FP-0020 Component A).
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
        threat_scan: "object | None" = None,  # FP-0050/#1822 S5 (EP4)
        contextual_permission: "object | None" = None,  # #1912: per-session capability narrowing → phase RouterLoop + control-IR gates
        router_config: "object | None" = None,  # #1829 S3b: reyn.yaml llm.router.*
        environment_backend: "EnvironmentBackend | None" = None,
        sandbox_backend: "SandboxBackend | None" = None,
        multimodal_config: "MultimodalConfig | None" = None,
        media_store: "MediaStore | None" = None,
        secret_store: "ScopedSecretStore | None" = None,
        plan_step: dict | None = None,
        workspace_base_dir: "Path | None" = None,
        workspace_state_dir: "Path | None" = None,
        phase_compaction_engine: "CompactionEngine | None" = None,
        phase_compaction_cfg: "PhaseActResultsCompactionConfig | None" = None,
        tool_calls_op_loop_skills: list[str] | None = None,
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
        # Retained so sub-skill runs (run_skill / @sub_skill nodes) inherit the same
        # op-loop gate — a sub-skill named in the list also op-loops. A listed skill
        # runs the converged op-loop (phase drives the shared RouterLoop.run_loop,
        # #1092); un-listed skills run json-mode unchanged.
        self._tool_calls_op_loop_skills = list(tool_calls_op_loop_skills or [])
        self.events = EventLog(
            subscribers=subscribers, run_id=run_id, plan_step=plan_step,
        )
        # #1669: publish this runtime's EventLog as the ambient sink for the LLM
        # acompletion chokepoint, so every LLM call in this run emits an observable
        # `llm_request` event (non-message params) without threading events through
        # the call stack. Set at creation → propagates into the op-loop's tasks.
        from reyn.core.events.events import set_llm_request_event_log
        set_llm_request_event_log(self.events)
        # #1829 S3b: publish reyn.yaml llm.router.* for the LLM chokepoint. Guarded
        # — only set when provided, so a sub-runtime spawned within a session does
        # not clobber the session's inherited ContextVar with None.
        if router_config is not None:
            from reyn.llm.llm import set_router_config
            set_router_config(router_config)
        self.workspace = Workspace(
            self.events,
            permission_resolver=permission_resolver,
            skill_name=skill.name,
            base_dir=workspace_base_dir,
            state_dir=workspace_state_dir,
            environment_backend=environment_backend,
        )
        # C5 follow-up (#224): bound the growth of per-run control_ir offload
        # scratch dirs. Prune stale ones (TTL by mtime) once at top-level run
        # start — sub-runs (parent_run_id set) skip it to avoid redundant
        # sweeps; the TTL preserves active + recently-completed (resume-reachable)
        # runs. Best-effort: the helper swallows FS errors so GC never breaks a run.
        if parent_run_id is None:
            from reyn.services.offload import prune_stale_offload_dirs
            pruned = prune_stale_offload_dirs(
                self.workspace.state_dir / "control_ir_offload"
            )
            if pruned:
                self.events.emit("control_ir_offload_pruned", count=pruned)
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
        # #1868: publish the budget-exceed policy context (reuses safety.on_limit —
        # one unified limit policy covers budget too) so the per-LLM-call cost gate
        # (LLMCallRecorder) routes through the 3-mode framework. NOT guarded: bus /
        # run_id are per-run, so a child runtime correctly re-binds its own context.
        from reyn.llm.llm import set_budget_limit_context
        set_budget_limit_context(self._intervention_bus, self._on_limit, run_id, False)
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
        # #1326 + #1352-#1: the agent-level sandbox policy threaded into the phase /
        # orchestrator / pre- + post-processor executors as the deterministic
        # policy for the permission ∩ + sandboxed ops.
        #
        # #1352-#1 symmetrization: previously this was the operator policy
        # verbatim-or-None → when the operator declared no policy the phase
        # SandboxLayer stayed ⊤ (permission-only), while chat (#1347) already
        # resolved a CONCRETE default. resolve_sandbox_policy now gives phase the
        # SAME operator-or-concrete-default (write-tight to the workspace
        # base_dir, network=DEFAULT_SANDBOX_NETWORK, sensitive read-deny), so the
        # sandbox ∩ is active for phase ops by default too. Operator config still
        # wins verbatim when set. (Audit residual #1; chat/phase asymmetry closed.)
        from reyn.security.sandbox.policy import resolve_sandbox_policy

        self._agent_sandbox_policy = resolve_sandbox_policy(
            sandbox_config.policy if sandbox_config is not None else None,
            write_paths=[str(self.workspace.base_dir)],
        )
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
            threat_scan=threat_scan,
            contextual_permission=contextual_permission,  # #1912b: control-IR op gate
            sandbox_backend=sandbox_backend,
            multimodal_config=multimodal_config,
            media_store=media_store,
            secret_store=secret_store,
            budget_tracker=budget_tracker,  # #1190 stage (ii): judge_output cost recording
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
            sandbox_backend=sandbox_backend,
            agent_sandbox_policy=self._agent_sandbox_policy,
            threat_scan=threat_scan,  # FP-0050/#1822 S5 (EP4)
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
        # injection is provided, a default CompactionEngine is constructed
        # using this OSRuntime's model + events.  T_SP=0 is the conservative
        # non-chat default (= no session SP measured; main_pool = T_max, same
        # as planner step axis Path b).  The cfg default fires at
        # recent_act_turns_raw=5 which is higher than planner's 3, matching
        # the phase-axis policy (phase ops carry denser structured data).
        # Both attrs are set unconditionally so PhaseExecutor always gets them.
        if phase_compaction_engine is None:
            try:
                from reyn.services.compaction.engine import (
                    CompactionEngine as _CCE,
                )
                phase_compaction_engine = _CCE(
                    model=model,
                    events=self.events,
                    T_SP=0,
                    cfg=None,  # default CompactionConfig for budget derivation
                    # #1172: phase axis — `model` is the raw runtime param (a
                    # class); resolve via the same resolver the main phase LLM
                    # call uses (runtime.py main-call: self._resolver.resolve).
                    resolver=self._resolver,
                    # #1190 stage (ii): record phase act-results compaction spend.
                    recorder=budget_tracker,
                    # #1190 stage (iii) Part 4: attribute phase compaction to the
                    # run's agent (caller "agents/<name>" → "<name>"), matching
                    # the main phase call's budget agent (LLMCallRecorder).
                    recorder_agent=(
                        caller.split("/", 1)[1]
                        if caller and caller.startswith("agents/")
                        else None
                    ),
                )
            except Exception:  # noqa: BLE001 — best-effort; skip if unavailable
                phase_compaction_engine = None
        if phase_compaction_cfg is None:
            from reyn.config import PhaseActResultsCompactionConfig as _PARCC
            phase_compaction_cfg = _PARCC()
        self._phase_compaction_engine: "CompactionEngine | None" = phase_compaction_engine
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
            # OS-decided mechanism gate (P3). The config holds opted-in skill names
            # as data (P7-OK); this skill runs the converged native-tools op-loop
            # (#1092) iff its name is listed. Default empty = json-mode (zero change).
            op_loop_enabled=skill.name in (tool_calls_op_loop_skills or ()),
            agent_sandbox_policy=self._agent_sandbox_policy,  # #1326
            contextual_permission=contextual_permission,  # #1912 → phase RouterLoop live gate
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
            budget_tracker=budget_tracker,  # #1190 stage (ii): skill_node_adapt cost recording
            tool_calls_op_loop_skills=self._tool_calls_op_loop_skills,  # converged op-loop sub-skill gate
            agent_sandbox_policy=self._agent_sandbox_policy,  # #1326
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
    def phase_compaction_engine(self) -> "CompactionEngine | None":
        """PR-N8: read-only accessor for wiring-verification tests.

        Callers (tests) can assert that the injected engine is the exact object
        threaded into PhaseExecutor without reaching into private state.
        """
        return self._phase_compaction_engine

    @property
    def phase_compaction_cfg(self) -> "PhaseActResultsCompactionConfig | None":
        """PR-N8: read-only accessor for wiring-verification tests."""
        return self._phase_compaction_cfg

    @property
    def agent_sandbox_policy(self) -> dict | None:
        """#1352-#1: read-only accessor for the RESOLVED agent sandbox policy.

        Mirrors the chat factory (#1347): resolve_sandbox_policy yields an
        operator-or-concrete-default policy (never None) — wiring-verification
        tests assert the symmetrization without reaching into private state.
        """
        return self._agent_sandbox_policy

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
        act_turn_reasoning: list[str] | None = None,
    ) -> ContextFrame:
        effective_model = self._effective_model(current_phase)
        phase_def = self.skill.phases[current_phase]
        allowed = set(phase_def.allowed_ops)
        all_ops = self.control_ir_executor.available_ops()
        # #1240 Wave 2b (A)-alias: advertised kind may be a chat name that aliases
        # to an op kind (e.g. "invoke_skill" → "run_skill").  Resolve the alias
        # before checking membership so allowed_ops=[run_skill] includes the
        # invoke_skill spec and allowed_ops=[mcp] includes call_mcp_tool.
        from reyn.core.op_runtime.registry import _PHASE_TOOL_NAME_ALIAS
        filtered_ops = [
            op for op in all_ops
            if _PHASE_TOOL_NAME_ALIAS.get(op.kind, op.kind) in allowed
        ]
        # #997: wiring-gap detection. A phase that declares an op in allowed_ops
        # which the executor does NOT advertise (e.g. `mcp` with no servers
        # configured) has that op
        # filtered to nothing — the LLM sees the phase instruction referencing
        # the op but no schema, and hallucinates a fake one (the FP-0008 / #1133
        # failure class). Surface it as a P6 event so the trace tool catches the
        # caller-side wiring gap proactively, once per phase per run.
        # Apply the alias in reverse to exclude aliased names from the gap set:
        # an allowed_ops entry "run_skill" is covered by the "invoke_skill" spec.
        _advertised_resolved = {
            _PHASE_TOOL_NAME_ALIAS.get(op.kind, op.kind) for op in all_ops
        }
        gap = allowed - _advertised_resolved
        if gap and current_phase not in self._state.op_catalog_gap_warned:
            self._state.op_catalog_gap_warned.add(current_phase)
            self.events.emit(
                "phase_op_catalog_gap",
                phase=current_phase,
                missing_ops=sorted(gap),
                advertised_ops=sorted(op.kind for op in all_ops),
            )
        # When the act budget is exhausted, strip available ops so the LLM has
        # no ops to call and is structurally forced into a decide turn.
        effective_ops = [] if force_decide else filtered_ops
        # C5 (FP-0008): per-result offload directory — workspace scratch path so
        # oversized control_ir_results can be written + referenced by the LLM
        # via a file.read op. Scoped by run_id to avoid cross-run collisions.
        offload_dir = (
            self.workspace.state_dir
            / "control_ir_offload"
            / (self.run_id or "_default")
        )
        # FP-0008 #1115 Stage 0: store_artifact returns a state_dir-relative
        # handle (decoupled from base_dir). The LLM-facing artifact_ref produced
        # by maybe_ref_artifact needs a path the file op can resolve, so the OS
        # resolves the handle to an absolute path here — consistent with the C5
        # control_ir offload refs, which are also absolute and pass the workspace
        # read check (under base_dir in the co-located default). Stage 2
        # (container backend) swaps this resolution for a container-served read
        # without touching skills.
        resolved_artifact_path = (
            str(self.workspace.resolve_artifact_handle(artifact_path))
            if artifact_path is not None
            else None
        )
        model_resolved = self._resolver.resolve(effective_model).model
        # #1176 B1: OS-injected context-size signal (symmetric with chat). Render
        # the exact-token free-window from the phase compaction engine's budget;
        # render_context_size_signal returns None when the window is ample (most
        # turns → frame stays stable, no noise) and a header when it is filling.
        context_size_signal = None
        _eng = self._phase_compaction_engine
        _budgets = getattr(_eng, "budgets", None) if _eng is not None else None
        if _budgets is not None:
            import json as _json

            from reyn.services.compaction.context_signal import (
                render_context_size_signal,
            )
            from reyn.services.compaction.engine import estimate_tokens

            _used = estimate_tokens(
                _json.dumps(control_ir_results or [], ensure_ascii=False), model_resolved
            )
            context_size_signal = render_context_size_signal(
                free_window=max(0, _budgets.effective_trigger - _used),
                effective_trigger=_budgets.effective_trigger,
            )
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
            model_resolved=model_resolved,
            events=self.events,
            control_ir_results=control_ir_results,
            act_turn_reasoning=act_turn_reasoning or [],
            artifact_path=resolved_artifact_path,
            remaining_act_turns=remaining_act_turns,
            offload_dir=offload_dir,
            context_size_signal=context_size_signal,
            # #1383 (D12): when build_frame offloads an artifact to a state-dir
            # path and hands the LLM a readable ref, register a scoped read-grant
            # on that exact path so the agent can read what it is told to read.
            on_offload_ref=(self._perm.grant_offload_read if self._perm else None),
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

