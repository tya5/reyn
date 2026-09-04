"""BudgetGateway — per-session adapter on top of process-shared BudgetTracker
(extracted from Session wave 3 PR1).

BudgetTracker is a process-shared, ledger-backed object owned by the
AgentRegistry / startup config; it is NOT owned by this gateway. The gateway
holds a reference to it (or None for unlimited mode) and absorbs the
per-session bookkeeping that previously lived as scattered attributes on
Session: total_usage, total_cost_usd, router cap counter, last reason.
"""
from __future__ import annotations

from reyn.core.events.events import EventLog
from reyn.llm.pricing import CostBreakdown, EmbeddingCost, TokenUsage, estimate_embedding_cost


class BudgetGateway:
    """Per-session budget adapter on top of the process-shared BudgetTracker.

    Parameters
    ----------
    budget_tracker:
        The process-shared BudgetTracker, or None for unlimited mode.
        The gateway never owns or mutates the tracker — it is passed in by
        reference and used as-is.
    events:
        The session's EventLog.  Used to emit budget-related events (e.g.
        ``router_retry_exhausted``, ``budget_reset``).
    agent_name:
        Name of the owning agent; forwarded to tracker queries.
    default_router_cap:
        Maximum consecutive router invocations per user turn. Mirrors
        ``CostConfig.router_invocations_per_turn``.  cap<=0 disables check.
    """

    def __init__(
        self,
        *,
        budget_tracker,            # BudgetTracker | None
        events: EventLog,
        agent_name: str,
        default_router_cap: int = 3,
        # #4206 Slice B (#4724): ③ preference-axis live override for the
        # cost.*.warn_ratio keys, same callback shape as RouterHostAdapter's
        # own `warn_ratio_overrides_fn` — makes `/budget`'s display match
        # the SAME overrides gating this session's own warn events. None
        # (every pre-Slice-B caller) -> {} -> byte-identical display.
        warn_ratio_overrides_fn=None,  # Callable[[], dict[str, float]] | None
    ) -> None:
        self._tracker = budget_tracker
        self._events = events
        self._agent_name = agent_name
        self._warn_ratio_overrides_fn = warn_ratio_overrides_fn
        self._total_usage: TokenUsage = TokenUsage()
        self._last_call_usage: TokenUsage = TokenUsage()
        self._total_cost_usd: float = 0.0
        # Cost-panel breakdown (Session scope, #cost-panel-breakdown):
        # cache-aware CostBreakdown accumulated turn-by-turn in
        # ``add_router_usage`` alongside ``_total_cost_usd``. Session-scoped
        # (this Session/process only) by construction — mirrors the existing
        # non-durable semantics of ``_total_usage``/``_total_cost_usd`` above.
        self._total_cost_breakdown: CostBreakdown = CostBreakdown()
        # FP-0063 PC (Session scope): embedding spend is its OWN independent
        # aggregate, deliberately NOT folded into ``_total_cost_breakdown``
        # above (see ``EmbeddingCost``'s docstring — embedding is input-only /
        # uncacheable; mapping it onto CostBreakdown.prompt_cost would dilute
        # cache_hit_rate / cache_savings, which are chat-call-only figures).
        self._embedding_cost: EmbeddingCost = EmbeddingCost()
        self._router_cap: int = default_router_cap
        self._router_invocations_this_turn: int = 0
        self._router_last_reason: str = ""

    # ── tracker passthrough ───────────────────────────────────────────────────

    @property
    def tracker(self):
        """The underlying process-shared BudgetTracker (or None)."""
        return self._tracker

    # ── FP-0063 PC: embedding cost (Session scope, independent aggregate) ─────

    @property
    def embedding_cost(self) -> EmbeddingCost:
        """Cumulative INDEPENDENT embedding-spend aggregate for this session
        (all embed calls this session recorded via ``record_embedding``) —
        deliberately separate from ``total_cost_breakdown`` (the chat
        aggregate). See ``EmbeddingCost``'s docstring for why."""
        return self._embedding_cost

    def record_embedding(self, *, model: str, tokens: int) -> None:
        """Accumulate one embedding call's spend into this session's
        INDEPENDENT ``EmbeddingCost`` aggregate, AND forward it to the
        process-shared tracker for agent/project scope — the single recording
        entry point for the `embed` op (``ctx.budget_gateway``).

        This gateway is the only object that holds BOTH the tracker and the
        session's AGENT NAME, which is why the fan-out lives here rather than
        in the op handler. The tracker keys per-agent counters by agent NAME
        (matching ``record_llm`` / ``Registry.agent_embedding_cost``); the op
        handler has only ``ctx.agent_id`` — the FP-0016 HOST identity
        (``reyn/<hostname>``), a DIFFERENT value — so recording from there
        would file the spend under a key no per-scope reader ever looks up.

        Prices THIS call at its own model's rate (X6 mixed-model correctness)
        — never pools tokens across models and prices them afterwards at a
        single rate. An unpriced/unknown model contributes 0 to cost_usd but
        still counts toward tokens/calls, with ``unpriced_calls`` incremented
        (visible, not a silent $0.00).
        """
        if tokens <= 0:
            return
        cost_usd, _ = estimate_embedding_cost(model, tokens)
        self._embedding_cost += EmbeddingCost(
            cost_usd=cost_usd or 0.0,
            tokens=tokens,
            calls=1,
            unpriced_calls=0 if cost_usd is not None else 1,
        )
        # Agent/project scope. The tracker re-prices the call itself (same
        # per-call, own-model-rate path), so the two aggregates stay consistent
        # without this method having to pass a pre-computed figure through.
        if self._tracker is not None:
            self._tracker.record_embedding(
                model=model, agent=self._agent_name, tokens=tokens,
            )

    # ── per-session usage totals ──────────────────────────────────────────────

    @property
    def total_usage(self) -> TokenUsage:
        """Cumulative TokenUsage for this session (all LLM calls)."""
        return self._total_usage

    @property
    def total_cost_usd(self) -> float:
        """Cumulative USD cost for this session (all LLM calls)."""
        return self._total_cost_usd

    @property
    def total_cost_breakdown(self) -> CostBreakdown:
        """Cumulative cache-aware ``CostBreakdown`` for this session (Session
        scope for the cost panel's Input/Output/Saved/Saved% rows)."""
        return self._total_cost_breakdown

    @property
    def last_call_usage(self) -> TokenUsage:
        """TokenUsage of the single MOST RECENT LLM call only — distinct from
        BOTH the cumulative session total AND a turn-summed figure. A chat
        turn can make several LLM calls (tool-loop iterations), each re-
        sending nearly the same growing context; summing them would wildly
        overstate "how much of the context window is currently occupied"
        (status-bar ctx chip's headline figure). Overwritten (not
        accumulated) on each call.

        #4703/#4709/#5745: this field's writers today are :meth:`accumulate`
        (``session.py``'s own router-turn-result path), :meth:`add_router_usage`
        (``router_loop_driver.py``, once per whole turn), and
        :meth:`update_last_call_usage` (``router_loop.py``'s own
        ``on_call_usage`` wire, once per LLM call — #5745, the actual LIVE
        source most of the time a turn is in flight) — all three ROUTER
        calls. That is why the ctx chip's figure never needs a ``purpose``
        filter: compaction's own LLM call (a real, separate recorder —
        ``BudgetTracker`` via ``recorder=self._budget_tracker`` in
        ``session.py``, never this gateway) has no path to this field at
        all. If a FOURTH writer is ever wired to this gateway from a
        non-router call site (a purpose OTHER than the conversation turn),
        it will overwrite this property with that call's usage instead —
        silently, no exception, no raise — and the ctx chip will start
        showing that OTHER call's prompt size as if it were the
        conversation's own."""
        return self._last_call_usage

    def accumulate(self, result) -> None:
        """Accumulate a single LLM call result's tokens + cost into per-session
        totals. Mirrors Session._accumulate. ``result.token_usage`` is already
        a single call's usage (not turn-summed), so it doubles as last_call_usage.

        ``result.cost_breakdown`` (a ``CostBreakdown``) is OPTIONAL — this call
        site has no ``model`` in scope to derive one itself, unlike
        ``add_router_usage`` (the actual production accumulation path), so a
        caller that already computed one can pass it through; absent, the
        session's ``total_cost_breakdown`` simply does not grow from this call."""
        if result.token_usage is not None:
            self._total_usage += result.token_usage
            self._last_call_usage = result.token_usage
        if result.cost_usd is not None:
            self._total_cost_usd += result.cost_usd
        cost_breakdown = getattr(result, "cost_breakdown", None)
        if cost_breakdown is not None:
            self._total_cost_breakdown += cost_breakdown

    def update_last_call_usage(self, usage: "TokenUsage | None") -> None:
        """#5745: the LIVE per-call wire — ``router_loop.py``'s
        ``on_call_usage`` callback calls this immediately after EVERY LLM
        call, not just once at the whole turn's end. Touches ONLY
        ``_last_call_usage`` — never billing (``_total_usage``/
        ``_total_cost_usd``/``_total_cost_breakdown``, all still exclusively
        :meth:`add_router_usage`'s and :meth:`accumulate`'s own job, each
        called exactly as often as before this method existed) — calling
        this per-call is what a per-call call to ``add_router_usage``
        itself would have double(triple/...)-counted into billing, which
        is why this narrower method exists instead of just calling that
        one more often.

        ``None`` or an all-zero ``usage`` (a call whose provider echoed no
        usage at all — ``TokenUsage`` has no ``__bool__``, so a bare
        ``TokenUsage()`` is truthy despite being empty; ``total_tokens``
        is the real emptiness check, mirroring ``add_router_usage``'s own
        ``usage.total_tokens == 0`` guard) is a no-op — the previous
        value, however stale, is a more honest answer than silently
        zeroing what was actually the most recently OBSERVED figure."""
        if usage is not None and usage.total_tokens != 0:
            self._last_call_usage = usage

    def add_router_usage(
        self, *, usage: TokenUsage, last_call_usage: "TokenUsage | None" = None,
        resolver, router_model_name: str,
    ) -> None:
        """Accumulate router LLM usage with proxy-prefix stripping.

        Mirrors the inline block at session.py:3842-3858. Strips the proxy
        prefix (e.g. ``openai/``) from the resolved model name before
        passing it to ``estimate_cost`` so the litellm pricing lookup
        succeeds (F4 Bug 1).

        ``usage`` is the TURN-SUMMED total (all LLM calls this turn) and is
        what gets accumulated into total_usage/total_cost_usd (billing must
        count every call). ``last_call_usage`` — the single most recent call,
        from RouterLoop.last_call_usage — is what last_call_usage reports;
        they are NOT the same figure for a multi-tool-iteration turn.

        #5745 fix: a ``None`` (or omitted) ``last_call_usage`` no longer
        falls back to writing ``usage`` (the TURN-SUMMED figure) into the
        single-most-recent-call field — that fallback was itself the bug
        this issue named, now unreachable from its own ONE call site
        (``router_loop_driver.py`` always passes ``loop.last_call_usage``)
        but left in place before as a live footgun for any future caller
        that omitted it. ``None`` here means "nothing to say about the
        most recent call" — leave whatever :meth:`update_last_call_usage`
        (the #5745 per-call live wire) already wrote alone, never overwrite
        it with a turn aggregate.
        """
        if usage is None or usage.total_tokens == 0:
            return
        self._total_usage += usage
        if last_call_usage is not None:
            self._last_call_usage = last_call_usage
        # F4 Bug 1: strip proxy prefix so estimate_cost lookup succeeds.
        from reyn.llm.llm import proxy_kwargs
        from reyn.llm.pricing import estimate_cost, estimate_cost_breakdown
        resolved = resolver.resolve(router_model_name).model
        pricing_model = (
            resolved.split("/", 1)[1]
            if "/" in resolved and proxy_kwargs()
            else resolved
        )
        cost_usd, _ = estimate_cost(pricing_model, usage)
        if cost_usd is not None:
            self._total_cost_usd += cost_usd
        # Cost-panel breakdown (Session scope): accumulate the same call's
        # cache-aware component breakdown. None for an unpriced/unknown model
        # (mirrors estimate_cost's None-sentinel) — skip rather than treat
        # unknown as free.
        breakdown = estimate_cost_breakdown(pricing_model, usage)
        if breakdown is not None:
            self._total_cost_breakdown += breakdown

    # ── router cap ────────────────────────────────────────────────────────────

    @property
    def router_cap(self) -> int:
        """Configured cap on consecutive router invocations per turn."""
        return self._router_cap

    def reset_router_turn_counter(self) -> None:
        """Reset the per-turn router invocation counter and last reason.

        Called at the top of each fresh turn (``_handle_inbox_text``,
        ``_handle_agent_request``). Re-entrant in-chain paths intentionally
        do NOT reset — their invocations count against the same budget.
        """
        self._router_invocations_this_turn = 0
        self._router_last_reason = ""

    def check_and_increment_router_cap(self, user_text: "str | None") -> None:
        """Increment the per-turn router invocation counter and enforce the cap.

        Raises RouterCapExceeded after the ``cap``-th invocation and emits a
        ``router_retry_exhausted`` event with count + last_reason. cap<=0
        disables the check.

        ``user_text=None`` (#5678/#5686: this turn's content is already
        history's own tail, no separate seed) degrades the event's
        ``user_message`` preview to ``""`` rather than raising — the cap
        check itself never depended on the text.
        """
        # Import here to avoid circular import at module load time.
        from reyn.runtime.errors import RouterCapExceeded

        if self._router_cap <= 0:
            return
        if self._router_invocations_this_turn >= self._router_cap:
            count = self._router_invocations_this_turn
            self._events.emit(
                "router_retry_exhausted",
                user_message=(user_text or "")[:200],
                count=count,
                cap=self._router_cap,
                last_reason=self._router_last_reason,
            )
            raise RouterCapExceeded(
                count=count,
                cap=self._router_cap,
                last_reason=self._router_last_reason,
            )
        self._router_invocations_this_turn += 1

    def set_router_last_reason(self, reason: str) -> None:
        """Record the router's last decision reason for cap-exceeded messages."""
        self._router_last_reason = reason

    def extend_router_cap(self, additional: int) -> int:
        """FP-0005: extend the per-turn router cap by ``additional``.

        Used by the safety-limit checkpoint flow when the user / auto-extend
        approves a continuation past the original cap. Returns the new
        effective cap. ``additional <= 0`` is a no-op (FP-0003 semantics).
        """
        if additional <= 0:
            return self._router_cap
        self._router_cap += int(additional)
        return self._router_cap

    # ── slash-command formatters ──────────────────────────────────────────────

    def cost_line(self) -> str | None:
        """Return a single-line cost summary for ``/cost``.

        Returns None when tracker is None (unlimited mode).
        """
        if self._tracker is None:
            return None
        from reyn.runtime.budget.budget import format_cost_line
        snap = self._tracker.snapshot()
        return format_cost_line(snap, self._agent_name)

    def budget_full(self) -> str | None:
        """Return the full budget breakdown for ``/budget``.

        Returns None when tracker is None (unlimited mode).

        #4206 Slice B (#4724): the displayed "warn at" figures reflect the
        SAME ③ preference-axis overrides (``warn_ratio_overrides_fn``, when
        supplied) that gate this session's own warn events — not silently
        the project default.
        """
        if self._tracker is None:
            return None
        from reyn.runtime.budget.budget import format_budget_full
        snap = self._tracker.snapshot()
        _overrides = self._warn_ratio_overrides_fn() if self._warn_ratio_overrides_fn else None
        return format_budget_full(snap, attached=self._agent_name, warn_ratio_overrides=_overrides)

    def reset_all(self) -> dict | None:
        """Reset BudgetTracker if present, emit ``budget_reset`` event, and
        return a summary dict for slash output.

        Returns None when tracker is None (unlimited mode).
        """
        if self._tracker is None:
            return None
        before = self._tracker.reset_all()
        self._events.emit("budget_reset", before=before)
        return before


__all__ = ["BudgetGateway"]
