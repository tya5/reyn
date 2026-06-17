"""PhaseRouterLoopHost — #1092 PR-A (FD1, ADR-0036).

The phase-side ``RouterLoopCore`` implementation that lets a phase act-loop
drive the shared chat ``RouterLoop`` (Fork 1 convergence). It mirrors the
proven narrow ``_PlanStepHost`` (``chat/planner.py``) shape but is
TERMINAL-VALUED: a phase has no parent chat host to delegate to, and — per
#1212 PR3 decision A — no skills / agents / mcp / universal catalog.

It owns the catalog-source REPLACE seam:

- ``get_phase_op_catalog`` — the catalog-source REPLACE seam (``RouterLoop.run``
  uses this INSTEAD of chat-discovery ``build_tools`` when present).

The earlier op-execution seam (``execute_phase_op`` / ``RouterLoop._execute_tool``
host delegation, #1234 FD1 beta) was OBVIATED by the #1240 catalog axis: a phase's
op tool NAMES are now the unified fine registry kinds (``read_file`` … plus
``invoke_skill`` / ``call_mcp_tool``), so ``RouterLoop._invoke_router_tool`` routes
them through its existing ``REGISTRY_DISPATCH_TOOLS`` registry path — no
phase-specific exec hook needed (RouterLoop still holds no phase op-kind strings,
P7). The convergence wiring (PR-B) closes the two residuals this leaves:
(1) add ``edit_file`` / ``glob_files`` / ``grep_files`` to ``REGISTRY_DISPATCH_TOOLS``
(registry ToolDefs that chat never exposed as router tools), and
(2) implement :meth:`make_router_op_context` to return a phase ``OpContext``
(carrying the phase ``PermissionDecl`` / ``allowed_ops`` / sandbox policy) so the
registry handlers enforce phase permissions — the role the obviated seam's
``control_ir_executor`` dispatch played.

This host is the chat-vs-phase polymorphism point — the same role
``RouterHostAdapter`` (chat) and ``_PlanStepHost`` (plan-step) play for their loops.
It is INERT until ``PhaseExecutor`` wires it in (PR-B); today's phase act-loop still
runs the json-mode ``_run_op_loop`` unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Callable


class PhaseRouterLoopHost:
    """RouterLoopCore for a single phase act-loop iteration set.

    Construction deps all originate in ``PhaseExecutor._run_op_loop``'s scope
    (``phase_executor.py``): the shared ``control_ir_executor``, the phase
    ``EventLog``, the current phase name + ``PermissionDecl`` + ``allowed_ops``
    + phase-default ``SandboxPolicy``, plus agent identity and the OS model
    resolver (passed as ``resolve_model_fn`` so this host stays decoupled from
    OSRuntime's resolver wiring — the loop only needs ``name -> model id``).
    """

    def __init__(
        self,
        *,
        control_ir_executor: Any,
        events: Any,
        phase: str,
        decl: Any,
        allowed_ops: set[str] | None,
        default_sandbox_policy: dict | None,
        agent_name: str,
        agent_role: str,
        output_language: str | None,
        resolve_model_fn: Callable[[str], str],
        compaction_engine: Any = None,
        compaction_cfg: Any = None,
        check_phase_budget_fn: Callable[[], Any] | None = None,
        summary_memo: Any = None,
        turn_budget_engine: Any = None,
    ) -> None:
        self._control_ir_executor = control_ir_executor
        self._events = events
        self._phase = phase
        self._decl = decl
        self._allowed_ops = allowed_ops
        self._default_sandbox_policy = default_sandbox_policy
        self._agent_name = agent_name
        self._agent_role = agent_role
        self._output_language = output_language
        self._resolve_model_fn = resolve_model_fn
        # #1092 PR-C-4b: phase compaction engine + config for the per-turn in-loop
        # message-history compaction hook (``maybe_compact_messages``). Both None
        # when the phase has no compaction wired → the hook is a no-op.
        self._compaction_engine = compaction_engine
        self._compaction_cfg = compaction_cfg
        # #1092 PR-C-5 (2): async callable that runs the phase wall-clock budget
        # check (PhaseExecutor._check_phase_budget bound to this phase + state).
        # None for chat hosts → the per-turn enforcement hook is a no-op.
        self._check_phase_budget_fn = check_phase_budget_fn
        # #1267: WAL-memo seam for the in-loop (C-4b) compaction summary call.
        self._summary_memo = summary_memo
        # #1092 C2: cumulative-axis force-close engine (TurnBudgetEngine). None
        # → the ``should_force_close`` trigger is inert (no phase force-close).
        self._turn_budget_engine = turn_budget_engine
        # #1092 PR-D1: the consolidation finish of a force-close in this run_loop
        # (set via ``record_force_close``), read by PhaseExecutor after the loop
        # to persist the checkpoint. None = the phase did not force-close.
        self._forced_close_result: Any = None

    # ── RouterLoopCore identity / static config ───────────────────────────

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def agent_role(self) -> str:
        return self._agent_role

    @property
    def output_language(self) -> str | None:
        return self._output_language

    @property
    def events(self) -> Any:
        return self._events

    def resolve_model(self, name: str) -> str:
        return self._resolve_model_fn(name)

    def make_router_op_context(self) -> Any:
        """Phase ``OpContext`` factory for the registry tool-dispatch handlers.

        RouterLoop's ``op_context_factory`` (= this method) feeds the registry
        tool-dispatch handlers (``REGISTRY_DISPATCH_TOOLS`` path). With the op-exec
        seam obviated (see module docstring), phase ops route through that same
        registry path, so this returns the SAME phase ``OpContext`` the json-mode
        op-loop builds — delegated to ``ControlIRExecutor._build_ctx`` with the
        phase ``PermissionDecl`` + sandbox policy so the registry handlers enforce
        phase permissions identically to ``control_ir_executor.execute`` (the role
        the obviated seam played). Single-sourced via the executor so there is no
        second permission/sandbox provisioning path to drift (P3/P5).
        """
        return self._control_ir_executor._build_ctx(
            self._decl,
            self._phase,
            default_sandbox_policy=self._default_sandbox_policy,
        )

    def op_dispatch_memo(self) -> dict | None:
        """Phase-mode op-dispatch WAL-memoization context (#1092 PR-C-2.5).

        ``RouterLoop._execute_tool`` consults this hook to decide whether a phase
        op dispatch is crash-resume memoized. A phase host returns the per-phase
        WAL wiring (``state_log`` + ``skill_run_id`` + ``resume_plan`` + ``phase``)
        so the dispatch threads them into ``dispatch_tool`` (with a phase-relative
        ``op_invocation_id``), reproducing the json-mode-equal crash-resume HARD
        GATE (#1225 Decision A): on resume the op memo-HITS and does not
        re-execute. Single-sourced from the shared ``ControlIRExecutor`` so there
        is no second resume-wiring path to drift (P5/P6).

        Chat hosts do NOT implement this method — ``RouterLoop._execute_tool``
        getattr-guards it to ``None``, leaving the chat dispatch path byte-identical
        (``caller_kind="router"``, no WAL step). Returns ``None`` here too when the
        executor has no WAL wired (state_log/skill_run_id absent = non-resumable run),
        so a non-resumable phase run also stays on the plain dispatch path.
        """
        cie = self._control_ir_executor
        state_log = getattr(cie, "_state_log", None)
        skill_run_id = getattr(cie, "_skill_run_id", None)
        if state_log is None or skill_run_id is None:
            return None
        return {
            "state_log": state_log,
            "skill_run_id": skill_run_id,
            "resume_plan": getattr(cie, "_resume_plan", None),
            "phase": self._phase,
        }

    def compute_memo_key(
        self, *, model: str, messages: list[dict], tools: Any, tool_choice: Any,
    ) -> str:
        """#1092 PR-C-2.6: datetime-robust act-turn memo key for the converged op-loop.

        RouterLoop keys the act-turn LLM memo on ``compute_sub_loop_args_hash(messages)``
        (``get_recorded_result`` / ``record``). For the converged op-loop the seed
        ``user`` message embeds the whole ``ContextFrame`` as JSON (``build_phase_messages``
        → ``json.dumps(frame.model_dump())``), INCLUDING the volatile ``current_datetime``.
        A real crash-resume happens at a LATER wall-clock time, so the raw-message hash
        would differ → the memo MISSES → the act turn re-invokes → a non-deterministic
        model can diverge → the op re-executes (the PR5 HARD GATE breaks). The frame-fed
        op-loop avoided this by hashing the FRAME with volatile fields stripped
        (``_compute_llm_args_hash`` / ``_LLM_VOLATILE_FRAME_FIELDS``).

        This restores that property at the message layer: strip the SAME volatile frame
        fields from any ``user`` message carrying a frame JSON, then hash. The phase-layer
        knowledge (which fields are volatile) lives HERE — RouterLoop stays frame-agnostic
        (P7). Chat hosts do NOT implement this method; RouterLoop getattr-guards it and
        falls back to ``compute_sub_loop_args_hash`` → chat memo key byte-identical.
        """
        from reyn.core.dispatch.dispatcher import (
            _LLM_VOLATILE_FRAME_FIELDS,
            _LLM_VOLATILE_NESTED_FIELDS,
        )
        from reyn.core.plan.sub_loop_memo import compute_sub_loop_args_hash

        robust = [
            self._strip_volatile_frame_fields(
                m, _LLM_VOLATILE_FRAME_FIELDS, _LLM_VOLATILE_NESTED_FIELDS,
            )
            for m in messages
        ]
        return compute_sub_loop_args_hash(
            model=model, messages=robust, tools=tools, tool_choice=tool_choice,
        )

    @staticmethod
    def _strip_volatile_frame_fields(
        msg: dict, volatile: "frozenset[str]", nested: "frozenset[str]",
    ) -> dict:
        """Return ``msg`` with volatile frame fields removed if it carries a frame JSON.

        Mirrors ``_compute_llm_args_hash``'s top-level + nested exclusion. A message is
        treated as a frame carrier only when it is a ``user`` message whose ``content``
        is a JSON object string mentioning ``current_datetime`` — so non-frame messages
        (tool results, the system prompt, assistant turns) are returned untouched.
        Re-serialised deterministically (``sort_keys``) so the stripped content is
        identical across the run-1 / run-2 boundary regardless of the original key order.
        """
        if msg.get("role") != "user":
            return msg
        content = msg.get("content")
        if not isinstance(content, str) or "current_datetime" not in content:
            return msg
        try:
            frame = json.loads(content)
        except (TypeError, ValueError):
            return msg
        if not isinstance(frame, dict):
            return msg
        cleaned: dict = {}
        for k, v in frame.items():
            if k in volatile:
                continue
            if isinstance(v, dict):
                v = {
                    sk: sv for sk, sv in v.items()
                    if f"{k}.{sk}" not in nested
                }
            cleaned[k] = v
        new_msg = dict(msg)
        new_msg["content"] = json.dumps(cleaned, sort_keys=True, ensure_ascii=False)
        return new_msg

    async def maybe_compact_messages(self, messages: list[dict], *, model: str) -> list[dict]:
        """#1092 PR-C-4b: per-turn in-loop message-history compaction for the
        converged op-loop. ``RouterLoop.run_loop`` calls this at the top of each
        iteration; chat hosts do NOT implement it (getattr-guarded → no-op → chat
        byte-identical).

        WHY: the converged op-loop threads op results as native ``tool`` messages
        that accumulate linearly (measured ~unbounded, +N tok/op, no proactive
        bound — RouterLoop's retry-shrink/voluntary-compact are overflow-only
        last-resorts). This recovers json-mode's PROACTIVE per-turn bounding (the
        json-mode act loop summarises older control_ir_results once they exceed
        ``recent_act_turns_raw``).

        HOW (validity-preserving, no structural mutation): when the serialised
        history exceeds the phase compaction trigger, the OLDER ``tool`` messages'
        result CONTENTS (beyond the last ``recent_act_turns_raw``) are folded into
        one summary via the SHARED ``compact_control_ir_results`` (the SAME engine
        primitive C-4a / the json-mode loop use — no bespoke compaction logic);
        the summary replaces the FIRST older tool message's content and the rest
        get a tiny marker. NO messages are added/removed and NO
        assistant↔tool pairing / role-alternation changes — so tool_call_id pairing
        stays API-valid across providers, and the only delta is shrunk ``tool``
        contents (token bound).

        CRASH-RESUME (scoped, json-mode-parity per #1267): the compaction summary
        is a non-WAL-memoized LLM call (shared engine), so compaction×resume has the
        SAME pre-existing memo-drift as json-mode (tracked #1267, NOT introduced
        here). The no-compaction window keeps op + LLM memo-HIT (#1263 / #1264).
        Best-effort: ``compact_control_ir_results`` never raises (LLM error →
        identity + ``phase_act_results_compaction_failed``).
        """
        engine = self._compaction_engine
        cfg = self._compaction_cfg
        if engine is None or cfg is None:
            return messages

        from reyn.services.compaction.engine import compact_control_ir_results

        n_recent = getattr(cfg, "recent_act_turns_raw", 1)
        tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(tool_idxs) <= n_recent:
            # Not enough older tool results to fold — leave the history as-is.
            return messages
        older_idxs = tool_idxs[:-n_recent] if n_recent > 0 else tool_idxs

        # Fold the OLDER tool-result contents into one summary via the SHARED
        # primitive (wrap each content as a result dict so the engine sees a list).
        # ``compact_control_ir_results`` applies its OWN token threshold (the same
        # gate json-mode uses) and returns identity — without an LLM call — when the
        # older slice is under it, so calling it each turn is cheap until the history
        # actually grows past the threshold.
        older_results = [{"result": messages[i].get("content")} for i in older_idxs]
        compacted = await compact_control_ir_results(
            older_results, engine=engine, cfg=cfg, events=self._events, phase=self._phase,
            summary_memo=self._summary_memo,
        )
        if not compacted or compacted == older_results:
            # Identity (LLM error or under the engine's own threshold) → no change.
            return messages
        summary = compacted[0].get("summary") if isinstance(compacted[0], dict) else None
        if not summary:
            return messages

        new = list(messages)
        first = older_idxs[0]
        new[first] = {**messages[first], "content": f"[earlier tool results compacted: {summary}]"}
        for i in older_idxs[1:]:
            new[i] = {**messages[i], "content": "[compacted — folded into the earlier summary]"}
        return new

    async def check_phase_budget(self) -> None:
        """#1092 PR-C-5 (2): per-turn phase wall-clock budget enforcement for the
        converged op-loop. ``RouterLoop.run_loop`` calls this at the top of each
        iteration (before the act-turn LLM call); chat hosts don't implement it
        (getattr-guarded → no-op → chat byte-identical).

        Delegates to the bound ``PhaseExecutor._check_phase_budget(phase, state)``
        (the SAME enforcement the json-mode ``_run_act_loop`` runs before each
        call_llm): it RAISES ``PhaseBudgetExceededError`` when the phase exceeds its
        wall-clock budget, unless ``safety.on_limit`` approves a continuation
        (extension granted + clock reset). Without this, a runaway converged phase
        would never be limit-checked — the #1128 safety-net gap this closes.
        """
        if self._check_phase_budget_fn is None:
            return
        await self._check_phase_budget_fn()

    async def should_force_close(self, messages: list[dict], *, model: str) -> bool:
        """#1092 C2: the layer-1 force-close trigger for the phase axis.

        ``RouterLoop.run_loop`` consults this each turn AFTER compaction (so it
        sees the shrunk content). Chat/plan hosts don't implement it (getattr →
        no-op → byte-identical). Returns True when the accumulated current-turn
        *content* (every non-system turn — the wrap-up SP swaps the system turn
        at force-close time, so it is excluded from the content measure) has
        reached the TurnBudgetEngine's layer-1 threshold. When the engine is
        absent (not activated) → always False.

        On True, RouterLoop swaps the act-turn call for the wrap-up (force-close)
        call — a terminal finish (PR-C), which the phase-axis shrink-retry (PR-B)
        wraps and PR-D's handoff will re-enter from.
        """
        engine = self._turn_budget_engine
        if engine is None:
            return False
        from reyn.services.compaction.engine import estimate_tokens_for_turn

        # use_chars4 makes the estimate model-independent (len/4 + fixed image
        # cost), so the cosmetic run-loop ``model`` (= phase name, not the real
        # model) is harmless here AND it matches how the engine measured its
        # own threshold terms (the engine is built use_chars4=True against the
        # real resolved model, which only governs T_max).
        content_tokens = sum(
            estimate_tokens_for_turn(m, model, use_chars4=True)
            for m in messages
            if isinstance(m, dict) and m.get("role") != "system"
        )
        return engine.should_force_close(content_tokens)

    def record_force_close(self, result: Any) -> None:
        """#1092 PR-D1: ``RouterLoop.run_loop`` hands the force-close consolidation
        finish here so the OS (PhaseExecutor) can persist it as a checkpoint after
        the loop. Stored, not acted on (P3 — the OS executes the handoff). Chat
        hosts don't implement this (their handoff is the outer retry_loop terminal,
        PR-F)."""
        self._forced_close_result = result

    @property
    def forced_close_result(self) -> Any:
        """The consolidation finish of a force-close in the last run_loop, or
        None if the phase did not force-close."""
        return self._forced_close_result

    @property
    def wrap_up_output_reserve(self) -> int | None:
        """#1092 PR-E: the wrap-up call's OUTPUT budget (``output_reserve``), or
        None when the phase has no force-close engine. ``RouterLoop._force_close_call``
        passes it as ``max_tokens`` to HARD-CAP the consolidation ≤ output_reserve —
        the by-construction guarantee that the re-injected checkpoint stays below
        the threshold (assert_turn_budget_bounds enforces output_reserve + offload_cap
        < threshold). Chat hosts don't implement this → no cap (their handoff is the
        outer retry_loop terminal, PR-F)."""
        engine = self._turn_budget_engine
        return engine.budget.output_reserve if engine is not None else None

    # ── Chat-discovery methods (phase = empty) ────────────────────────────
    # #1092 PR-C-0: ``RouterLoop._build_router_caller_state`` calls these EAGERLY
    # while building the per-dispatch RouterCallerState (router_loop.py). A phase
    # has no skills/agents catalog (#1212 PR3 decision A — the catalog is the
    # phase op tools via ``get_phase_op_catalog``), so they return empty. The
    # eager call is also getattr-guarded on the RouterLoop side (defence in depth);
    # implementing them here makes the "phase has no chat discovery" contract
    # explicit rather than relying on the guard's default.

    def list_available_skills(self) -> list:
        return []

    def list_available_agents(self) -> list:
        return []

    async def put_outbox(self, *, kind: str, text: str, meta: dict) -> None:
        """Phase NO-OP — a concept-absent legitimate no-op (P-clean).

        A phase's output is its artifact + transition, not a user-facing outbox
        stream. The phase act-loop accumulates op results into the RouterLoop
        message history (the phase's working state), so no-op-ing the outbox
        drops nothing the phase relies on — unlike a fragile chat stub.
        """
        return None

    # ── Catalog-source REPLACE seam ───────────────────────────────────────

    def get_phase_op_catalog(self) -> list[dict]:
        """The phase's op tool catalog in litellm ``tools=`` list shape.

        REPLACES chat-discovery in ``RouterLoop.run`` (a phase has no skills /
        agents / mcp / universal). Mirrors the exact build the json-mode
        ``_run_op_loop`` does today (``phase_executor.py``): ``allowed_ops`` →
        ``_build_phase_tool_catalog`` → ``{"type": "function", **entry}`` list.
        """
        from reyn.core.kernel.control_ir_executor import _build_phase_tool_catalog

        catalog = _build_phase_tool_catalog(self._allowed_ops or set())
        return [{"type": "function", **entry} for entry in catalog.values()]
