"""Budget / cost / rate-limit enforcement (PR22).

A process-shared `BudgetTracker` accumulates token + USD usage per agent,
per-chain per-skill spawn counts, and per-model call rates. Hooked into
LLM calls (pre-check refuses on hard cap, post-record updates counters)
and into skill spawns (refuses on per-chain cap).

Hybrid cap behavior:
  - hard_limit: refuse the next operation (subsequent calls return
    BudgetCheck.allowed=False)
  - warn at hard_limit * warn_ratio: emit one warn per dimension/key,
    pushed to the user as a status message and recorded in events.jsonl

Per P7: this is OS-level generic infrastructure — the dimension names
are not tied to any specific skill or domain.
"""
from __future__ import annotations
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from reyn.pricing import TokenUsage, estimate_cost


# ── exceptions ──────────────────────────────────────────────────────────────


class BudgetExceeded(Exception):
    """Raised by OSRuntime when a pre-call check refuses the LLM call."""

    def __init__(self, dimension: str, detail: str) -> None:
        super().__init__(detail)
        self.dimension = dimension
        self.detail = detail


# ── config ──────────────────────────────────────────────────────────────────


@dataclass
class CostLimitConfig:
    """A single hybrid-cap dimension. None hard_limit = unlimited."""

    hard_limit: float | None = None
    warn_ratio: float = 0.8

    @property
    def warn_threshold(self) -> float | None:
        if self.hard_limit is None or self.warn_ratio <= 0:
            return None
        return self.hard_limit * self.warn_ratio

    @property
    def is_active(self) -> bool:
        return self.hard_limit is not None


@dataclass
class CostConfig:
    """`cost:` — budget caps and rate limits (PR22)."""

    per_agent_tokens: CostLimitConfig = field(default_factory=CostLimitConfig)
    per_agent_cost_usd: CostLimitConfig = field(default_factory=CostLimitConfig)
    per_chain_skill_calls: CostLimitConfig = field(default_factory=CostLimitConfig)
    per_chain_skill_tokens: CostLimitConfig = field(default_factory=CostLimitConfig)
    rate_limit_per_minute: dict[str, int] = field(default_factory=dict)
    rate_limit_warn_ratio: float = 0.8


# ── check result ────────────────────────────────────────────────────────────


@dataclass
class BudgetCheck:
    allowed: bool = True
    warn_dimensions: list[str] = field(default_factory=list)
    hard_dimension: str | None = None
    detail: str = ""
    # Snapshot of current/limit values for the dimension that triggered
    # warn or hard. Used by formatters to build user-facing messages.
    context: dict = field(default_factory=dict)


# ── tracker ─────────────────────────────────────────────────────────────────


class BudgetTracker:
    """Process-wide accumulator + hybrid-cap enforcer.

    Counters live in memory and reset on process restart. `:budget reset`
    clears them mid-process. The tracker is single-thread / asyncio-safe
    by virtue of running in a single event loop (no internal locking).
    """

    def __init__(self, config: CostConfig) -> None:
        self._config = config
        self._agent_tokens: dict[str, int] = defaultdict(int)
        self._agent_cost_usd: dict[str, float] = defaultdict(float)
        self._chain_skill_calls: dict[tuple[str, str], int] = defaultdict(int)
        self._chain_skill_tokens: dict[tuple[str, str], int] = defaultdict(int)
        self._call_window: dict[str, deque[float]] = defaultdict(deque)
        self._warned: set[tuple[str, str]] = set()

    @property
    def config(self) -> CostConfig:
        return self._config

    # ── pre-call checks ─────────────────────────────────────────────────

    def check_pre_llm(
        self, *, model: str, agent: str | None,
    ) -> BudgetCheck:
        """Run before every LLM call. Returns allowed=False to refuse."""
        # 1. Rate limit (per model)
        rl_check = self._check_rate_limit(model)
        if not rl_check.allowed:
            return rl_check

        # 2. Per-agent token / cost — already-exceeded check
        if agent is not None:
            cap = self._config.per_agent_tokens
            if cap.is_active:
                used = self._agent_tokens[agent]
                if used >= cap.hard_limit:
                    return BudgetCheck(
                        allowed=False,
                        hard_dimension="per_agent_tokens",
                        detail=f"agent {agent!r}: tokens {used}/{int(cap.hard_limit)}",
                        context=self._agent_context(agent),
                    )
            cap = self._config.per_agent_cost_usd
            if cap.is_active:
                used = self._agent_cost_usd[agent]
                if used >= cap.hard_limit:
                    return BudgetCheck(
                        allowed=False,
                        hard_dimension="per_agent_cost_usd",
                        detail=f"agent {agent!r}: cost ${used:.2f}/${cap.hard_limit:.2f}",
                        context=self._agent_context(agent),
                    )
        return rl_check  # may carry warn dims

    def check_pre_spawn(self, *, chain_id: str, skill: str) -> BudgetCheck:
        """Run before spawning a skill from chat. Refuses on per-chain cap."""
        cap = self._config.per_chain_skill_calls
        if not cap.is_active:
            return BudgetCheck(allowed=True)
        used = self._chain_skill_calls[(chain_id, skill)]
        if used >= cap.hard_limit:
            return BudgetCheck(
                allowed=False,
                hard_dimension="per_chain_skill_calls",
                detail=(
                    f"skill {skill!r} already spawned {used} times in chain "
                    f"{chain_id} (hard limit {int(cap.hard_limit)})"
                ),
                context={
                    "skill": skill, "chain_id": chain_id,
                    "current": used, "hard": int(cap.hard_limit),
                },
            )
        warn_dims: list[str] = []
        threshold = cap.warn_threshold
        if threshold is not None and used + 1 >= threshold:
            key = ("per_chain_skill_calls", f"{chain_id}/{skill}")
            if key not in self._warned:
                self._warned.add(key)
                warn_dims.append("per_chain_skill_calls")
        return BudgetCheck(
            allowed=True,
            warn_dimensions=warn_dims,
            context={
                "skill": skill, "chain_id": chain_id,
                "current": used, "hard": int(cap.hard_limit),
            },
        )

    # ── recording ───────────────────────────────────────────────────────

    def record_llm(
        self,
        *,
        model: str,
        agent: str | None,
        usage: TokenUsage,
        chain_id: str | None = None,
        skill: str | None = None,
    ) -> BudgetCheck:
        """Update counters after a successful LLM call.

        Computes USD cost via litellm (`reyn.pricing.estimate_cost`).
        Returns a BudgetCheck whose warn_dimensions list any dimensions
        that newly crossed the warn threshold (for the caller to emit
        events / outbox notifications).
        """
        # rate limit window
        self._call_window[model].append(time.monotonic())

        warn_dims: list[str] = []
        cost_usd, _ = estimate_cost(model, usage)
        cost_usd = cost_usd or 0.0

        if agent is not None:
            new_tokens = self._agent_tokens[agent] + usage.total_tokens
            self._agent_tokens[agent] = new_tokens
            new_cost = self._agent_cost_usd[agent] + cost_usd
            self._agent_cost_usd[agent] = new_cost

            cap = self._config.per_agent_tokens
            if cap.is_active and cap.warn_threshold is not None:
                if new_tokens >= cap.warn_threshold:
                    self._maybe_warn(warn_dims, "per_agent_tokens", agent)

            cap = self._config.per_agent_cost_usd
            if cap.is_active and cap.warn_threshold is not None:
                if new_cost >= cap.warn_threshold:
                    self._maybe_warn(warn_dims, "per_agent_cost_usd", agent)

        if chain_id is not None and skill is not None:
            new_tok = self._chain_skill_tokens[(chain_id, skill)] + usage.total_tokens
            self._chain_skill_tokens[(chain_id, skill)] = new_tok
            cap = self._config.per_chain_skill_tokens
            if cap.is_active and cap.warn_threshold is not None:
                if new_tok >= cap.warn_threshold:
                    key = f"{chain_id}/{skill}"
                    self._maybe_warn(warn_dims, "per_chain_skill_tokens", key)

        return BudgetCheck(
            allowed=True,
            warn_dimensions=warn_dims,
            context=self._agent_context(agent) if agent else {},
        )

    def record_spawn(self, *, chain_id: str, skill: str) -> None:
        self._chain_skill_calls[(chain_id, skill)] += 1

    # ── reset / introspect ──────────────────────────────────────────────

    def reset_all(self) -> dict:
        """Clear every counter. Returns a dict describing what was reset
        (for `:budget reset` confirmation output)."""
        before = {
            "agent_tokens": dict(self._agent_tokens),
            "agent_cost_usd": dict(self._agent_cost_usd),
            "chain_skill_calls": dict(self._chain_skill_calls),
            "chain_skill_tokens": dict(self._chain_skill_tokens),
            "rate_window_sizes": {m: len(q) for m, q in self._call_window.items()},
        }
        self._agent_tokens.clear()
        self._agent_cost_usd.clear()
        self._chain_skill_calls.clear()
        self._chain_skill_tokens.clear()
        self._call_window.clear()
        self._warned.clear()
        return before

    def reset_chain(self, chain_id: str) -> None:
        """Clear state tied to a single chain (called when chain resolves)."""
        keys = [k for k in self._chain_skill_calls if k[0] == chain_id]
        for k in keys:
            self._chain_skill_calls.pop(k, None)
        keys = [k for k in self._chain_skill_tokens if k[0] == chain_id]
        for k in keys:
            self._chain_skill_tokens.pop(k, None)
        self._warned = {
            w for w in self._warned
            if not w[1].startswith(f"{chain_id}/")
        }

    def snapshot(self) -> dict:
        """Return a structured view used by `:cost` / `:budget` formatters."""
        return {
            "agent_tokens": dict(self._agent_tokens),
            "agent_cost_usd": dict(self._agent_cost_usd),
            "chain_skill_calls": {
                f"{cid}/{sk}": v
                for (cid, sk), v in self._chain_skill_calls.items()
            },
            "chain_skill_tokens": {
                f"{cid}/{sk}": v
                for (cid, sk), v in self._chain_skill_tokens.items()
            },
            "rate_window": {
                m: len([t for t in q if time.monotonic() - t <= 60])
                for m, q in self._call_window.items()
            },
            "config": self._config,
        }

    # ── internals ───────────────────────────────────────────────────────

    def _maybe_warn(
        self, warn_dims: list[str], dimension: str, key: str,
    ) -> None:
        wkey = (dimension, key)
        if wkey in self._warned:
            return
        self._warned.add(wkey)
        warn_dims.append(dimension)

    def _check_rate_limit(self, model: str) -> BudgetCheck:
        cap = self._config.rate_limit_per_minute.get(model)
        if cap is None:
            return BudgetCheck(allowed=True)
        now = time.monotonic()
        window = self._call_window[model]
        while window and now - window[0] > 60:
            window.popleft()
        used = len(window)
        if used >= cap:
            return BudgetCheck(
                allowed=False,
                hard_dimension="rate_limit",
                detail=f"model {model}: {used}/{cap} calls in last minute",
                context={"model": model, "current": used, "hard": cap},
            )
        warn_dims: list[str] = []
        warn_threshold = int(cap * self._config.rate_limit_warn_ratio)
        if warn_threshold > 0 and used >= warn_threshold:
            wkey = ("rate_limit", model)
            if wkey not in self._warned:
                self._warned.add(wkey)
                warn_dims.append("rate_limit")
        return BudgetCheck(
            allowed=True,
            warn_dimensions=warn_dims,
            context={"model": model, "current": used, "hard": cap},
        )

    def _agent_context(self, agent: str | None) -> dict:
        if agent is None:
            return {}
        return {
            "agent": agent,
            "tokens": self._agent_tokens.get(agent, 0),
            "cost_usd": round(self._agent_cost_usd.get(agent, 0.0), 4),
            "tokens_hard": self._config.per_agent_tokens.hard_limit,
            "cost_hard": self._config.per_agent_cost_usd.hard_limit,
        }


# ── user-visible formatters ─────────────────────────────────────────────────


def format_refusal_message(check: BudgetCheck, *, agent: Optional[str] = None) -> str:
    """Build the multi-line outbox message shown when a budget refuses a call."""
    dim = check.hard_dimension or "budget"
    lines: list[str] = []
    if dim == "rate_limit":
        ctx = check.context
        lines.append(
            f"[budget exceeded] rate limit for model "
            f"{ctx.get('model')!r} ({ctx.get('current')}/{ctx.get('hard')} calls/min)."
        )
    elif dim == "per_chain_skill_calls":
        ctx = check.context
        lines.append(
            f"[budget exceeded] skill {ctx.get('skill')!r} hit hard cap "
            f"({ctx.get('current')}/{ctx.get('hard')} calls in this chain)."
        )
    elif dim in ("per_agent_tokens", "per_agent_cost_usd"):
        ctx = check.context
        lines.append(
            f"[budget exceeded] agent {ctx.get('agent')!r} is over the hard limit."
        )
        lines.append("")
        if dim == "per_agent_tokens":
            lines.append(
                f"  Triggered:  per_agent_tokens "
                f"({ctx.get('tokens')}/{int(ctx.get('tokens_hard') or 0)})"
            )
            if ctx.get("cost_hard") is not None:
                lines.append(
                    f"  Also used:  ${ctx.get('cost_usd', 0):.2f} "
                    f"(limit: ${ctx.get('cost_hard'):.2f})"
                )
            else:
                lines.append(f"  Also used:  ${ctx.get('cost_usd', 0):.2f}")
        else:
            lines.append(
                f"  Triggered:  per_agent_cost_usd "
                f"(${ctx.get('cost_usd', 0):.2f}/${ctx.get('cost_hard', 0):.2f})"
            )
            if ctx.get("tokens_hard") is not None:
                lines.append(
                    f"  Also used:  {ctx.get('tokens')} tokens "
                    f"(limit: {int(ctx.get('tokens_hard'))})"
                )
            else:
                lines.append(f"  Also used:  {ctx.get('tokens')} tokens")
    else:
        lines.append(f"[budget exceeded] {check.detail}")
    lines.append("")
    lines.append("The next LLM call has been refused.")
    lines.append("")
    lines.append("What you can do:")
    lines.append("  • Raise the limit in `.reyn/config.yaml` (cost: section)")
    lines.append("  • Reset counters with `:budget reset`")
    lines.append("  • Restart `reyn chat` (limits are per-process)")
    lines.append("  • See current usage with `:budget`")
    return "\n".join(lines)


def format_warn_message(dimension: str, ctx: dict) -> str:
    """Build a 1-2 line outbox status when a warn threshold is crossed."""
    if dimension == "per_agent_tokens":
        return (
            f"[budget warn] agent {ctx.get('agent')!r}: "
            f"{ctx.get('tokens')} / {int(ctx.get('tokens_hard') or 0)} tokens "
            f"(${ctx.get('cost_usd', 0):.2f} so far)"
        )
    if dimension == "per_agent_cost_usd":
        return (
            f"[budget warn] agent {ctx.get('agent')!r}: "
            f"${ctx.get('cost_usd', 0):.2f} / ${ctx.get('cost_hard', 0):.2f} USD"
        )
    if dimension == "rate_limit":
        return (
            f"[budget warn] rate limit approaching for model "
            f"{ctx.get('model')}: {ctx.get('current')} / {ctx.get('hard')} calls/min"
        )
    if dimension == "per_chain_skill_calls":
        return (
            f"[budget warn] skill {ctx.get('skill')!r} approaching cap "
            f"({ctx.get('current')+1}/{ctx.get('hard')} in chain {ctx.get('chain_id')})"
        )
    if dimension == "per_chain_skill_tokens":
        return f"[budget warn] {dimension} approaching cap"
    return f"[budget warn] {dimension}"


def format_cost_line(snapshot: dict, agent: str) -> str:
    """`:cost` 1-line output for the attached agent."""
    tokens = snapshot["agent_tokens"].get(agent, 0)
    cost = snapshot["agent_cost_usd"].get(agent, 0.0)
    return f"{agent}: {tokens:,} tokens, ${cost:.4f}  (this session)"


def format_budget_full(snapshot: dict, attached: str | None) -> str:
    """`:budget` full breakdown across all dimensions."""
    cfg: CostConfig = snapshot["config"]
    lines: list[str] = ["Usage (process invocation):", ""]

    agents = sorted(set(snapshot["agent_tokens"]) | set(snapshot["agent_cost_usd"]))
    if not agents and attached is not None:
        agents = [attached]
    for agent in agents:
        marker = " (attached)" if agent == attached else ""
        lines.append(f"  {agent}{marker}")
        tok = snapshot["agent_tokens"].get(agent, 0)
        cost = snapshot["agent_cost_usd"].get(agent, 0.0)
        tok_cap = cfg.per_agent_tokens
        if tok_cap.is_active:
            warn = int(tok_cap.warn_threshold or 0)
            mark = "  ⚠ approaching" if tok >= warn > 0 else ""
            lines.append(
                f"    tokens:  {tok:>10,} / {int(tok_cap.hard_limit):,}  "
                f"(warn at {warn:,}){mark}"
            )
        else:
            lines.append(f"    tokens:  {tok:>10,}             (no cap)")
        cost_cap = cfg.per_agent_cost_usd
        if cost_cap.is_active:
            warn = (cost_cap.warn_threshold or 0)
            mark = "  ⚠ approaching" if cost >= warn > 0 else ""
            lines.append(
                f"    cost:    ${cost:>9.4f} / ${cost_cap.hard_limit:.2f}     "
                f"(warn at ${warn:.2f}){mark}"
            )
        else:
            lines.append(f"    cost:    ${cost:>9.4f}              (no cap)")
        lines.append("")

    if snapshot["chain_skill_calls"]:
        lines.append("  Per-chain skill calls:")
        cap = cfg.per_chain_skill_calls
        for key, used in sorted(snapshot["chain_skill_calls"].items()):
            if cap.is_active:
                lines.append(f"    {key}:  {used} / {int(cap.hard_limit)}")
            else:
                lines.append(f"    {key}:  {used}")
        lines.append("")

    if snapshot["rate_window"]:
        lines.append("  Rate limit (last minute):")
        for model, used in sorted(snapshot["rate_window"].items()):
            cap = cfg.rate_limit_per_minute.get(model)
            if cap is not None:
                warn = int(cap * cfg.rate_limit_warn_ratio)
                mark = "  ⚠" if used >= warn > 0 else ""
                lines.append(f"    {model}:  {used} / {cap}  (warn at {warn}){mark}")
            else:
                lines.append(f"    {model}:  {used}  (no cap)")
        lines.append("")

    lines.append("  Reset counters with `:budget reset`.")
    return "\n".join(lines)
