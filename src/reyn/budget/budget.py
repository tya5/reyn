"""Budget / cost / rate-limit enforcement (PR22 + PR25).

A process-shared `BudgetTracker` accumulates token + USD usage per agent,
per-chain per-skill spawn counts, and per-model call rates. Hooked into
LLM calls (pre-check refuses on hard cap, post-record updates counters)
and into skill spawns (refuses on per-chain cap).

PR25 adds persistent daily / monthly quota enforcement via a JSONL ledger
(.reyn/state/budget_ledger.jsonl). On startup call `tracker.hydrate(path)`
to re-aggregate today's / this month's usage from the ledger. Every
`record_llm()` call appends a line to the ledger (fsync'd for durability).

Hybrid cap behavior:
  - hard_limit: refuse the next operation (subsequent calls return
    BudgetCheck.allowed=False)
  - warn at hard_limit * warn_ratio: emit one warn per dimension/key,
    pushed to the user as a status message and recorded in events.jsonl

Per P7: this is OS-level generic infrastructure — the dimension names
are not tied to any specific skill or domain.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from reyn.llm.pricing import TokenUsage, estimate_cost

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
    """A single hybrid-cap dimension. None hard_limit = unlimited.

    FP-0003: ``ask_on_exceed`` opts a dimension into the user-approval
    flow on hard-limit hit. When True, instead of refusing immediately,
    the budget gateway prompts the user via ``InterventionBus.ask`` and
    extends the chain's effective cap by ``extension_calls`` (or
    ``extension_tokens`` for token-axis dimensions) on approval.
    Default False preserves the pre-FP-0003 hard-refuse behaviour.
    """

    hard_limit: float | None = None
    warn_ratio: float = 0.8
    # FP-0003: opt-in user-approval flow on hard-limit hit.
    ask_on_exceed: bool = False
    # FP-0003: how much to extend the chain cap by on approval (only
    # consulted when ``ask_on_exceed`` is True). For per_chain_skill_calls
    # this is a count; for per_chain_skill_tokens it is a token count.
    extension_calls: int = 0

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
    """`cost:` — budget caps and rate limits (PR22 + PR25)."""

    per_agent_tokens: CostLimitConfig = field(default_factory=CostLimitConfig)
    per_agent_cost_usd: CostLimitConfig = field(default_factory=CostLimitConfig)
    per_chain_skill_calls: CostLimitConfig = field(default_factory=CostLimitConfig)
    per_chain_skill_tokens: CostLimitConfig = field(default_factory=CostLimitConfig)
    rate_limit_per_minute: dict[str, int] = field(default_factory=dict)
    rate_limit_warn_ratio: float = 0.8
    # Hard cap on consecutive skill_router invocations within a single user
    # turn (or top-level agent_request). Prevents runaway re-routing loops
    # such as the S4 dogfood incident (16 invocations / 245k prompt tokens
    # for one paste). 0 disables the cap (not recommended).
    router_invocations_per_turn: int = 3
    # PR25: persistent daily / monthly quota (reset automatically at period boundary)
    daily_tokens: CostLimitConfig = field(default_factory=CostLimitConfig)
    daily_cost_usd: CostLimitConfig = field(default_factory=CostLimitConfig)
    monthly_tokens: CostLimitConfig = field(default_factory=CostLimitConfig)
    monthly_cost_usd: CostLimitConfig = field(default_factory=CostLimitConfig)


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


# ── period helpers ──────────────────────────────────────────────────────────


def _period_key(ts: float, kind: str) -> tuple[str, str]:
    """Return a period key tuple for the given POSIX timestamp.

    kind="day"   → ("day",   "2026-05-02")
    kind="month" → ("month", "2026-05")

    Uses local time (time.localtime) — no external TZ config needed.
    """
    lt = time.localtime(ts)
    if kind == "day":
        return ("day", f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}")
    if kind == "month":
        return ("month", f"{lt.tm_year:04d}-{lt.tm_mon:02d}")
    raise ValueError(f"unknown period kind: {kind!r}")


def _current_period_key(kind: str) -> tuple[str, str]:
    return _period_key(time.time(), kind)


# ── persistent ledger ───────────────────────────────────────────────────────


class BudgetLedger:
    """Append-only JSONL ledger for per-LLM-call budget records (PR25).

    One record per line:
        {"ts": "2026-05-02T10:23:00+09:00", "agent": "alice",
         "model": "...", "tokens": 300, "cost_usd": 0.0023}

    Records are fsync'd on append so a process crash cannot roll back a
    completed LLM call and under-count quota usage.

    This class is synchronous and not asyncio-aware — all writes are tiny
    and complete in microseconds.  The asyncio event loop is never blocked
    meaningfully.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(
        self,
        *,
        agent: str | None,
        model: str,
        tokens: int,
        cost_usd: float,
    ) -> None:
        """Append one record and fsync."""
        # Build ISO-8601 timestamp with local UTC offset.
        now = time.time()
        lt = time.localtime(now)
        offset_sec = lt.tm_gmtoff  # seconds east of UTC
        sign = "+" if offset_sec >= 0 else "-"
        offset_abs = abs(offset_sec)
        offset_str = f"{sign}{offset_abs // 3600:02d}:{(offset_abs % 3600) // 60:02d}"
        ts_str = (
            f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
            f"T{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
            f"{offset_str}"
        )
        record: dict = {
            "ts": ts_str,
            "agent": agent,
            "model": model,
            "tokens": tokens,
            "cost_usd": cost_usd,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        # Guard against partial writes from a previous crash (no trailing newline).
        need_lead = self._needs_lead_newline()
        with self._path.open("a", encoding="utf-8") as f:
            if need_lead:
                f.write("\n")
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def _needs_lead_newline(self) -> bool:
        if not self._path.is_file():
            return False
        try:
            size = self._path.stat().st_size
        except OSError:
            return False
        if size == 0:
            return False
        try:
            with self._path.open("rb") as f:
                f.seek(-1, 2)
                return f.read(1) != b"\n"
        except OSError:
            return False

    def iter_records(self):
        """Yield parsed record dicts; skip broken / non-dict lines."""
        if not self._path.is_file():
            return
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                yield entry


# ── ISO-8601 timestamp parser ───────────────────────────────────────────────


def _parse_iso_ts(ts_str: str) -> float:
    """Parse an ISO-8601 timestamp (with +HH:MM offset) → POSIX float.

    Handles the format written by BudgetLedger.append:
      "2026-05-02T10:23:00+09:00"

    Raises ValueError on parse failure (caller should skip the record).
    """
    # datetime.fromisoformat supports timezone offsets in Python 3.7+.
    from datetime import datetime, timezone
    # Python 3.10 accepts "+09:00" directly; earlier versions need workaround.
    # Use a simple manual parse to stay compatible with 3.8+.
    if len(ts_str) >= 19:
        # Try stdlib first (3.7+ handles +HH:MM in Python 3.11+)
        try:
            dt = datetime.fromisoformat(ts_str)
            return dt.timestamp()
        except ValueError:
            pass
    # Fallback: strip offset manually and apply it.
    # Format: "2026-05-02T10:23:00+09:00" (25 chars)
    import re
    m = re.match(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
        r"([+-])(\d{2}):(\d{2})$",
        ts_str,
    )
    if not m:
        raise ValueError(f"cannot parse ts: {ts_str!r}")
    dt_naive_str, sign, hh, mm = m.groups()
    from datetime import datetime, timedelta
    dt_naive = datetime.strptime(dt_naive_str, "%Y-%m-%dT%H:%M:%S")
    offset = timedelta(hours=int(hh), minutes=int(mm))
    if sign == "-":
        offset = -offset
    tz = timezone(offset)
    dt = dt_naive.replace(tzinfo=tz)
    return dt.timestamp()


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
        # FP-0003: per-(chain_id, skill) extensions granted via the
        # ``ask_on_exceed`` user-approval flow. The effective hard limit
        # for a (chain, skill) pair is ``cap.hard_limit + extensions[key]``.
        # Tracked separately from ``_chain_skill_calls`` so the counter
        # itself remains a simple monotonic spawn count, and so the
        # extension carries clean audit semantics ("user approved +N
        # spawns at 12:34:56" vs mutating the counter retroactively).
        self._chain_skill_call_extensions: dict[tuple[str, str], int] = defaultdict(int)
        self._chain_skill_token_extensions: dict[tuple[str, str], int] = defaultdict(int)
        self._call_window: dict[str, deque[float]] = defaultdict(deque)
        self._warned: set[tuple[str, str]] = set()
        # PR25: persistent daily / monthly counters
        self._daily_tokens: int = 0
        self._daily_cost_usd: float = 0.0
        self._monthly_tokens: int = 0
        self._monthly_cost_usd: float = 0.0
        self._day_key: tuple[str, str] | None = None    # ("day", "2026-05-02")
        self._month_key: tuple[str, str] | None = None  # ("month", "2026-05")
        self._ledger: BudgetLedger | None = None
        # R-D8: auto-save state path + throttle. None path = no auto-save.
        self._state_path: Path | None = None
        self._save_throttle_secs: float = 1.0
        self._last_save_monotonic: float = 0.0  # 0 = never saved
        # R-D8: True once load_state was called. The loaded state already
        # includes every committed step's usage, so memo-hit forward-calc
        # would double-count. Caller (runtime) checks this flag.
        self._state_loaded: bool = False

    @property
    def config(self) -> CostConfig:
        return self._config

    # ── PR25: persistent ledger hydration ───────────────────────────────

    def hydrate(self, ledger_path: Path) -> None:
        """Load today's / this month's counters from the persistent ledger.

        Call once at startup after constructing the tracker. No-op if the
        ledger file does not exist yet. Broken JSON lines are silently skipped
        (same pattern as StateLog.iter_from).
        """
        self._ledger = BudgetLedger(ledger_path)
        now = time.time()
        day_key = _period_key(now, "day")
        month_key = _period_key(now, "month")

        daily_tokens = 0
        daily_cost = 0.0
        monthly_tokens = 0
        monthly_cost = 0.0

        for record in self._ledger.iter_records():
            ts_str = record.get("ts")
            if not isinstance(ts_str, str):
                continue
            try:
                ts = _parse_iso_ts(ts_str)
            except (ValueError, OSError):
                continue
            tokens = record.get("tokens", 0)
            cost = record.get("cost_usd", 0.0)
            if not isinstance(tokens, (int, float)):
                tokens = 0
            if not isinstance(cost, (int, float)):
                cost = 0.0
            tokens = int(tokens)
            cost = float(cost)

            rec_day = _period_key(ts, "day")
            rec_month = _period_key(ts, "month")
            if rec_day == day_key:
                daily_tokens += tokens
                daily_cost += cost
            if rec_month == month_key:
                monthly_tokens += tokens
                monthly_cost += cost

        self._daily_tokens = daily_tokens
        self._daily_cost_usd = daily_cost
        self._monthly_tokens = monthly_tokens
        self._monthly_cost_usd = monthly_cost
        self._day_key = day_key
        self._month_key = month_key

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

        # 3. Daily / monthly caps (PR25) — check before call
        day_check = self._check_daily_monthly()
        if not day_check.allowed:
            return day_check

        return rl_check  # may carry warn dims

    def check_pre_spawn(self, *, chain_id: str, skill: str) -> BudgetCheck:
        """Run before spawning a skill from chat. Refuses on per-chain cap.

        FP-0003: the effective hard limit is the configured ``cap.hard_limit``
        plus any per-(chain, skill) extensions granted via the
        ``ask_on_exceed`` user-approval flow (see ``extend_chain_calls``).
        ``BudgetCheck.context['ask_on_exceed']`` tells the caller whether
        a refusal is eligible for the user-approval flow.
        """
        cap = self._config.per_chain_skill_calls
        if not cap.is_active:
            return BudgetCheck(allowed=True)
        used = self._chain_skill_calls[(chain_id, skill)]
        extension = self._chain_skill_call_extensions[(chain_id, skill)]
        effective_hard = int(cap.hard_limit) + extension
        if used >= effective_hard:
            return BudgetCheck(
                allowed=False,
                hard_dimension="per_chain_skill_calls",
                detail=(
                    f"skill {skill!r} already spawned {used} times in chain "
                    f"{chain_id} (effective hard limit {effective_hard}; "
                    f"base {int(cap.hard_limit)} + extensions {extension})"
                ),
                context={
                    "skill": skill, "chain_id": chain_id,
                    "current": used,
                    "hard": effective_hard,
                    "base_hard": int(cap.hard_limit),
                    "extensions_granted": extension,
                    "ask_on_exceed": cap.ask_on_exceed,
                    "extension_calls": int(cap.extension_calls),
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
                "current": used, "hard": effective_hard,
                "base_hard": int(cap.hard_limit),
                "extensions_granted": extension,
            },
        )

    def extend_chain_calls(
        self, *, chain_id: str, skill: str, additional: int
    ) -> int:
        """Extend the effective hard limit for a (chain, skill) pair.

        FP-0003: invoked after the user approves continuation via
        ``ask_user`` on a hard-limit hit. The next ``additional`` spawns
        of ``skill`` in ``chain_id`` will be allowed before the gate
        refuses again. Returns the new total extension count for audit.

        ``additional`` is clamped to >= 0; passing a negative value is
        silently treated as 0 (= the typical caller derives ``additional``
        from ``CostLimitConfig.extension_calls`` so a misconfigured zero
        extension produces a no-op rather than an exception).
        """
        if additional <= 0:
            return self._chain_skill_call_extensions[(chain_id, skill)]
        self._chain_skill_call_extensions[(chain_id, skill)] += additional
        # Reset the warn flag for this dimension so the user gets a
        # fresh warn around the new effective threshold.
        self._warned.discard(
            ("per_chain_skill_calls", f"{chain_id}/{skill}")
        )
        return self._chain_skill_call_extensions[(chain_id, skill)]

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

        # PR25: update daily / monthly counters and append to ledger
        self._update_period_counters(usage.total_tokens, cost_usd)
        if self._ledger is not None:
            self._ledger.append(
                agent=agent,
                model=model,
                tokens=usage.total_tokens,
                cost_usd=cost_usd,
            )

        # Warn on daily / monthly thresholds
        self._check_period_warn(warn_dims)

        # R-D8: persist state for crash recovery (throttled)
        self._maybe_auto_save()

        return BudgetCheck(
            allowed=True,
            warn_dimensions=warn_dims,
            context=self._agent_context(agent) if agent else {},
        )

    def record_spawn(self, *, chain_id: str, skill: str) -> None:
        self._chain_skill_calls[(chain_id, skill)] += 1
        self._maybe_auto_save()

    # ── reset / introspect ──────────────────────────────────────────────

    def reset_all(self) -> dict:
        """Clear per-agent / per-chain / rate-window counters.

        PR25: daily / monthly counters are NOT reset here — they auto-reset
        at period boundary and are backed by the persistent ledger. Returns
        a dict describing what was reset (for `:budget reset` output).
        """
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

    # ── R-D8: state persistence ─────────────────────────────────────────

    def set_state_path(
        self, path: Path, *, throttle_secs: float = 1.0,
    ) -> None:
        """Enable auto-save: every record_llm / record_spawn after this call
        writes the state file (subject to throttle).

        ``throttle_secs`` collapses rapid consecutive writes (LLM call paths
        are hot in multi-skill scenarios — a per-call fsync would dominate).
        Default 1 second. Set to 0 in tests for deterministic save semantics.
        """
        self._state_path = Path(path)
        self._save_throttle_secs = float(throttle_secs)
        # Reset throttle clock so the first record after set_state_path
        # always writes (otherwise the very first save would be skipped if
        # ``set_state_path`` happens close to a prior save).
        self._last_save_monotonic = 0.0

    def _maybe_auto_save(self) -> None:
        """Save state if path is configured and throttle has elapsed.

        Defensive: any I/O error is logged + swallowed (auto-save is a
        best-effort cache write; the in-memory state is the source of
        truth until save lands).
        """
        if self._state_path is None:
            return
        now = time.monotonic()
        if (self._last_save_monotonic > 0
                and now - self._last_save_monotonic < self._save_throttle_secs):
            return
        try:
            self.save_state(self._state_path)
            self._last_save_monotonic = now
        except Exception as e:  # noqa: BLE001 — never fail record on save failure
            import logging
            logging.getLogger(__name__).warning(
                "BudgetTracker auto-save to %s failed: %s",
                self._state_path, e,
            )

    def save_state(self, path: Path) -> None:
        """Persist in-memory counters to ``path`` (atomic write).

        R-D8: closes the gap left by PR25 (which only persists daily /
        monthly via ``budget_ledger.jsonl``). On restart, ``load_state``
        restores ``agent_tokens`` / ``agent_cost_usd`` /
        ``chain_skill_calls`` / ``chain_skill_tokens`` so cap enforcement
        continues across crash.

        Volatile state is NOT persisted:
          - rate-limit window (60-second time-based; entries older than
            the window are invalid anyway)
          - warning state (operational dedup; OK to re-warn after restart)
          - daily / monthly (PR25 owns these via ledger)

        Atomic write: tmp file → fsync → rename. Mid-write crash leaves
        the previous state file intact.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "version": 1,
            "agent_tokens": dict(self._agent_tokens),
            "agent_cost_usd": dict(self._agent_cost_usd),
            "chain_skill_calls": [
                [cid, sk, v]
                for (cid, sk), v in self._chain_skill_calls.items()
            ],
            "chain_skill_tokens": [
                [cid, sk, v]
                for (cid, sk), v in self._chain_skill_tokens.items()
            ],
        }
        import os
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)

    def load_state(self, path: Path) -> None:
        """Restore in-memory counters from ``path``.

        Defensive on failure: missing file → silent no-op (fresh start);
        corrupt JSON → silent no-op + log warning. Operator can use
        ``reyn chat --reset`` if state is unrecoverable.
        """
        path = Path(path)
        # Mark loaded regardless of file presence — the caller's intent
        # is "use the persisted state as truth"; memo-hit forward-calc
        # is suppressed accordingly.
        self._state_loaded = True
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).warning(
                "BudgetTracker.load_state: cannot read %s: %s; starting fresh",
                path, e,
            )
            return
        if not isinstance(data, dict):
            return
        # agent counters
        for k, v in (data.get("agent_tokens") or {}).items():
            self._agent_tokens[str(k)] = int(v)
        for k, v in (data.get("agent_cost_usd") or {}).items():
            self._agent_cost_usd[str(k)] = float(v)
        # chain_skill counters (list of [cid, sk, v] entries)
        for entry in (data.get("chain_skill_calls") or []):
            if isinstance(entry, list) and len(entry) >= 3:
                self._chain_skill_calls[(str(entry[0]), str(entry[1]))] = int(entry[2])
        for entry in (data.get("chain_skill_tokens") or []):
            if isinstance(entry, list) and len(entry) >= 3:
                self._chain_skill_tokens[(str(entry[0]), str(entry[1]))] = int(entry[2])

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
            # PR25: persistent daily / monthly counters
            "daily_tokens": self._daily_tokens,
            "daily_cost_usd": round(self._daily_cost_usd, 6),
            "monthly_tokens": self._monthly_tokens,
            "monthly_cost_usd": round(self._monthly_cost_usd, 6),
            "day_key": self._day_key[1] if self._day_key else None,
            "month_key": self._month_key[1] if self._month_key else None,
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

    # ── PR25: period counter helpers ─────────────────────────────────────

    def _roll_period_if_needed(self) -> None:
        """Reset daily / monthly counters when the local-time period boundary
        has been crossed since the last update.

        Called from both check_pre_llm (to avoid wrongly refusing across the
        midnight boundary when no record_llm has run yet) and record_llm.
        """
        now = time.time()
        new_day = _period_key(now, "day")
        new_month = _period_key(now, "month")

        if self._day_key is None or self._day_key != new_day:
            self._daily_tokens = 0
            self._daily_cost_usd = 0.0
            self._day_key = new_day

        if self._month_key is None or self._month_key != new_month:
            self._monthly_tokens = 0
            self._monthly_cost_usd = 0.0
            self._month_key = new_month

    def _update_period_counters(self, tokens: int, cost_usd: float) -> None:
        """Roll period if needed, then add the new tokens / cost."""
        self._roll_period_if_needed()
        self._daily_tokens += tokens
        self._daily_cost_usd += cost_usd
        self._monthly_tokens += tokens
        self._monthly_cost_usd += cost_usd

    def _check_daily_monthly(self) -> BudgetCheck:
        """Return allowed=False if a daily or monthly hard limit is exceeded."""
        # Roll the period first so a check immediately after midnight does not
        # see yesterday's exhausted counters.
        self._roll_period_if_needed()
        # Daily tokens
        cap = self._config.daily_tokens
        if cap.is_active and self._daily_tokens >= cap.hard_limit:
            label = self._day_key[1] if self._day_key else "today"
            return BudgetCheck(
                allowed=False,
                hard_dimension="daily_tokens",
                detail=f"daily token cap: {self._daily_tokens}/{int(cap.hard_limit)} (day: {label})",
                context=self._period_context(),
            )
        # Daily cost
        cap = self._config.daily_cost_usd
        if cap.is_active and self._daily_cost_usd >= cap.hard_limit:
            label = self._day_key[1] if self._day_key else "today"
            return BudgetCheck(
                allowed=False,
                hard_dimension="daily_cost_usd",
                detail=f"daily cost cap: ${self._daily_cost_usd:.4f}/${cap.hard_limit:.2f} (day: {label})",
                context=self._period_context(),
            )
        # Monthly tokens
        cap = self._config.monthly_tokens
        if cap.is_active and self._monthly_tokens >= cap.hard_limit:
            label = self._month_key[1] if self._month_key else "this month"
            return BudgetCheck(
                allowed=False,
                hard_dimension="monthly_tokens",
                detail=f"monthly token cap: {self._monthly_tokens}/{int(cap.hard_limit)} (month: {label})",
                context=self._period_context(),
            )
        # Monthly cost
        cap = self._config.monthly_cost_usd
        if cap.is_active and self._monthly_cost_usd >= cap.hard_limit:
            label = self._month_key[1] if self._month_key else "this month"
            return BudgetCheck(
                allowed=False,
                hard_dimension="monthly_cost_usd",
                detail=f"monthly cost cap: ${self._monthly_cost_usd:.4f}/${cap.hard_limit:.2f} (month: {label})",
                context=self._period_context(),
            )
        return BudgetCheck(allowed=True)

    def _check_period_warn(self, warn_dims: list[str]) -> None:
        """Append warning dimension names for daily / monthly thresholds."""
        for dim, used, cap_cfg in (
            ("daily_tokens", self._daily_tokens, self._config.daily_tokens),
            ("daily_cost_usd", self._daily_cost_usd, self._config.daily_cost_usd),
            ("monthly_tokens", self._monthly_tokens, self._config.monthly_tokens),
            ("monthly_cost_usd", self._monthly_cost_usd, self._config.monthly_cost_usd),
        ):
            if cap_cfg.is_active and cap_cfg.warn_threshold is not None:
                if used >= cap_cfg.warn_threshold:
                    key = self._day_key[1] if "daily" in dim else (
                        self._month_key[1] if self._month_key else "month"
                    )
                    self._maybe_warn(warn_dims, dim, key or dim)

    def _period_context(self) -> dict:
        return {
            "daily_tokens": self._daily_tokens,
            "daily_cost_usd": round(self._daily_cost_usd, 6),
            "monthly_tokens": self._monthly_tokens,
            "monthly_cost_usd": round(self._monthly_cost_usd, 6),
            "daily_tokens_hard": self._config.daily_tokens.hard_limit,
            "daily_cost_hard": self._config.daily_cost_usd.hard_limit,
            "monthly_tokens_hard": self._config.monthly_tokens.hard_limit,
            "monthly_cost_hard": self._config.monthly_cost_usd.hard_limit,
            "day_label": self._day_key[1] if self._day_key else None,
            "month_label": self._month_key[1] if self._month_key else None,
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
    elif dim in ("daily_tokens", "daily_cost_usd", "monthly_tokens", "monthly_cost_usd"):
        ctx = check.context
        period = "daily" if dim.startswith("daily") else "monthly"
        label = ctx.get("day_label") if period == "daily" else ctx.get("month_label")
        label_str = f" ({label})" if label else ""
        lines.append(f"[budget exceeded] {period} limit reached{label_str}.")
        lines.append("")
        if dim == "daily_tokens":
            lines.append(
                f"  Triggered:  daily_tokens "
                f"({ctx.get('daily_tokens', 0):,}/{int(ctx.get('daily_tokens_hard') or 0):,})"
            )
            if ctx.get("daily_cost_hard") is not None:
                lines.append(
                    f"  Also used:  ${ctx.get('daily_cost_usd', 0):.4f} today"
                    f" (limit: ${ctx.get('daily_cost_hard'):.2f})"
                )
        elif dim == "daily_cost_usd":
            lines.append(
                f"  Triggered:  daily_cost_usd "
                f"(${ctx.get('daily_cost_usd', 0):.4f}/${ctx.get('daily_cost_hard', 0):.2f})"
            )
            if ctx.get("daily_tokens_hard") is not None:
                lines.append(
                    f"  Also used:  {ctx.get('daily_tokens', 0):,} tokens today"
                    f" (limit: {int(ctx.get('daily_tokens_hard')):,})"
                )
        elif dim == "monthly_tokens":
            lines.append(
                f"  Triggered:  monthly_tokens "
                f"({ctx.get('monthly_tokens', 0):,}/{int(ctx.get('monthly_tokens_hard') or 0):,})"
            )
            if ctx.get("monthly_cost_hard") is not None:
                lines.append(
                    f"  Also used:  ${ctx.get('monthly_cost_usd', 0):.4f} this month"
                    f" (limit: ${ctx.get('monthly_cost_hard'):.2f})"
                )
        elif dim == "monthly_cost_usd":
            lines.append(
                f"  Triggered:  monthly_cost_usd "
                f"(${ctx.get('monthly_cost_usd', 0):.4f}/${ctx.get('monthly_cost_hard', 0):.2f})"
            )
            if ctx.get("monthly_tokens_hard") is not None:
                lines.append(
                    f"  Also used:  {ctx.get('monthly_tokens', 0):,} tokens this month"
                    f" (limit: {int(ctx.get('monthly_tokens_hard')):,})"
                )
    else:
        lines.append(f"[budget exceeded] {check.detail}")
    lines.append("")
    lines.append("The next LLM call has been refused.")
    lines.append("")
    lines.append("What you can do:")
    lines.append("  • Raise the limit in `reyn.local.yaml` (cost: section)")
    lines.append("  • Reset counters with `:budget reset`")
    if check.hard_dimension and check.hard_dimension.startswith(("daily_", "monthly_")):
        lines.append("  • Daily / monthly limits reset automatically at period boundary")
    else:
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
    if dimension == "daily_tokens":
        return (
            f"[budget warn] daily token quota approaching: "
            f"{ctx.get('daily_tokens', 0):,} / {int(ctx.get('daily_tokens_hard') or 0):,}"
        )
    if dimension == "daily_cost_usd":
        return (
            f"[budget warn] daily cost quota approaching: "
            f"${ctx.get('daily_cost_usd', 0):.4f} / ${ctx.get('daily_cost_hard', 0):.2f}"
        )
    if dimension == "monthly_tokens":
        return (
            f"[budget warn] monthly token quota approaching: "
            f"{ctx.get('monthly_tokens', 0):,} / {int(ctx.get('monthly_tokens_hard') or 0):,}"
        )
    if dimension == "monthly_cost_usd":
        return (
            f"[budget warn] monthly cost quota approaching: "
            f"${ctx.get('monthly_cost_usd', 0):.4f} / ${ctx.get('monthly_cost_hard', 0):.2f}"
        )
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

    # PR25: Today / Month sections (shown first if any persistent data)
    day_label = snapshot.get("day_key")
    month_label = snapshot.get("month_key")
    daily_tok = snapshot.get("daily_tokens", 0)
    daily_cost = snapshot.get("daily_cost_usd", 0.0)
    monthly_tok = snapshot.get("monthly_tokens", 0)
    monthly_cost = snapshot.get("monthly_cost_usd", 0.0)

    def _pct(used, limit) -> str:
        if limit and limit > 0:
            return f" ({int(used / limit * 100)}%)"
        return ""

    if day_label is not None or any([
        cfg.daily_tokens.is_active,
        cfg.daily_cost_usd.is_active,
        cfg.monthly_tokens.is_active,
        cfg.monthly_cost_usd.is_active,
    ]):
        if day_label:
            tok_cap = cfg.daily_tokens
            cost_cap = cfg.daily_cost_usd
            tok_str = (
                f"{daily_tok:,} / {int(tok_cap.hard_limit):,}{_pct(daily_tok, tok_cap.hard_limit)}"
                if tok_cap.is_active else f"{daily_tok:,}"
            )
            cost_str = (
                f"${daily_cost:.4f} / ${cost_cap.hard_limit:.2f}{_pct(daily_cost, cost_cap.hard_limit)}"
                if cost_cap.is_active else f"${daily_cost:.4f}"
            )
            lines.append(f"  Today ({day_label}):   tokens {tok_str} | {cost_str}")

        if month_label:
            tok_cap = cfg.monthly_tokens
            cost_cap = cfg.monthly_cost_usd
            tok_str = (
                f"{monthly_tok:,} / {int(tok_cap.hard_limit):,}{_pct(monthly_tok, tok_cap.hard_limit)}"
                if tok_cap.is_active else f"{monthly_tok:,}"
            )
            cost_str = (
                f"${monthly_cost:.4f} / ${cost_cap.hard_limit:.2f}{_pct(monthly_cost, cost_cap.hard_limit)}"
                if cost_cap.is_active else f"${monthly_cost:.4f}"
            )
            lines.append(f"  Month ({month_label}): tokens {tok_str} | {cost_str}")

        lines.append("")

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
