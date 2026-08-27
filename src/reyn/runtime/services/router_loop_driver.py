"""RouterLoopDriver — per-turn router loop orchestration for Session.

Owns:

  - run_turn(user_text, chain_id)
  - _run_with_shrink(loop, text)
  - _check_cap(user_text)
  - is_cancel_requested()
  - request_cancel()               — turn-cancel seam (called by cancel_inflight)

Cancel lifecycle (#1468): the cooperative-cancel flag lives here.
``request_cancel()`` is called by Session.cancel_inflight() for the turn
piece; ``is_cancel_requested()`` is polled at each run_loop iteration via the
RouterHostAdapter.turn_cancel_fn callback wired in Session.__init__.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

from reyn.runtime.session_pure import render_summary_for_storage

if TYPE_CHECKING:
    from reyn.config.chat import SafetyConfig


def _narrowing_per_iteration(safety: "SafetyConfig") -> bool:
    """Whether ``safety.threat_scan.capability_narrowing`` is at the ``iteration``
    rung (#3501). Delegates to the config object's own predicate rather than
    comparing the string here, so the vocabulary lives in exactly one place.

    #4525: direct attribute access, not a ``getattr`` guard. The prior
    docstring claimed the guard existed because "test hosts pass partial
    safety objects" — lead-coder's grep found this was true of exactly ONE
    test host (which has since been fixed to construct a real
    ``SafetyConfig()``, matching what production has ALWAYS passed:
    ``session.py``'s own ``safety or SafetyConfig()`` normalization means
    ``RouterLoopDriver``'s one real construction site, ``session.py:1471``,
    never sees anything but a fully-populated ``SafetyConfig``, whose own
    ``threat_scan`` field is never ``None`` by construction
    (``field(default_factory=ThreatScanConfig)``). A ``getattr`` guard
    protecting a shape nothing ever constructs is the shape CLAUDE.md's
    test-review Q3 calls out — the fix is removing the guard, not adding a
    test for it, per architect's revised call after the premise was
    falsified. A caller genuinely passing a wrong type now gets a loud
    ``AttributeError`` instead of a silent ``False``."""
    return bool(safety.threat_scan.narrowing_per_iteration())


class RouterLoopDriver:
    """Orchestrates the per-turn router loop for one Session.

    Constructed once per Session; all stateful orchestration that previously
    lived inline in Session._run_router_loop is concentrated here.
    """

    def __init__(
        self,
        *,
        router_host: Any,             # RouterHostAdapter
        safety: Any,                  # SafetyConfig — loop.max_tool_calls_per_turn
        router_max_iterations: int,
        budget_tracker: Any,          # BudgetTracker — for RouterLoop
        non_interactive: bool,
        exclude_tools: Any,           # set — for RouterLoop
        excluded_categories: Any = frozenset(),  # #1667 set — catalog categories for RouterLoop
        contextual_permission: Any = None,  # #1827 S3 ContextualPermission — for RouterLoop live tool gate; None = no narrowing
        contextual_for_turn_fn: Any = None,  # #1827 S4b: () -> ContextualPermission|None — per-turn effective contextual (context-auto); None → static contextual_permission (byte-identical)
        budget: Any,                  # BudgetGateway — cap + usage accounting
        resolver: Any,                # LLMResolver — model resolution
        compaction: Any,              # CompactionConfig — retry_loop cfg
        compaction_controller: Any,   # CompactionController — retry_loop engine
        token_learner: Any,           # TokenMultiplierLearner — retry_loop learner
        events: Any,                  # EventLog — emit events
        model_override_fn: "Callable[[], str | None]",  # () -> _model_override (None = unset)
        history_buffer: Any,          # RouterHistoryBuffer — history + SP
        budget_advisor: Any,          # ContextBudgetAdvisor — maybe_force_compact
        limit_checkpoint_fn: Callable,  # async; Session._handle_chat_limit_checkpoint
        next_seq_fn: Callable[[], int], # Session._next_seq reader
        append_history_fn: Callable,    # Session._append_history
        chat_scheme_name: "str | None" = None,  # #1593 PR-2: chat-layer ToolUseScheme name → RouterLoop(scheme_name=); None → universal default
        empty_stop_retry: bool = False,  # #4677: chat.empty_stop_retry (owner default False, 2026-08-14) — was hardcoded True below
        _loop_observer: "Callable | None" = None,  # Tier-2 test seam: called with the constructed RouterLoop before run
    ) -> None:
        self._router_host = router_host
        self._safety = safety
        self._router_max_iterations = router_max_iterations
        self._budget_tracker = budget_tracker
        self._non_interactive = non_interactive
        self._exclude_tools = exclude_tools
        self._excluded_categories = excluded_categories  # #1667
        self._contextual_permission = contextual_permission  # #1827 S3
        self._contextual_for_turn_fn = contextual_for_turn_fn  # #1827 S4b
        self._budget = budget
        self._resolver = resolver
        self._compaction = compaction
        self._compaction_controller = compaction_controller
        self._token_learner = token_learner
        self._events = events
        self._model_override_fn = model_override_fn
        self._loop_observer = _loop_observer
        self._history_buffer = history_buffer
        self._budget_advisor = budget_advisor
        self._limit_checkpoint_fn = limit_checkpoint_fn
        self._next_seq_fn = next_seq_fn
        self._append_history_fn = append_history_fn
        self._chat_scheme_name = chat_scheme_name  # #1593 PR-2
        self._empty_stop_retry = empty_stop_retry  # #4677
        # #1468: per-turn cooperative cancellation flag + asyncio.Event for
        # deep-cancel propagation into running subprocess ops (#1470).
        self._turn_cancel_requested: bool = False
        self._turn_cancel_event: asyncio.Event = asyncio.Event()
        # 0062: per-session structured-output override, set post-construction by
        # ``configure_structured_output`` (mirrors the existing ``_loop_observer``
        # Tier-2 test-seam pattern — a public post-construction configure call, NOT
        # a constructor kwarg thread-through every Session caller would need to
        # touch). None for every Session except an ephemeral agent-step spawn
        # whose ``AgentStep.schema`` is set — byte-identical otherwise.
        self._response_format: "dict | None" = None
        self._schema_validate_fn: "Any | None" = None
        self._max_schema_reprompt_attempts: int = 2
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

    # ── Structured output (0062) ─────────────────────────────────────────────

    def configure_structured_output(
        self, *,
        response_format: "dict | None",
        schema_validate_fn: "Any | None" = None,
        max_reprompt_attempts: int = 2,
    ) -> None:
        """Configure the NEXT ``run_turn`` call's answer turn to be a
        schema-constrained ``response_format`` call (0062 §2.1) instead of
        emitting the tool-turn's own free-form text. Called by
        ``session_api.run_agent_step`` right after spawning a
        ``schema``-bearing agent-step's ephemeral session, before its one
        ``MessageBus.request`` turn — mirrors the existing ``_loop_observer``
        Tier-2 seam's "configure the constructed session before its turn"
        shape, but is production wiring (not test-only): every OTHER Session
        never calls this, so ``RouterLoop(response_format=None, ...)`` (this
        driver's default) keeps every other turn byte-identical."""
        self._response_format = response_format
        self._schema_validate_fn = schema_validate_fn
        self._max_schema_reprompt_attempts = max(0, int(max_reprompt_attempts))

    # ── Model resolution ──────────────────────────────────────────────────────

    def _effective_router_model_class(self) -> str:
        """Return the model class to use for router LLM calls this turn.

        /model override wins when set; otherwise falls back to the router
        purpose-class (class_for_purpose("router"), which honours the
        operator's model_class_by_purpose config — byte-identical to the
        pre-/model behaviour when no override is active).
        """
        override = self._model_override_fn()
        if override:
            return override
        from reyn.llm.model_resolver import resolve_purpose_class
        return resolve_purpose_class(None, self._resolver, "router")

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
        from reyn.runtime.errors import RouterCapExceeded
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

    # ── Overflow-resilient router invocation ─────────────────────────────────

    async def _run_with_shrink(self, loop: Any, user_text: str) -> Any:
        """Run the router once with the reactive bounded-shrink ``retry_loop``.

        Returns the router usage, or raises ``_ContextOverflowError``
        (the context window is genuinely too small) or ``_UnrecoveredError``
        (shrinking recovered the SAME cause repeatedly without resolving
        it — #4885: e.g. an HTTP 413 request-body-byte limit, a DIFFERENT
        axis from token count, which shrinking cannot address) when even
        the floor overflows — the caller (#4381 PR-4) treats either as
        unrecovered on the first attempt; there is no consolidating retry
        loop here anymore (#1092 PR-F2b's force-close handoff, removed).
        Still rebuilds the history each call — a plain re-entry after ANY
        earlier turn's edits (compaction, etc.), not specifically a
        force-close one.

        No ``router_context_overflow_unrecovered`` event here — the caller
        is the one that emits it, on the overflow it treats as terminal.

        #5256: a provider usage-window/plan quota exhaustion is checked
        FIRST and re-raised UNWRAPPED without entering the shrink loop at
        all — shrinking cannot fix a clock-based limit, and trying anyway
        (compaction's own LLM call) spends more of the same exhausted
        quota. This is NOT one of the two overflow outcomes above; the
        caller's own except clause does not catch it, so it reaches
        Session's generic exception handler, which keeps the session
        alive (owner ruling).
        """
        from reyn.runtime.usage_shim import _RouterUsageShim
        from reyn.services.compaction.engine import (
            ContextOverflowError as _ContextOverflowError,
        )
        from reyn.services.compaction.engine import (
            UnrecoveredError as _UnrecoveredError,
        )
        from reyn.services.compaction.engine import (
            is_context_overflow_error as _is_context_overflow_error,
        )
        # #4954 (b): `UnrecoveredError` IS caught here now, but only to
        # read `.saw_byte_limit` and trigger a real compaction as a side
        # effect (see the except block below) — it is always re-raised
        # unchanged, so it still propagates unwrapped to `run_turn`'s own
        # widened except exactly as #4885 established. This is not a
        # reversion of that fix.

        # #4995/#5267: dispatched to a worker thread rather than run inline
        # on this coroutine's own turn — build_history() re-serialises every
        # elide-candidate turn (path-ref image materialise included, #3185's
        # own measurement) every call, a cost proportional to session length
        # that otherwise runs synchronously on the SAME event loop the TUI's
        # own frame scheduling shares. `asyncio.to_thread` suspends THIS
        # coroutine at the `await` (the GIL still time-slices with the
        # default ~5ms `sys.getswitchinterval()`, so the loop gets scheduled
        # while the thread runs) — same shape as this codebase's own
        # existing `voice.py` precedent ("Inference is dispatched to
        # asyncio.to_thread so the Textual event loop ...").
        #
        # `expected_owner` MUST be captured here, before the dispatch — see
        # RouterHistoryBuffer.build_history's own docstring and #5267:
        # cancel_inflight()'s hard `Task.cancel()` can now land INSIDE this
        # await (impossible when build_history() was purely synchronous),
        # and a cancelled `to_thread` await does not stop the underlying
        # worker thread — it keeps running and, without this ownership
        # check, could race a NEXT turn's own build_history() call over the
        # SAME shared `_cached_elide_*` fields. `asyncio.current_task()`
        # from inside this coroutine IS the session's `_turn_owner_task`
        # (this method runs on that task) — captured here, not on the
        # worker thread, where there is no running task to ask.
        #
        # #5267 ⚠️ WHOEVER CHANGES THIS CALL CHAIN'S SHAPE, READ THIS: that
        # identity holds ONLY because every step from
        # `Session.run_one_iteration`'s `self._turn_owner_task =
        # asyncio.create_task(self._run_turn_body(...))` down to THIS line
        # is a plain `await` — verified by grepping every file on the path
        # (`session.py`, `inter_agent_messaging.py`, this file) for
        # `create_task`/`ensure_future` and confirming the only hits are
        # either that ONE task-creation site itself or on entirely
        # unrelated paths (intervention re-dispatch, buffered-answer
        # persistence) — see #5267's own PR body for the exact commands
        # run and their output. If a FUTURE change inserts a
        # `create_task`/`ensure_future` ANYWHERE between those two points,
        # `asyncio.current_task()` here silently stops being
        # `_turn_owner_task` — `expected_owner` would then ALWAYS mismatch
        # the live owner, and EVERY `build_history()` call would silently
        # stop updating the shared incremental cache (a real perf
        # regression, a full recompute every turn) with NOTHING going red
        # to say so. This is the single point of fragility the ownership
        # design has — see #5267's own disclosure of this as an absence,
        # not a solved problem.
        _expected_owner = asyncio.current_task()
        history = await asyncio.to_thread(
            self._history_buffer.build_history, expected_owner=_expected_owner,
        )
        try:
            return await loop.run(user_text=user_text, history=history)
        except Exception as _exc:
            # #5256: a provider usage-window/plan quota exhaustion is NEVER
            # shrinkable — it is time-based, not input-size-based — and
            # entering retry_loop below would spend more of the SAME
            # exhausted quota on compaction's own LLM call (the real
            # incident: 2 agents x 2 turns x 3 shrink attempts = 12 wasted
            # calls, each itself hitting the same 429). Without this gate,
            # a RateLimitError whose message happens to contain a context-
            # overflow keyword (e.g. "usage LIMIT reached") matches is_
            # context_overflow_error's own keyword fallback and gets
            # misdiagnosed as "context too large" — the exact defect this
            # issue reports (see this file's own witness in test_5256_
            # quota_not_context_overflow.py, which pins that a quota
            # exception DOES classify as overflow today, proving this
            # gate is load-bearing, not decorative).
            #
            # Checked BEFORE is_context_overflow_error, but not because
            # ordering changes the OUTCOME (it doesn't — that predicate
            # calling ensure_litellm_ready_or_defer() to check for
            # litellm.ContextWindowExceededError first, then this check
            # right after, would still end in the same re-raise). It's
            # checked first so the quota path never pays for warming
            # litellm at all — a plain attribute read on exc.body, no
            # import, no warm-up cost, for a cause that was never going to
            # be treated as an overflow either way.
            #
            # Re-raising here (never wrapped in ContextOverflowError/
            # UnrecoveredError) means it propagates to run_turn's own
            # except, which does NOT catch a bare RateLimitError, then to
            # Session._handle_inbox_text's generic catch-all — which
            # already does the right thing for an un-wrapped exception:
            # surface it via the outbox (classify_router_error) and
            # return normally, keeping the session alive (owner ruling,
            # #5256: quota exhaustion must never end the session).
            from reyn.runtime.error_format import (
                is_quota_exhausted_error as _is_quota_exhausted_error,
            )
            if _is_quota_exhausted_error(_exc):
                raise
            # #3783 stage 1: single shared predicate (was an inline copy).
            if not _is_context_overflow_error(_exc):
                raise
            self._events.emit(
                "router_context_overflow_detected", error=repr(_exc)
            )
            from reyn.services.compaction.engine import retry_loop as _retry_loop
            engine = self._compaction_controller._engine
            _head, _raw_middle, _tail, _summary_dict, _ = (
                self._history_buffer.decompose_history_for_retry()
            )
            _new_msg = {"role": "user", "content": user_text}

            async def _router_main_call(*, SP, head, summary, tail, new_msg):
                _msgs = list(head)
                if summary:
                    _summary_text = render_summary_for_storage(summary)
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
                    # #3783 stage 1: single shared predicate (was an inline
                    # copy) — closes over the outer method's own import.
                    if _is_context_overflow_error(_call_exc):
                        raise _ContextOverflowError(str(_call_exc)) from _call_exc
                    raise
                return _RouterUsageShim(_usage)

            # #4885 (architect finding, #4381's own late-stage remainder):
            # this used to catch BOTH `_ContextOverflowError` (window too
            # small) and `_UnrecoveredError` (shrinking recovered the SAME
            # cause repeatedly without resolving it — a MISCLASSIFICATION,
            # per retry_loop's own docstring) and re-raise both as a single
            # `_ContextOverflowError("Router context overflow after bounded
            # shrink: ...")`. That merge is the reported defect: it renamed
            # `_UnrecoveredError`'s correct diagnosis ("shrink can't fix
            # this cause") into the WRONG one ("the context window is too
            # small") — an HTTP 413 (a request-BODY-BYTE limit) recovers
            # via this exact path and got relabelled as a token-overflow.
            # Let each propagate as itself; `run_turn`'s own except below
            # widened to catch both (same audit event, same `repr(exc)`
            # field — which now correctly names `UnrecoveredError` instead
            # of always `ContextOverflowError`, no new event kind needed).
            try:
                _shim = await _retry_loop(
                    SP=self._history_buffer.build_system_prompt(),
                    head=_head,
                    summary=_summary_dict,
                    raw_middle=_raw_middle,
                    tail=_tail,
                    new_msg=_new_msg,
                    cfg=self._compaction,
                    model=self._effective_router_model_class(),
                    engine=engine,
                    learner=self._token_learner,
                    main_call=_router_main_call,
                    max_iterations=self._compaction.max_shrink_iterations,
                    # #4957: operator-tunable escape valve (chat.compaction.
                    # max_shrink_iterations) — was previously always the
                    # signature default (8) here, with no way to raise it.
                    # Distinct from this class's own `_router_max_iterations`
                    # (RouterLoop's tool-call loop bound, unrelated) —
                    # `self._compaction` is retry_loop's OWN config, not
                    # this driver's.
                )
            except _UnrecoveredError as _unrecovered:
                # #4954 (b), architect-ruled: on a BYTE-limit exhaustion
                # (an HTTP 413 that recurred all the way to retry_loop's
                # own terminal condition), trigger a REAL compaction here
                # — in the driver's except block, not inside retry_loop
                # itself (retry_loop stays a pure TRANSPORT operation;
                # compaction is the SEMANTIC operation that actually
                # retires history entries, Session's own docstring:
                # "the only operation meant to retire an entry"). Routed
                # through `force_compact_now` — the SAME durable-watermark
                # path `ContextBudgetAdvisor`'s pre-frame guard already
                # uses (`_durable_active_history_after`-backed,
                # continuous from the last real `covers_through_seq`) —
                # deliberately NOT `retry_loop`'s own compaction result:
                # its `covers` can cover only `raw_middle` while skipping
                # `head` entirely, which is not continuous from the
                # previous watermark and would silently mark the OLDEST
                # unsummarized part of history "covered" without ever
                # summarizing it (exactly owner's own real-machine shape).
                #
                # This turn still fails — re-raised below unchanged. What
                # this buys is the NEXT turn: a real compaction now
                # advances the watermark, so a persistent 413 becomes "one
                # turn fails, not every turn" once paired with #4958 (the
                # projection that reads the advanced watermark back). No
                # new infinite-loop guard needed — force_compact_now
                # already returns immediately on `self._compacting` (a
                # concurrent pass) or zero candidates (nothing left to
                # compact = terminal), and this except fires at most once
                # per turn.
                #
                # #4885's own token-overflow pre-trigger already covers
                # non-byte-limit overflow before it would ever reach here
                # — a token overflow that STILL reaches this point means
                # the pre-trigger's own estimate was wrong, which the
                # existing adaptive learner (not a second compaction
                # trigger here) is the correct fix for.
                if _unrecovered.saw_byte_limit:
                    await self._compaction_controller.force_compact_now()
                raise
            return _shim.usage

    # ── Main turn entry point ─────────────────────────────────────────────────

    async def run_turn(self, user_text: str, chain_id: str) -> None:
        """Run RouterLoop for one user utterance.

        Enforces the per-turn cap, builds history, and calls RouterLoop.run().
        Does NOT modify history or outbox directly — RouterLoop calls host
        callbacks.

        Raises RouterCapExceeded when the per-turn cap is reached.
        """
        from reyn.runtime.router_loop import EMPTY_STOP_RETRY_DIRECTIVE, RouterLoop
        from reyn.services.compaction.engine import (
            ContextOverflowError as _ContextOverflowError,
        )
        from reyn.services.compaction.engine import (
            UnrecoveredError as _UnrecoveredError,
        )

        # #1468 / #1470: reset cancel flag + event at turn entry so an idle
        # cancel_inflight() call (Ctrl-C while no turn is running) is
        # spurious-safe and does not bleed into the next turn.
        self._turn_cancel_requested = False
        self._turn_cancel_event.clear()
        # FP-0005: now async (consults safety.on_limit on hit).
        await self._check_cap(user_text)
        # #1666: per-turn tool_call count cap (cost-bound) sourced from
        # safety.loop.max_tool_calls_per_turn (default 50). 0 = unlimited.
        _max_tool_calls_per_turn = getattr(
            getattr(self._safety, "loop", None),
            "max_tool_calls_per_turn",
            50,
        )
        loop = RouterLoop(
            host=self._router_host, chain_id=chain_id,
            # /model override wins when set; None → RouterLoop resolves router-purpose-class default.
            router_model=self._model_override_fn(),
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
            # #1827 S4b: per-turn effective contextual — when untrusted external
            # content is live in context (context-auto), the static narrowing is
            # composed with the minimal _untrusted profile; otherwise the static
            # S3 contextual (byte-identical). The fn is provided by Session.
            contextual_permission=(
                self._contextual_for_turn_fn()
                if self._contextual_for_turn_fn is not None
                else self._contextual_permission
            ),
            # B43-NF-W6-1 / #187: chat router empty-stop retry + uniform
            # "resume" directive. #4677 (owner, 2026-08-14): the auto-retry
            # switch is now config-driven (chat.empty_stop_retry, default
            # False) rather than hardcoded True — see that field's own
            # docstring for the incident + tradeoff. The directive itself
            # is still always threaded (it is inert unless
            # empty_stop_retry_auto is True), so a config-driven re-enable
            # needs no other change here.
            empty_stop_retry_directive=EMPTY_STOP_RETRY_DIRECTIVE,
            empty_stop_retry_auto=self._empty_stop_retry,
            # #1666: per-turn tool_call count cap (cost-bound).
            max_tool_calls_per_turn=_max_tool_calls_per_turn,
            # FP-0005: wire safety.on_limit so max_iterations exhaustion routes
            # through handle_limit_exceeded instead of flat-aborting.
            on_limit=getattr(self._safety, "on_limit", None),
            # 0062: None for every Session except a schema-bearing agent-step spawn
            # (configure_structured_output called before this run_turn).
            response_format=self._response_format,
            schema_validate_fn=self._schema_validate_fn,
            max_schema_reprompt_attempts=self._max_schema_reprompt_attempts,
            # #1909 / #3501 (OPT-IN, default off —
            # safety.threat_scan.capability_narrowing): only thread the
            # per-iteration re-resolve callable (+ its un-narrowed identity
            # baseline) into RouterLoop at the TOP rung of the ladder
            # (``iteration``). At ``off`` and ``turn`` neither kwarg is passed
            # (both stay None on the RouterLoop side) → RouterLoop's
            # per-iteration re-resolve block is a no-op, and the narrowing is
            # whatever the turn-boundary resolve produced.
            **(
                {
                    "intra_turn_contextual_for_turn_fn": self._contextual_for_turn_fn,
                    "contextual_static_baseline": self._contextual_permission,
                }
                if (
                    _narrowing_per_iteration(self._safety)
                    and self._contextual_for_turn_fn is not None
                )
                else {}
            ),
        )
        if self._loop_observer:
            self._loop_observer(loop)
        # PR-N3: pre-frame context-overflow guard.
        await self._budget_advisor.maybe_force_compact(new_msg_text=user_text)

        # #4381 PR-4 (owner ruling, verbatim: "２の force close 廃止して
        # spill にしよう。予算のための force close は残すで良い"): the
        # #1092 PR-F2b bounded handoff-and-retry loop is gone — an overflow
        # that survives ``_run_with_shrink``'s own shrink attempts is
        # unrecovered on the FIRST attempt, no consolidating retry. The
        # #4381 family (tool-result spill) is what now keeps an
        # oversized single result from reaching this point at all.
        try:
            router_usage = await self._run_with_shrink(loop, user_text)
        # #4885: widened from `_ContextOverflowError` alone — `_run_with_
        # shrink` no longer merges `_UnrecoveredError` (shrinking recovered
        # the SAME cause repeatedly without resolving it, e.g. an HTTP 413
        # request-body-byte limit) into `_ContextOverflowError` (the context
        # window itself is too small). Both still reach this ONE event with
        # the SAME schema — `repr(_overflow_exc)` now correctly names
        # whichever one actually happened instead of always the latter.
        except (_ContextOverflowError, _UnrecoveredError) as _overflow_exc:
            self._events.emit(
                "router_context_overflow_unrecovered",
                error=repr(_overflow_exc),
            )
            raise

        # F4 Bug 2 / F4 Bug 1: accumulate router LLM usage into per-session
        # totals via the gateway.
        if router_usage is not None:
            self._budget.add_router_usage(
                usage=router_usage,
                last_call_usage=loop.last_call_usage,
                resolver=self._resolver,
                router_model_name=loop.router_model,
            )
