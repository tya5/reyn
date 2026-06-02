"""LLMCallRecorder — Layer 3 of OSRuntime decomposition.

Extracted from OSRuntime (FP-0020 Component B). Owns one LLM call from
budget pre-check through WAL recording.

Design note — 11-dep ctor:
The constructor receives 11 dependencies. Three groupings were evaluated:

  A) ``BudgetEnforcer(budget_tracker, caller, chain_id, skill_name)`` — the
     four budget-related deps share a natural interface (check_pre / record_post /
     agent_name). However, ``BudgetEnforcer`` would be a new abstraction with
     only one consumer; extracting it here would duplicate the grouping effort
     already implicit in the three budget methods. Deferred to Component C/D
     when the natural home becomes clearer.

  B) ``WalRecorder(state_log, run_id, skill_registry)`` — the three WAL deps
     are tightly coupled to the step_completed emission pattern. Same reasoning:
     single consumer, value unclear before Component D shapes the picture.

  C) Leave flat (11 deps, no grouping) — chosen for this PR. Avoids introducing
     unstable intermediate abstractions when the full decomposition (C/D) is two
     PRs away. The 11-dep smell is acknowledged; the refactor opportunity is
     preserved via a TODO comment.

Conclusion: 11-dep ctor retained. Grouping deferred to Component C or D when
BudgetEnforcer / WalRecorder have ≥ 2 consumers.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from reyn.budget.budget import BudgetExceeded, format_refusal_message
from reyn.dispatch.dispatcher import _compute_llm_args_hash, _lookup_memoized_step
from reyn.llm.llm import call_llm, call_llm_tools
from reyn.llm.llm import proxy_kwargs as _proxy_kwargs
from reyn.llm.pricing import TokenUsage, estimate_cost
from reyn.schemas.models import ContextFrame, Skill

if TYPE_CHECKING:
    from reyn.budget.budget import BudgetTracker
    from reyn.events.state_log import StateLog
    from reyn.kernel.run_state import RunState
    from reyn.llm.llm import LLMToolCallResult
    from reyn.llm.model_resolver import ModelResolver
    from reyn.skill.skill_registry import SkillRegistry

_log = logging.getLogger(__name__)


class LLMCallRecorder:
    """Owns one LLM call: budget pre-check → memo lookup → call_llm → WAL record.

    Extracted from OSRuntime._call_llm_and_record and its six helper methods.
    All methods that were private on OSRuntime become private here; the single
    public entry point is ``call()``.

    ``_check_phase_budget`` is intentionally NOT extracted here — it depends on
    ``phase_started_at`` / ``elapsed_phase_seconds()`` which belong at the
    PhaseExecutor (Layer 2) level. See Component C for its future home.
    """

    def __init__(
        self,
        *,
        resolver: "ModelResolver",
        state_log: "StateLog | None",
        run_id: str | None,
        skill_registry: "SkillRegistry | None",
        budget_tracker: "BudgetTracker | None",
        caller: str,
        chain_id: str | None,
        skill_name: str,
        prompt_cache_enabled: bool,
        events,
        skill: Skill,
        model: str,
        llm_timeout: float,
        llm_max_retries: int,
        project_context: str,
        agent_role: str,
        resume_plan: object,
    ) -> None:
        # TODO(fp-0020-c): consider grouping budget deps into BudgetEnforcer
        # and WAL deps into WalRecorder when Component C/D land and provide
        # ≥2 consumers for each grouping.
        self._resolver = resolver
        self._state_log = state_log
        self._run_id = run_id
        self._skill_registry = skill_registry
        self._budget_tracker = budget_tracker
        self._caller = caller
        self._chain_id = chain_id
        self._budget_skill_name = skill_name
        self._prompt_cache_enabled = prompt_cache_enabled
        self._events = events
        self._skill = skill
        self._base_model = model
        self._llm_timeout = llm_timeout
        self._llm_max_retries = llm_max_retries
        self._project_context = project_context
        self._agent_role = agent_role
        self._resume_plan = resume_plan

    # ── Public entry point ─────────────────────────────────────────────────────

    async def call(
        self,
        phase: str,
        frame: ContextFrame,
        prior_attempts: list[dict] | None,
        rollback_context: dict | None,
        state: "RunState",
    ) -> dict:
        """Budget check → memo lookup → call_llm → WAL record → accumulate usage.

        ``_check_phase_budget`` is NOT called here — it lives at the PhaseExecutor
        layer (Component C). The caller (OSRuntime._call_llm_and_record shim) is
        responsible for invoking it before calling this method.
        """
        resolved_spec = self._resolver.resolve(self._effective_model(phase))
        resolved_model = resolved_spec.model

        phase_def = self._skill.phases.get(phase)

        # R-D2: per-phase LLM op_invocation_id + memoization.
        op_invocation_id = state.next_llm_invocation_id(phase)

        # Compute args_hash regardless of resume_plan presence.
        args_hash = _compute_llm_args_hash(
            model=resolved_model,
            frame=frame.model_dump(mode="json"),
            prior_attempts=prior_attempts,
            rollback_context=rollback_context,
            system_inputs={
                "skill_name": self._skill.name,
                "skill_description": self._skill.description,
                "phase_role": phase_def.role if phase_def else None,
                "project_context": self._project_context,
                "agent_role": self._agent_role,
            },
        )

        # Memo lookup (resume only).
        if self._resume_plan is not None:
            memo = _lookup_memoized_step(
                self._resume_plan, op_invocation_id, phase, args_hash,
            )
            if memo is not None:
                memoized = self._extract_memoized_llm_result(
                    memo, phase=phase, op_invocation_id=op_invocation_id,
                )
                if memoized is not None:
                    self._credit_budget_from_memo(
                        memo,
                        resolved_model=resolved_model,
                        phase=phase,
                        op_invocation_id=op_invocation_id,
                        state=state,
                    )
                    self._events.emit(
                        "step_memoized",
                        run_id=self._run_id,
                        phase=phase,
                        op_invocation_id=op_invocation_id,
                        op_kind="llm",
                        args_hash=args_hash,
                    )
                    return memoized
                # else: corrupt memo result → fall through to fresh call

        # Normal call path
        self._check_budget_pre_llm(resolved_model)
        self._events.emit(
            "llm_called",
            run_id=self._run_id,
            skill=self._skill.name,
            phase=phase,
            model=resolved_model,
        )
        llm_result = await call_llm(
            resolved_spec, frame,
            prior_attempts=prior_attempts or None,
            rollback_context=rollback_context,
            timeout=self._llm_timeout,
            max_retries=self._llm_max_retries,
            prompt_cache_enabled=self._prompt_cache_enabled,
            skill_name=self._skill.name,
            skill_description=self._skill.description,
            phase_role=phase_def.role if phase_def else None,
            project_context=self._project_context,
            agent_role=self._agent_role,
            trace_caller=f"phase:{phase}",
            event_log=self._events,
        )
        raw = llm_result.data
        cost_usd: float | None = None
        pricing_snapshot: dict | None = None
        if llm_result.usage:
            _pricing_model = (
                resolved_model.split("/", 1)[1]
                if "/" in resolved_model and _proxy_kwargs()
                else resolved_model
            )
            cost_usd, pricing_snapshot = estimate_cost(_pricing_model, llm_result.usage)
            state.add_usage(llm_result.usage, cost_usd)
            self._record_budget_post_llm(resolved_model, llm_result.usage)
        self._events.emit(
            "llm_response_received",
            run_id=self._run_id,
            skill=self._skill.name,
            phase=phase,
            response_type=raw.get("type"),
            raw=raw,
            prompt_tokens=llm_result.usage.prompt_tokens if llm_result.usage else None,
            completion_tokens=llm_result.usage.completion_tokens if llm_result.usage else None,
            cost_usd=cost_usd,
            pricing_snapshot=pricing_snapshot,
        )

        await self._wal_step_completed_for_llm(
            phase=phase,
            op_invocation_id=op_invocation_id,
            args_hash=args_hash,
            result=raw,
            usage=llm_result.usage.to_dict() if llm_result.usage else None,
        )

        return raw

    async def call_tools(
        self,
        phase: str,
        frame: ContextFrame,
        tools: list[dict],
        state: "RunState",
    ) -> "LLMToolCallResult":
        """#1212 PR2: native-tools variant of ``call``. Returns the raw assistant
        message (content / tool_calls / finish_reason) for the op-loop.

        Shares ``call``'s model-resolution + budget pre-check + cost-record + the
        ``llm_called`` / ``llm_response_received`` events, and builds the [system,
        user(frame)] messages via the SAME ``build_phase_messages`` helper as the
        json-mode path (no drift), but calls ``call_llm_tools`` instead of
        ``call_llm`` and — per ADR-0035 PR2 scope — **skips decide-memo and the
        per-step WAL** (the op-EXECUTION crash-recovery WAL is owned by the
        control_ir_executor's ``dispatch_tool``, D8; act-turn LLM memoization is a
        PR5 decision, see ADR Open items). Un-opted skills never reach here, so the
        json-mode ``call`` path is byte-for-byte unchanged.
        """
        from reyn.llm.llm import build_phase_messages

        resolved_spec = self._resolver.resolve(self._effective_model(phase))
        resolved_model = resolved_spec.model
        phase_def = self._skill.phases.get(phase)

        messages = build_phase_messages(
            frame,
            skill_name=self._skill.name,
            skill_description=self._skill.description,
            phase_role=phase_def.role if phase_def else None,
            project_context=self._project_context,
            agent_role=self._agent_role,
            prompt_cache_enabled=self._prompt_cache_enabled,
        )

        self._check_budget_pre_llm(resolved_model)
        self._events.emit(
            "llm_called",
            run_id=self._run_id,
            skill=self._skill.name,
            phase=phase,
            model=resolved_model,
        )
        result = await call_llm_tools(
            model=resolved_spec,
            messages=messages,
            tools=tools,
            timeout=self._llm_timeout,
            max_retries=self._llm_max_retries,
            prompt_cache_enabled=self._prompt_cache_enabled,
            skill_name=self._skill.name,
            skill_description=self._skill.description,
            trace_caller=f"phase:{phase}",
            event_log=self._events,
        )

        cost_usd: float | None = None
        pricing_snapshot: dict | None = None
        if result.usage:
            _pricing_model = (
                resolved_model.split("/", 1)[1]
                if "/" in resolved_model and _proxy_kwargs()
                else resolved_model
            )
            cost_usd, pricing_snapshot = estimate_cost(_pricing_model, result.usage)
            state.add_usage(result.usage, cost_usd)
            self._record_budget_post_llm(resolved_model, result.usage)
        self._events.emit(
            "llm_response_received",
            run_id=self._run_id,
            skill=self._skill.name,
            phase=phase,
            response_type="tool_calls" if result.tool_calls else "content",
            raw={
                "tool_calls": result.tool_calls,
                "content": result.content,
                "finish_reason": result.finish_reason,
            },
            prompt_tokens=result.usage.prompt_tokens if result.usage else None,
            completion_tokens=result.usage.completion_tokens if result.usage else None,
            cost_usd=cost_usd,
            pricing_snapshot=pricing_snapshot,
        )
        return result

    # ── Model resolution ───────────────────────────────────────────────────────

    def _effective_model(self, phase_name: str) -> str:
        """Return the phase-level model override, falling back to the runtime model.

        Mirrors OSRuntime._effective_model. LLMCallRecorder needs this internally
        to resolve the model before the call; OSRuntime still exposes its own copy
        for build_frame() consumers.
        """
        phase = self._skill.phases.get(phase_name)
        # _runtime_model is injected at construction time via the `resolver`'s
        # default, but LLMCallRecorder doesn't store the base model string. The
        # caller (OSRuntime) passes the already-resolved frame; for _effective_model
        # we need the base model. Store it separately.
        return phase.model_class if phase and phase.model_class else self._base_model

    # ── WAL recording ──────────────────────────────────────────────────────────

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

        R-D10: large results (> 32 KB serialized) are off-loaded to a ref file.
        """
        if self._state_log is None or self._run_id is None:
            return
        wal_result = result
        if self._skill_registry is not None:
            from reyn.skill import llm_result_ref
            wal_result = llm_result_ref.write_if_large(
                agent_state_dir=self._skill_registry.state_dir,
                run_id=self._run_id,
                args_hash=args_hash,
                result=result,
            )
        try:
            await self._state_log.append(
                "step_completed",
                run_id=self._run_id,
                phase=phase,
                op_invocation_id=op_invocation_id,
                op_kind="llm",
                args_hash=args_hash,
                result=wal_result,
                usage=usage,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "WAL step_completed (llm) emission failed (run=%s phase=%s id=%s): %s",
                self._run_id, phase, op_invocation_id, e,
            )

    # ── Memo support ───────────────────────────────────────────────────────────

    def _extract_memoized_llm_result(
        self,
        memo: object,
        *,
        phase: str,
        op_invocation_id: str,
    ) -> dict | None:
        """Return the recorded LLM response dict, or None on schema mismatch.

        R-D10: resolves ``{"_ref": "<file>"}`` placeholders transparently.
        """
        result = getattr(memo, "result", None)
        if not isinstance(result, dict):
            _log.warning(
                "LLM memo result is not a dict (run=%s phase=%s id=%s); "
                "falling through to fresh call",
                self._run_id, phase, op_invocation_id,
            )
            return None
        if (self._skill_registry is not None and self._run_id is not None
                and list(result.keys()) == ["_ref"]):
            from reyn.skill import llm_result_ref
            resolved = llm_result_ref.resolve(
                agent_state_dir=self._skill_registry.state_dir,
                run_id=self._run_id,
                value=result,
            )
            if resolved is None:
                return None
            if not isinstance(resolved, dict):
                _log.warning(
                    "LLM memo ref resolved to non-dict (run=%s phase=%s id=%s)",
                    self._run_id, phase, op_invocation_id,
                )
                return None
            return resolved
        return result

    def _credit_budget_from_memo(
        self,
        memo: object,
        *,
        resolved_model: str,
        phase: str,
        op_invocation_id: str,
        state: "RunState",
    ) -> None:
        """R-D8 L3: re-credit the budget tracker from a memoized LLM step.

        Suppressed when the BudgetTracker has loaded its persisted state (R-D8 L4+L5).
        """
        if self._budget_tracker is None:
            return
        if getattr(self._budget_tracker, "_state_loaded", False):
            return
        usage_dict = getattr(memo, "usage", None)
        if not usage_dict:
            _log.debug(
                "memo hit (run=%s phase=%s id=%s) has no usage data; "
                "skipping budget credit (pre-R-D8 step or LLM returned no usage)",
                self._run_id, phase, op_invocation_id,
            )
            return
        usage = TokenUsage.from_dict(usage_dict)
        _pricing_model_memo = (
            resolved_model.split("/", 1)[1]
            if "/" in resolved_model and _proxy_kwargs()
            else resolved_model
        )
        cost_usd, _ = estimate_cost(_pricing_model_memo, usage)
        state.add_usage(usage, cost_usd)
        self._record_budget_post_llm(resolved_model, usage)

    # ── Budget hooks ───────────────────────────────────────────────────────────

    def _budget_agent_name(self) -> str | None:
        """Extract the agent name from caller (``agents/<name>`` → ``<name>``).

        Returns None when caller is ``direct`` (no agent context).
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
            self._events.emit(
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
            self._events.emit(
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
            purpose="phase",  # #1190: the OS phase-execution LLM path
        )
        for dim in check.warn_dimensions:
            self._events.emit(
                "budget_warn",
                dimension=dim,
                agent=agent,
                chain_id=self._chain_id,
                **check.context,
            )
