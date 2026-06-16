"""RouterLoopDriver — per-turn router loop orchestration for ChatSession.

Extracted from ChatSession (session.py refactor PR-3).  Owns:

  - run_turn(user_text, chain_id)  — was _run_router_loop
  - _run_with_shrink(loop, text)   — was _router_run_with_shrink
  - _check_cap(user_text)          — was _check_and_increment_router_cap
  - _force_close_handoff(loop, text) — was _force_close_handoff
  - _force_close_wrap_up(loop, model) — was _force_close_wrap_up
  - is_cancel_requested()          — was _is_turn_cancel_requested
  - request_cancel()               — turn-cancel seam (called by cancel_inflight)

Cancel lifecycle (#1468): the cooperative-cancel flag lives here.
``request_cancel()`` is called by ChatSession.cancel_inflight() for the turn
piece; ``is_cancel_requested()`` is polled at each run_loop iteration via the
RouterHostAdapter.turn_cancel_fn callback wired in ChatSession.__init__.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    pass

# Cap on force-close handoffs per turn (#1092 PR-F2b).
_MAX_FORCE_CLOSE_HANDOFFS = 1


class RouterLoopDriver:
    """Orchestrates the per-turn router loop for one ChatSession.

    Constructed once per ChatSession; all stateful orchestration that previously
    lived inline in ChatSession._run_router_loop is concentrated here.
    """

    def __init__(
        self,
        *,
        router_host: Any,             # RouterHostAdapter
        safety: Any,                  # SafetyConfig — loop.plan_invalid_retries
        router_max_iterations: int,
        budget_tracker: Any,          # BudgetTracker — for RouterLoop
        non_interactive: bool,
        exclude_tools: Any,           # set — for RouterLoop
        excluded_categories: Any = frozenset(),  # #1667 set — catalog categories for RouterLoop
        budget: Any,                  # BudgetGateway — cap + usage accounting
        resolver: Any,                # LLMResolver — model resolution
        compaction: Any,              # CompactionConfig — retry_loop cfg
        compaction_controller: Any,   # CompactionController — retry_loop engine
        token_learner: Any,           # TokenMultiplierLearner — retry_loop learner
        events: Any,                  # EventLog — emit events
        model: str,
        history_buffer: Any,          # RouterHistoryBuffer — history + SP
        budget_advisor: Any,          # ContextBudgetAdvisor — maybe_force_compact
        limit_checkpoint_fn: Callable,  # async; ChatSession._handle_chat_limit_checkpoint
        next_seq_fn: Callable[[], int], # ChatSession._next_seq reader
        append_history_fn: Callable,    # ChatSession._append_history
        chat_scheme_name: "str | None" = None,  # #1593 PR-2: chat-layer ToolUseScheme name → RouterLoop(scheme_name=); None → universal default
    ) -> None:
        self._router_host = router_host
        self._safety = safety
        self._router_max_iterations = router_max_iterations
        self._budget_tracker = budget_tracker
        self._non_interactive = non_interactive
        self._exclude_tools = exclude_tools
        self._excluded_categories = excluded_categories  # #1667
        self._budget = budget
        self._resolver = resolver
        self._compaction = compaction
        self._compaction_controller = compaction_controller
        self._token_learner = token_learner
        self._events = events
        self._model = model
        self._history_buffer = history_buffer
        self._budget_advisor = budget_advisor
        self._limit_checkpoint_fn = limit_checkpoint_fn
        self._next_seq_fn = next_seq_fn
        self._append_history_fn = append_history_fn
        self._chat_scheme_name = chat_scheme_name  # #1593 PR-2
        # #1468: per-turn cooperative cancellation flag + asyncio.Event for
        # deep-cancel propagation into running subprocess ops (#1470).
        self._turn_cancel_requested: bool = False
        self._turn_cancel_event: asyncio.Event = asyncio.Event()
        # #1470: wire cancel_event onto router_host so make_router_op_context
        # can thread it into OpContext → sandboxed_exec backend.
        _set_fn = getattr(router_host, "_set_cancel_event", None)
        if callable(_set_fn):
            _set_fn(self._turn_cancel_event)

    # ── Cancel lifecycle (#1468 / #1470) ─────────────────────────────────────

    @property
    def cancel_event(self) -> asyncio.Event:
        """Per-turn asyncio.Event set by request_cancel(), cleared at turn entry."""
        return self._turn_cancel_event

    def is_cancel_requested(self) -> bool:
        """True when a cooperative turn cancel has been requested.

        Polled at the top of each run_loop iteration via
        RouterHostAdapter.turn_cancel_fn. The flag is reset at turn entry
        so idle cancel calls (Ctrl-C while no turn is running) are
        spurious-safe.
        """
        return self._turn_cancel_requested

    def request_cancel(self) -> None:
        """Set the cooperative cancel flag and cancel_event. Called by cancel_inflight()."""
        self._turn_cancel_requested = True
        self._turn_cancel_event.set()

    # ── Cap enforcement ───────────────────────────────────────────────────────

    async def _check_cap(self, user_text: str) -> None:
        """Increment the per-turn router invocation counter and enforce the cap.

        Raises RouterCapExceeded when the counter would exceed the configured
        cap. cap=0 disables the check.

        FP-0005: when ``safety.on_limit.mode`` is ``interactive`` /
        ``auto_extend`` and the cap is hit, ask the user / auto-extend
        before re-raising. On approval the cap is extended by the configured
        amount and the run continues.
        """
        from reyn.chat.session import RouterCapExceeded
        try:
            self._budget.check_and_increment_router_cap(user_text)
        except RouterCapExceeded as exc:
            decision = await self._limit_checkpoint_fn(
                kind="router_cap",
                prompt=(
                    f"Router hit the per-turn cap of {exc.cap} invocations. "
                    f"Allow more invocations this turn?"
                ),
                detail=(
                    f"count={exc.count} cap={exc.cap} "
                    f"last_reason={exc.last_reason}"
                ),
                extension_amount=1.0,
            )
            if not decision.allow_continue:
                raise
            # Approved — extend the cap and increment for THIS attempt.
            self._budget.extend_router_cap(int(decision.extension))
            self._budget.check_and_increment_router_cap(user_text)

    # ── Force-close overflow handling (#1092 PR-F2b) ─────────────────────────

    async def _force_close_handoff(self, loop: Any, user_text: str) -> None:
        """Consolidate the working context into a capped force-close summary.

        Fires F2a's durable covers-respecting reset so the re-entry slices
        ``[consolidation] + new turn`` instead of the raw head/tail that just
        overflowed.  ``user_text`` is unused here (the new message is re-applied
        by the loop's next ``_run_with_shrink``); kept for symmetry / future audit.
        """
        from reyn.chat.session import ChatMessage, _now_iso, _render_summary_for_storage

        resolved_model = self._resolver.resolve(self._model).model
        consolidation = await self._force_close_wrap_up(loop, resolved_model)
        covers = max(self._next_seq_fn() - 1, 0)
        structured = {"consolidation": consolidation}
        msg = ChatMessage(
            role="summary",
            content=_render_summary_for_storage(structured),
            ts=_now_iso(),
            meta={"structured": structured, "covers_through_seq": covers},
        )
        self._append_history_fn(msg)
        self._events.emit(
            "router_force_close_handoff",
            covers_through_seq=covers,
            consolidation_chars=len(consolidation),
        )

    async def _force_close_wrap_up(self, loop: Any, resolved_model: str) -> str:
        """Produce the capped consolidation via the force-close wrap-up call.

        Made to FIT by a bounded fallback that shrinks the input if the
        wrap-up itself would overflow (#1092 PR-F2b Fork 1 — wrap-up-fits).
        Input candidates, decreasing:
        ``[summary + raw_middle + tail]`` → ``[summary + tail]`` → ``[summary]``.
        If even summary-only overflows, the model is RUNTIME sub-viable → raise,
        surfaced as a genuine dead-end by the handoff loop.
        """
        from reyn.chat.session import _render_summary_for_storage
        from reyn.services.compaction.engine import (
            ContextOverflowError as _ContextOverflowError,
        )

        _head, _raw_middle, _tail, _summary_dict = (
            self._history_buffer.decompose_history_for_retry()
        )
        _summary_msg: list[dict] = []
        if _summary_dict:
            _summary_msg = [{
                "role": "assistant",
                "content": (
                    "[summary of earlier conversation]\n"
                    + _render_summary_for_storage(_summary_dict)
                ),
            }]
        _candidates = [
            _summary_msg + _raw_middle + _tail,
            _summary_msg + _tail,
            _summary_msg,
        ]
        _last_exc: Exception | None = None
        for _inp in _candidates:
            try:
                _result = await loop._force_close_call(
                    _inp, resolved_model=resolved_model
                )
                return _result.content or ""
            except Exception as _exc:
                if not any(kw in str(_exc).lower() for kw in (
                    "context", "token", "length", "limit", "too long", "too large",
                )):
                    raise
                _last_exc = _exc
        raise _ContextOverflowError(
            "force-close wrap-up overflowed even at summary-only "
            f"(runtime sub-viable model): {_last_exc}"
        )

    # ── Overflow-resilient router invocation ─────────────────────────────────

    async def _run_with_shrink(self, loop: Any, user_text: str) -> Any:
        """Run the router once with the reactive bounded-shrink ``retry_loop``.

        #1092 PR-F2b: returns the router usage, or raises ``_ContextOverflowError``
        when even the floor overflows (the terminal the F2b handoff loop catches
        to force-close). Rebuilds the history each call so a force-close re-entry
        sees the post-handoff (F2a-reset) slice.

        No ``router_context_overflow_unrecovered`` event here — the caller emits
        it ONLY when it actually gives up (cap reached / sub-viable model), so a
        recoverable handoff is not mislogged as a dead-end.
        """
        from reyn.chat.session import _render_summary_for_storage, _RouterUsageShim
        from reyn.services.compaction.engine import (
            ContextOverflowError as _ContextOverflowError,
        )
        from reyn.services.compaction.engine import (
            UnrecoveredError as _UnrecoveredError,
        )

        history = self._history_buffer.build_history()
        try:
            return await loop.run(user_text=user_text, history=history)
        except Exception as _exc:
            _exc_str = str(_exc).lower()
            if not any(
                kw in _exc_str
                for kw in ("context", "token", "length", "limit", "too long", "too large")
            ):
                raise
            self._events.emit(
                "router_context_overflow_detected", error=repr(_exc)
            )
            from reyn.services.compaction.engine import retry_loop as _retry_loop
            engine = self._compaction_controller._engine
            _head, _raw_middle, _tail, _summary_dict = (
                self._history_buffer.decompose_history_for_retry()
            )
            _new_msg = {"role": "user", "content": user_text}

            async def _router_main_call(*, SP, head, summary, tail, new_msg):
                _msgs = list(head)
                if summary:
                    _summary_text = _render_summary_for_storage(summary)
                    _msgs.append({
                        "role": "assistant",
                        "content": (
                            "[summary of earlier conversation]\n" + _summary_text
                        ),
                    })
                _msgs.extend(tail)
                try:
                    _usage = await loop.run(user_text=user_text, history=_msgs)
                except Exception as _call_exc:
                    if any(kw in str(_call_exc).lower() for kw in (
                        "context", "token", "length", "limit", "too long", "too large",
                    )):
                        raise _ContextOverflowError(str(_call_exc)) from _call_exc
                    raise
                return _RouterUsageShim(_usage)

            try:
                _shim = await _retry_loop(
                    SP=self._history_buffer.build_system_prompt(),
                    head=_head,
                    summary=_summary_dict,
                    raw_middle=_raw_middle,
                    tail=_tail,
                    new_msg=_new_msg,
                    cfg=self._compaction,
                    model=self._model,
                    engine=engine,
                    learner=self._token_learner,
                    main_call=_router_main_call,
                )
                return _shim.usage
            except (_ContextOverflowError, _UnrecoveredError) as _retry_exc:
                raise _ContextOverflowError(
                    f"Router context overflow after bounded shrink: {_retry_exc}"
                ) from _retry_exc

    # ── Main turn entry point ─────────────────────────────────────────────────

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        """Run RouterLoop for one user utterance.

        Enforces the per-turn cap, builds history, and calls RouterLoop.run().
        Does NOT modify history or outbox directly — RouterLoop calls host
        callbacks.

        Raises RouterCapExceeded when the per-turn cap is reached.
        """
        from reyn.chat.router_loop import EMPTY_STOP_RETRY_DIRECTIVE, RouterLoop
        from reyn.services.compaction.engine import (
            ContextOverflowError as _ContextOverflowError,
        )

        # #1468 / #1470: reset cancel flag + event at turn entry so an idle
        # cancel_inflight() call (Ctrl-C while no turn is running) is
        # spurious-safe and does not bleed into the next turn.
        self._turn_cancel_requested = False
        self._turn_cancel_event.clear()
        # FP-0005: now async (consults safety.on_limit on hit).
        await self._check_cap(user_text)
        # B51 NF-W6-3: plan_invalid self-correction cap, sourced from
        # safety.loop.plan_invalid_retries (default 1). When set to 0
        # the retry is disabled and the LLM sees the plain tool error.
        _plan_invalid_retries_cap = getattr(
            getattr(self._safety, "loop", None),
            "plan_invalid_retries",
            1,
        )
        # #1666: per-turn tool_call count cap (cost-bound) sourced from
        # safety.loop.max_tool_calls_per_turn (default 50). 0 = unlimited.
        _max_tool_calls_per_turn = getattr(
            getattr(self._safety, "loop", None),
            "max_tool_calls_per_turn",
            50,
        )
        loop = RouterLoop(
            host=self._router_host, chain_id=chain_id,
            # #1593 PR-2: select the chat-layer tool-use scheme (None → universal).
            scheme_name=self._chat_scheme_name,
            max_iterations=self._router_max_iterations,
            budget=self._budget_tracker,
            # #1440 followup: thread the run-once autonomy flag to the LIVE
            # chat-router SP path (router_loop build_system_prompt).
            non_interactive=self._non_interactive,
            # #187: hide excluded tools from the MAIN agent loop's LLM-visible
            # catalog.
            exclude_tools=self._exclude_tools,
            excluded_categories=self._excluded_categories,  # #1667
            # B43-NF-W6-1 / #187: chat router empty-stop retry — always-on +
            # uniform "resume" directive.
            empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE,
            empty_stop_retry_auto=True,
            plan_invalid_retries=_plan_invalid_retries_cap,
            # #1666: per-turn tool_call count cap (cost-bound).
            max_tool_calls_per_turn=_max_tool_calls_per_turn,
            # FP-0005: wire safety.on_limit so max_iterations exhaustion routes
            # through handle_limit_exceeded instead of flat-aborting.
            on_limit=getattr(self._safety, "on_limit", None),
        )
        # PR-N3: pre-frame context-overflow guard.
        await self._budget_advisor.maybe_force_compact(new_msg_text=user_text)

        # #1092 PR-F2b: bounded force-close handoff loop.
        _handoffs = 0
        while True:
            try:
                router_usage = await self._run_with_shrink(loop, user_text)
                break
            except _ContextOverflowError as _overflow_exc:
                _reserve = getattr(
                    self._router_host, "wrap_up_output_reserve", None
                )
                if _reserve is None or _handoffs >= _MAX_FORCE_CLOSE_HANDOFFS:
                    self._events.emit(
                        "router_context_overflow_unrecovered",
                        error=repr(_overflow_exc),
                    )
                    raise
                await self._force_close_handoff(loop, user_text)
                _handoffs += 1

        # F4 Bug 2 / F4 Bug 1: accumulate router LLM usage into per-session
        # totals via the gateway.
        if router_usage is not None:
            self._budget.add_router_usage(
                usage=router_usage,
                resolver=self._resolver,
                router_model_name=loop.router_model,
            )
