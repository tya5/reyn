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
from reyn.llm.pricing import CostBreakdown, TokenUsage


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
    ) -> None:
        self._tracker = budget_tracker
        self._events = events
        self._agent_name = agent_name
        self._total_usage: TokenUsage = TokenUsage()
        self._last_call_usage: TokenUsage = TokenUsage()
        self._total_cost_usd: float = 0.0
        # Cost-panel breakdown (Session scope, #cost-panel-breakdown):
        # cache-aware CostBreakdown accumulated turn-by-turn in
        # ``add_router_usage`` alongside ``_total_cost_usd``. Session-scoped
        # (this Session/process only) by construction — mirrors the existing
        # non-durable semantics of ``_total_usage``/``_total_cost_usd`` above.
        self._total_cost_breakdown: CostBreakdown = CostBreakdown()
        self._router_cap: int = default_router_cap
        self._router_invocations_this_turn: int = 0
        self._router_last_reason: str = ""

    # ── tracker passthrough ───────────────────────────────────────────────────

    @property
    def tracker(self):
        """The underlying process-shared BudgetTracker (or None)."""
        return self._tracker

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
        accumulated) on each call."""
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
        """
        if usage is None or usage.total_tokens == 0:
            return
        self._total_usage += usage
        self._last_call_usage = last_call_usage if last_call_usage is not None else usage
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

        Called at the top of each fresh turn (``_handle_user_message``,
        ``_handle_agent_request``). Re-entrant in-chain paths intentionally
        do NOT reset — their invocations count against the same budget.
        """
        self._router_invocations_this_turn = 0
        self._router_last_reason = ""

    def check_and_increment_router_cap(self, user_text: str) -> None:
        """Increment the per-turn router invocation counter and enforce the cap.

        Raises RouterCapExceeded after the ``cap``-th invocation and emits a
        ``router_retry_exhausted`` event with count + last_reason. cap<=0
        disables the check.
        """
        # Import here to avoid circular import at module load time.
        from reyn.runtime.errors import RouterCapExceeded

        if self._router_cap <= 0:
            return
        if self._router_invocations_this_turn >= self._router_cap:
            count = self._router_invocations_this_turn
            self._events.emit(
                "router_retry_exhausted",
                user_message=user_text[:200],
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
        """
        if self._tracker is None:
            return None
        from reyn.runtime.budget.budget import format_budget_full
        snap = self._tracker.snapshot()
        return format_budget_full(snap, attached=self._agent_name)

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
