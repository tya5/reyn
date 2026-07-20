"""Budget / cost / rate-limit enforcement (PR22 + PR25).

A process-shared `BudgetTracker` accumulates token + USD usage per agent
and per-model call rates. Hooked into LLM calls (pre-check refuses on hard
cap, post-record updates counters).

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
are not tied to any specific domain.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from reyn.llm.pricing import (
    CostBreakdown,
    EmbeddingCost,
    TokenUsage,
    estimate_cost,
    estimate_cost_breakdown,
    estimate_embedding_cost,
)

# ── exceptions ──────────────────────────────────────────────────────────────


class BudgetExceeded(Exception):
    """Raised when a pre-call budget check refuses the LLM call."""

    def __init__(self, dimension: str, detail: str) -> None:
        super().__init__(detail)
        self.dimension = dimension
        self.detail = detail


# ── config ──────────────────────────────────────────────────────────────────


@dataclass
class CostLimitConfig:
    """A single hybrid-cap dimension. None hard_limit = unlimited.

    FP-0005 (#1877): ``extension_calls`` is the per-grant extension amount
    for the unified ``safety.on_limit`` 3-mode policy; ``> 0`` opts the
    dimension into the on_limit flow (``interactive`` = ask the user,
    ``auto_extend`` = bounded auto-grant, ``unattended`` = deny). ``0``
    (default) keeps the hard-refuse behaviour regardless of mode (nothing to
    grant).
    """

    hard_limit: float | None = None
    warn_ratio: float = 0.8
    # FP-0005 (#1877): per-grant extension amount. ``> 0`` makes the
    # dimension participate in the ``safety.on_limit`` flow.
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
    """`cost:` — financial budget caps and rate limits (PR22 + PR25).

    Contains only financial knobs (per-agent token/USD, daily/monthly quota,
    rate limits). Loop-detection caps (router_invocations_per_turn) moved
    to ``SafetyConfig.loop`` in the FP-0004/0005 refactor.
    """

    per_agent_tokens: CostLimitConfig = field(default_factory=CostLimitConfig)
    per_agent_cost_usd: CostLimitConfig = field(default_factory=CostLimitConfig)
    rate_limit_per_minute: dict[str, int] = field(default_factory=dict)
    rate_limit_warn_ratio: float = 0.8
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
    """Append-only JSONL ledger for durable budget records (PR25).

    Each line is an LLM-call record::

        {"ts": "2026-05-02T10:23:00+09:00", "agent": "alice",
         "model": "...", "tokens": 300, "cost_usd": 0.0023}

    Records are fsync'd on append so a process crash cannot roll back a
    completed LLM call and under-count quota usage. This is the cap-critical
    durability layer; the throttled ``budget_state.json`` is a best-effort
    cache on top of it (see ``BudgetTracker.hydrate``).

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
        purpose: str | None = None,
    ) -> None:
        """Append one LLM-call record and fsync.

        ``purpose`` (#1190) is the cost-attribution bucket
        (main/compaction/judge/dogfood). It is omitted
        from the record when None so pre-existing ledger lines stay
        byte-identical.
        """
        record: dict = {
            "ts": self._now_iso(),
            "agent": agent,
            "model": model,
            "tokens": tokens,
            "cost_usd": cost_usd,
        }
        if purpose is not None:
            record["purpose"] = purpose
        self._write_record(record)

    @staticmethod
    def _now_iso() -> str:
        """Current local time as an ISO-8601 string with UTC offset."""
        lt = time.localtime(time.time())
        offset_sec = lt.tm_gmtoff  # seconds east of UTC
        sign = "+" if offset_sec >= 0 else "-"
        offset_abs = abs(offset_sec)
        offset_str = f"{sign}{offset_abs // 3600:02d}:{(offset_abs % 3600) // 60:02d}"
        return (
            f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
            f"T{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
            f"{offset_str}"
        )

    def _write_record(self, record: dict) -> None:
        """Serialize *record* as one JSONL line, append, flush, fsync."""
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

    def iter_records_from(self, byte_offset: int):
        """Yield parsed record dicts starting at *byte_offset* (the tail since
        the last checkpoint anchor). Same broken-line tolerance as
        ``iter_records``. Used by ``BudgetTracker.hydrate`` to bound the
        re-parse cost to activity since the last checkpoint instead of the
        whole (lifetime, monotonically-growing) ledger — see #2945."""
        if not self._path.is_file():
            return
        with self._path.open("rb") as f:
            f.seek(byte_offset)
            for raw_line in f:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                yield entry

    def tail_boundary(self, chunk_size: int = 8192) -> tuple[int, bytes] | None:
        """Return ``(file_size, last_line_bytes)`` for the ledger's current end,
        or ``None`` if the ledger is empty/missing.

        ``last_line_bytes`` is the raw bytes of the final record line
        (including its trailing newline) ending exactly at ``file_size``. Used
        as the checkpoint anchor's content pin (#2945 P1) — cheap (bounded by
        line length, not ledger size) since every append is fsync'd with a
        trailing newline, so the file reliably ends on a line boundary.
        """
        if not self._path.is_file():
            return None
        size = self._path.stat().st_size
        if size == 0:
            return None
        read_size = min(chunk_size, size)
        with self._path.open("rb") as f:
            f.seek(size - read_size)
            buf = f.read(read_size)
        # The buffer should end with the append's trailing "\n". Find the
        # newline immediately before it to isolate the last complete line.
        search_end = len(buf) - 1 if buf.endswith(b"\n") else len(buf)
        idx = buf.rfind(b"\n", 0, search_end)
        if idx == -1:
            if read_size < size:
                # Pathological: a single line longer than chunk_size. Grow the
                # window rather than mis-frame the anchor.
                return self.tail_boundary(chunk_size=chunk_size * 4)
            line_bytes = buf
        else:
            line_bytes = buf[idx + 1:]
        return (size, line_bytes)


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


# ── compacted checkpoint (#2945) ─────────────────────────────────────────────
#
# ``hydrate`` re-parsing the whole (monotonically-growing, never-rotated)
# ledger on every startup is a blocking-startup-path bug, not a "make it
# faster" optimization: the per-agent lifetime aggregate is the only
# unbounded dimension (daily/monthly self-heal at their period boundary), so
# ``BudgetCheckpoint`` compacts *only* per-agent totals + the current
# daily/monthly-so-far totals into a small file, anchored to an exact ledger
# byte position. ``hydrate`` then only re-parses the ledger TAIL after that
# anchor.
#
# Core invariant (do not weaken): the checkpoint must always be safe to
# delete. It carries no fact the ledger does not already durably hold — it is
# a *rebuildable* cache of a prefix-sum over the ledger, never the sole
# holder of truth.
#
# P3 (ambiguity -> over-count-safe, never under-count) resolves differently
# depending on WHY the checkpoint is untrustworthy — see ``hydrate``'s
# docstring for the full 3-way breakdown (missing/corrupt checkpoint vs.
# ledger "truncated" vs. ledger "invalid"/replaced). The one that matters
# most: a checkpoint that is itself still internally consistent
# (``content_sha256`` verifies) but whose ledger has been TRUNCATED below its
# anchor (or deleted outright) is NOT discarded — its per-agent totals are
# merged in as a floor. Silently falling back to "just re-scan whatever
# ledger remains" for that case would re-introduce the exact staleness this
# whole mechanism exists to prevent: a lost/truncated ledger would silently
# reset a cap-critical per-agent counter. A ledger that is instead REPLACED
# (same size or larger, content mismatch) is the one case that gets NO
# floor — see ``verify_anchor``.


@dataclass
class BudgetCheckpoint:
    """A compacted, point-in-time summary of ``budget_ledger.jsonl``.

    Lives at ``.reyn/cache/budget_checkpoint.json`` (DERIVED/cache — see
    ``docs/reference/runtime/reyn-dir-layout.md``): fully reconstructable from
    the ledger, so it is not write-gated recovery-core and may be deleted at
    any time with no data loss (the next ``hydrate`` falls back to a full
    ledger scan and rewrites it).
    """

    agent_tokens: dict[str, int]
    agent_cost_usd: dict[str, float]
    day_key: str | None
    daily_tokens: int
    daily_cost_usd: float
    month_key: str | None
    monthly_tokens: int
    monthly_cost_usd: float
    anchor_byte_offset: int
    anchor_line_len: int
    anchor_line_sha256: str

    def _content_payload(self) -> dict:
        """The counted-values subset covered by ``content_sha256`` — everything
        EXCEPT the anchor (which pins the checkpoint to the ledger, not to
        itself) and the hash field itself. Sorted keys for a stable digest."""
        return {
            "agent_tokens": dict(sorted(self.agent_tokens.items())),
            "agent_cost_usd": dict(sorted(self.agent_cost_usd.items())),
            "day_key": self.day_key,
            "daily_tokens": self.daily_tokens,
            "daily_cost_usd": self.daily_cost_usd,
            "month_key": self.month_key,
            "monthly_tokens": self.monthly_tokens,
            "monthly_cost_usd": self.monthly_cost_usd,
        }

    def content_sha256(self) -> str:
        canonical = json.dumps(self._content_payload(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        payload = self._content_payload()
        payload["version"] = 1
        # #2945 P3 hardening: the ledger-side anchor only proves the
        # checkpoint is pinned to an unmodified/untruncated ledger position —
        # it says nothing about whether the checkpoint's OWN counted values
        # were hand-edited/corrupted afterward. content_sha256 covers that
        # independently (a direct edit to agent_tokens etc. changes the
        # digest without touching the ledger at all).
        payload["content_sha256"] = self.content_sha256()
        payload["anchor"] = {
            "byte_offset": self.anchor_byte_offset,
            "line_len": self.anchor_line_len,
            "line_sha256": self.anchor_line_sha256,
        }
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "BudgetCheckpoint | None":
        """Parse *data*; return ``None`` on ANY shape mismatch OR a
        ``content_sha256`` mismatch (P3: ambiguous/tampered checkpoint content
        must fall back to a full ledger re-scan, never guess)."""
        try:
            if not isinstance(data, dict) or data.get("version") != 1:
                return None
            anchor = data["anchor"]
            agent_tokens = {str(k): int(v) for k, v in dict(data["agent_tokens"]).items()}
            agent_cost_usd = {
                str(k): float(v) for k, v in dict(data["agent_cost_usd"]).items()
            }
            expected_content_hash = str(data["content_sha256"])
            checkpoint = cls(
                agent_tokens=agent_tokens,
                agent_cost_usd=agent_cost_usd,
                day_key=data.get("day_key"),
                daily_tokens=int(data["daily_tokens"]),
                daily_cost_usd=float(data["daily_cost_usd"]),
                month_key=data.get("month_key"),
                monthly_tokens=int(data["monthly_tokens"]),
                monthly_cost_usd=float(data["monthly_cost_usd"]),
                anchor_byte_offset=int(anchor["byte_offset"]),
                anchor_line_len=int(anchor["line_len"]),
                anchor_line_sha256=str(anchor["line_sha256"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
        if checkpoint.content_sha256() != expected_content_hash:
            return None
        return checkpoint


def _default_checkpoint_path(ledger_path: Path) -> Path:
    """``.reyn/state/budget_ledger.jsonl`` → ``.reyn/cache/budget_checkpoint.json``.

    Derived rather than threaded through every caller: the checkpoint is an
    implementation detail of ``hydrate``'s bounded re-scan, always a fixed
    sibling of the ledger under the project's ``.reyn/`` tree (see
    ``docs/reference/runtime/reyn-dir-layout.md``).
    """
    reyn_dir = ledger_path.parent.parent
    return reyn_dir / "cache" / "budget_checkpoint.json"


def load_checkpoint_or_none(checkpoint_path: Path) -> BudgetCheckpoint | None:
    """Read + parse the checkpoint; ``None`` on any missing/corrupt/partial
    shape (P3: caller must fall back to a full ledger re-scan)."""
    if not checkpoint_path.is_file():
        return None
    try:
        raw = checkpoint_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return BudgetCheckpoint.from_dict(data)


def verify_anchor(checkpoint: BudgetCheckpoint, ledger_path: Path) -> tuple[str, int]:
    """Verify the checkpoint's anchor against the CURRENT ledger file.

    Returns ``(status, current_size)`` where ``status`` is one of:

    - ``"valid"`` — the ledger still contains, byte-for-byte, the line the
      checkpoint was anchored to. Fast tail-only path.
    - ``"truncated"`` — the CURRENT ledger is shorter than the anchor's byte
      offset (including "ledger file missing entirely", size 0). This is a
      genuine data-loss signal: the ledger no longer contains everything the
      checkpoint saw. Per P3 (ambiguity -> over-count-safe), the caller MUST
      treat the checkpoint's own (content-hash-verified) totals as a FLOOR
      merged into the re-scan result — discarding them here would silently
      UNDER-count exactly the durable spend this checkpoint recorded (the
      failure mode this whole mechanism exists to prevent).
    - ``"invalid"`` — the ledger is the SAME SIZE OR LARGER but its content at
      the anchor position no longer matches (or the anchor itself is
      malformed). Ledger is append-only, so byte-identical growth can never
      produce this — it means the file was replaced/rewritten (rotation,
      tamper), not merely truncated. The checkpoint's totals are NOT used as
      a floor here: a replaced ledger may describe entirely different agents/
      history, and blending its stale numbers in would leak a stale total
      into an unrelated new history (see the ledger-replacement test) — the
      full re-scan of the CURRENT ledger is the only trustworthy answer.
    """
    if not ledger_path.is_file():
        return ("truncated", 0)
    size = ledger_path.stat().st_size
    offset = checkpoint.anchor_byte_offset
    line_len = checkpoint.anchor_line_len
    if line_len <= 0 or offset < line_len:
        return ("invalid", size)  # malformed anchor — not a truncation signal
    if size < offset:
        return ("truncated", size)
    try:
        with ledger_path.open("rb") as f:
            f.seek(offset - line_len)
            buf = f.read(line_len)
    except OSError:
        return ("invalid", size)
    if len(buf) != line_len:
        return ("invalid", size)
    if hashlib.sha256(buf).hexdigest() != checkpoint.anchor_line_sha256:
        return ("invalid", size)
    return ("valid", size)


def write_checkpoint(
    checkpoint_path: Path,
    ledger_path: Path,
    *,
    agent_tokens: dict[str, int],
    agent_cost_usd: dict[str, float],
    day_key: tuple[str, str] | None,
    daily_tokens: int,
    daily_cost_usd: float,
    month_key: tuple[str, str] | None,
    monthly_tokens: int,
    monthly_cost_usd: float,
) -> None:
    """Write a fresh checkpoint anchored to the ledger's CURRENT end.

    No-op if the ledger is empty/missing (nothing to anchor to yet).

    P2 write order (durability): the ledger itself is already fsync'd per
    append by ``BudgetLedger._write_record`` — by construction this always
    runs *after* that, never before — then: write a temp file → fsync temp →
    atomic rename → fsync the containing directory (so the rename survives a
    crash immediately after).
    """
    ledger = BudgetLedger(ledger_path)
    boundary = ledger.tail_boundary()
    if boundary is None:
        return
    size, line_bytes = boundary
    checkpoint = BudgetCheckpoint(
        agent_tokens=dict(agent_tokens),
        agent_cost_usd=dict(agent_cost_usd),
        day_key=day_key[1] if day_key else None,
        daily_tokens=daily_tokens,
        daily_cost_usd=daily_cost_usd,
        month_key=month_key[1] if month_key else None,
        monthly_tokens=monthly_tokens,
        monthly_cost_usd=monthly_cost_usd,
        anchor_byte_offset=size,
        anchor_line_len=len(line_bytes),
        anchor_line_sha256=hashlib.sha256(line_bytes).hexdigest(),
    )
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
    payload = json.dumps(checkpoint.to_dict(), ensure_ascii=False, indent=2)
    with tmp.open("w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(checkpoint_path)
    try:
        dir_fd = os.open(str(checkpoint_path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Directory fsync is a best-effort durability step for the rename's
        # metadata; the atomic rename above already leaves either the old or
        # new file intact on a crash, so a failure here is not data loss.
        pass


# ── tracker ─────────────────────────────────────────────────────────────────


class BudgetTracker:
    """Process-wide accumulator + hybrid-cap enforcer.

    Counters live in memory and reset on process restart. `/budget reset`
    clears them mid-process. The tracker is single-thread / asyncio-safe
    by virtue of running in a single event loop (no internal locking).
    """

    def __init__(self, config: CostConfig) -> None:
        self._config = config
        self._agent_tokens: dict[str, int] = defaultdict(int)
        self._agent_cost_usd: dict[str, float] = defaultdict(float)
        # Cost-panel breakdown (#cost-panel-breakdown): per-agent CostBreakdown
        # (prompt/cache-read/cache-creation/completion + savings), accumulated
        # per call in ``record_llm`` alongside ``_agent_cost_usd``. NOT ledger-
        # persisted — in-memory only, resets on restart (unlike
        # ``_agent_cost_usd`` above, which the ledger hydrates). This is a
        # deliberate scope choice, not an oversight: persisting it durably
        # would mean extending ``BudgetLedger``'s on-disk schema with the 4
        # cache-breakdown fields and would trigger the CLAUDE.md recovery-
        # feature PR gate (truncate-falsify test). The authoritative, durable,
        # restart-surviving TOTAL is ``_agent_cost_usd`` (unchanged); this
        # breakdown is a same-process-only refinement the cost panel reads to
        # show Input/Output/Saved on top of that already-durable Total.
        self._agent_cost_breakdown: dict[str, CostBreakdown] = defaultdict(CostBreakdown)
        # FP-0063 PC: embedding spend is tracked as its OWN independent
        # aggregate (owner: "embedding は独立追跡の想定"), NOT folded into
        # ``_agent_cost_breakdown`` above — see ``EmbeddingCost``'s docstring
        # for why (embedding is input-only/uncacheable; mapping it onto
        # ``CostBreakdown.prompt_cost`` would dilute cache_hit_rate /
        # cache_savings). Same non-durability posture as
        # ``_agent_cost_breakdown``: in-memory only, resets on restart — a
        # deliberate scope choice (persisting it would need a BudgetLedger
        # schema extension + the CLAUDE.md recovery-feature truncate-falsify
        # gate), not an oversight.
        self._agent_embedding_cost: dict[str, EmbeddingCost] = defaultdict(EmbeddingCost)
        # #1190 stage (iii): per-purpose cost attribution
        # (main/compaction/judge/dogfood) for the /budget breakdown payoff.
        self._purpose_tokens: dict[str, int] = defaultdict(int)
        self._purpose_cost_usd: dict[str, float] = defaultdict(float)
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
        # #2945: compacted checkpoint path, derived in hydrate() from the
        # ledger path. None until hydrate() runs (mirrors self._ledger).
        self._checkpoint_path: Path | None = None
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

    def hydrate(self, ledger_path: Path, *, checkpoint_path: Path | None = None) -> None:
        """Reconstruct durable counters from the persistent ledger.

        Call once at startup after constructing the tracker. No-op if the
        ledger file does not exist yet. Broken JSON lines are silently skipped
        (same pattern as StateLog.iter_from).

        Reconstructed from the fsync-per-append ledger (the cap-critical
        source of truth):
          - daily / monthly token + cost (period-filtered to today / this month)
          - per-agent tokens + cost (#1911 — all-time cumulative, summed per
            ``agent`` field; mirrors what ``load_state`` restores)

        The throttled ``budget_state.json`` (``load_state``) is a best-effort
        cache only; a crash inside the 1s throttle window can leave it stale.
        Because every counted increment is fsync'd to the ledger *before* the
        throttled save runs, the ledger is always at least as complete — so
        ledger hydration is the authoritative restore for cap enforcement.

        #2945: re-parsing the WHOLE (monotonically-growing, never-rotated)
        ledger on every startup is a blocking-startup-path bug. A compacted
        ``BudgetCheckpoint`` (see module docstring section above) carries the
        per-agent totals as of an exact ledger byte position (the anchor); if
        that anchor still verifies against the current ledger (``verify_anchor``
        returns ``"valid"``), only the TAIL after it is re-parsed here —
        bounding the cost to activity since the last checkpoint refresh
        instead of the ledger's lifetime.

        Three fallback classes, each resolved per P3 (ambiguity ->
        over-count-safe, never under-count):
          - missing/corrupt/tampered checkpoint (parse failure, or its own
            ``content_sha256`` no longer matches) -> full re-scan of the
            ledger, no floor (nothing trustworthy survives to floor with).
          - ``"truncated"`` (current ledger shorter than the anchor, INCLUDING
            deleted entirely) -> full re-scan of the surviving ledger, then
            the checkpoint's per-agent totals are merged in as a per-agent
            FLOOR (``max()``, never lower). This is the critical case: a
            crash/rotation that shortens or removes the ledger must never
            silently reset a cap-critical per-agent counter just because its
            source records are no longer present — the checkpoint is the
            surviving witness. NOTE: this means archiving/deleting ONLY the
            ledger file no longer resets per-agent totals while a checkpoint
            still exists (see ``docs/reference/config/budget.md``).
          - ``"invalid"`` (current ledger is the SAME SIZE OR LARGER but its
            content at the anchor position no longer matches — a genuine
            truncation can never produce this since the ledger is
            append-only, so this means the file was replaced/rewritten) ->
            full re-scan only, NO floor. The checkpoint's stale totals must
            not leak into what may be an entirely different, unrelated
            ledger history.

        A fresh checkpoint is written at the end of every hydrate call
        regardless of which path was taken, so the checkpoint self-heals and
        the *next* hydrate is bounded even after a fallback. That write is
        best-effort: the checkpoint is DERIVED/cache
        (``docs/reference/runtime/reyn-dir-layout.md``), so a write failure
        (read-only cache dir, disk full) is logged and swallowed rather than
        propagated — it must never block startup.
        """
        self._ledger = BudgetLedger(ledger_path)
        self._checkpoint_path = checkpoint_path or _default_checkpoint_path(ledger_path)
        now = time.time()
        day_key = _period_key(now, "day")
        month_key = _period_key(now, "month")

        checkpoint = load_checkpoint_or_none(self._checkpoint_path)
        status: str | None = None
        agent_tokens: dict[str, int] = defaultdict(int)
        agent_cost: dict[str, float] = defaultdict(float)
        daily_tokens = 0
        daily_cost = 0.0
        monthly_tokens = 0
        monthly_cost = 0.0

        if checkpoint is not None:
            status, _ = verify_anchor(checkpoint, ledger_path)

        if checkpoint is not None and status == "valid":
            # Fast path: seed from the verified checkpoint, only re-parse the
            # tail written since its anchor.
            for agent, tok in checkpoint.agent_tokens.items():
                agent_tokens[agent] += tok
            for agent, cost in checkpoint.agent_cost_usd.items():
                agent_cost[agent] += cost
            # Period baselines only carry over if still the SAME period —
            # otherwise the checkpoint's stale period total must not leak
            # into the new period (self-healing at the boundary, same
            # semantics _roll_period_if_needed already relies on elsewhere).
            if checkpoint.day_key == day_key[1]:
                daily_tokens = checkpoint.daily_tokens
                daily_cost = checkpoint.daily_cost_usd
            if checkpoint.month_key == month_key[1]:
                monthly_tokens = checkpoint.monthly_tokens
                monthly_cost = checkpoint.monthly_cost_usd
            records = self._ledger.iter_records_from(checkpoint.anchor_byte_offset)
        else:
            # No verifiable fast path — full re-scan of the CURRENT ledger.
            # If status == "truncated", the checkpoint's own totals are
            # merged in as a floor AFTER this scan (below) — see P3 note on
            # ``verify_anchor``. If status == "invalid" or checkpoint is
            # None, no floor is applied (see ``verify_anchor`` docstring for
            # why an "invalid"/replaced ledger must NOT be floored).
            records = self._ledger.iter_records()

        for record in records:
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

            # #1911: per-agent counters are all-time cumulative (not
            # period-filtered) — same semantics as save_state/load_state.
            agent = record.get("agent")
            if isinstance(agent, str):
                agent_tokens[agent] += tokens
                agent_cost[agent] += cost

            rec_day = _period_key(ts, "day")
            rec_month = _period_key(ts, "month")
            if rec_day == day_key:
                daily_tokens += tokens
                daily_cost += cost
            if rec_month == month_key:
                monthly_tokens += tokens
                monthly_cost += cost

        # #2945 P3: a TRUNCATED ledger (or one deleted entirely) must never
        # cause the per-agent lifetime aggregate to drop below what a
        # content-hash-verified checkpoint already durably recorded — that
        # would silently reset a cap-critical counter (exactly the failure
        # this mechanism exists to prevent). Merge the checkpoint's totals in
        # as a per-agent FLOOR (max, never overwrite-down) on top of whatever
        # the re-scan of the current (shortened) ledger found. Deliberately
        # NOT applied when status == "invalid" (ledger replaced/rewritten,
        # not merely truncated) — see ``verify_anchor``'s docstring.
        if checkpoint is not None and status == "truncated":
            for agent, tok in checkpoint.agent_tokens.items():
                if tok > agent_tokens.get(agent, 0):
                    agent_tokens[agent] = tok
            for agent, cost in checkpoint.agent_cost_usd.items():
                if cost > agent_cost.get(agent, 0.0):
                    agent_cost[agent] = cost

        self._daily_tokens = daily_tokens
        self._daily_cost_usd = daily_cost
        self._monthly_tokens = monthly_tokens
        self._monthly_cost_usd = monthly_cost
        self._day_key = day_key
        self._month_key = month_key
        # #1911: durable per-agent restore. defaultdict so later record_llm
        # keeps its increment semantics.
        self._agent_tokens = agent_tokens
        self._agent_cost_usd = agent_cost

        # #2945: refresh the checkpoint to the ledger's current end so the
        # NEXT hydrate (even with zero interim record_llm calls) is bounded
        # too — the checkpoint is always safe to (re)write from confirmed
        # in-memory totals, and safe to lose (next hydrate falls back).
        #
        # The checkpoint is DERIVED/cache (docs/reference/runtime/
        # reyn-dir-layout.md): a write failure here (read-only cache dir,
        # disk full, permissions) must never block startup — swallow and
        # log, same posture as ``_maybe_auto_save``'s save_state failure
        # handling below. The in-memory counters (already correct from the
        # scan above) are unaffected; only the NEXT hydrate loses the fast
        # path and falls back to a full re-scan again.
        try:
            write_checkpoint(
                self._checkpoint_path,
                ledger_path,
                agent_tokens=dict(self._agent_tokens),
                agent_cost_usd=dict(self._agent_cost_usd),
                day_key=self._day_key,
                daily_tokens=self._daily_tokens,
                daily_cost_usd=self._daily_cost_usd,
                month_key=self._month_key,
                monthly_tokens=self._monthly_tokens,
                monthly_cost_usd=self._monthly_cost_usd,
            )
        except OSError as e:
            import logging
            logging.getLogger(__name__).warning(
                "BudgetTracker.hydrate: failed to write checkpoint to %s: %s "
                "(startup continues; next hydrate falls back to a full re-scan)",
                self._checkpoint_path, e,
            )

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

    # ── recording ───────────────────────────────────────────────────────

    def record_llm(
        self,
        *,
        model: str,
        agent: str | None,
        usage: TokenUsage,
        purpose: str | None = None,
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

        # #1190 stage (iii): per-purpose attribution for the /budget breakdown.
        if purpose is not None:
            self._purpose_tokens[purpose] += usage.total_tokens
            self._purpose_cost_usd[purpose] += cost_usd

        if agent is not None:
            new_tokens = self._agent_tokens[agent] + usage.total_tokens
            self._agent_tokens[agent] = new_tokens
            new_cost = self._agent_cost_usd[agent] + cost_usd
            self._agent_cost_usd[agent] = new_cost

            # Cost-panel breakdown accumulation (session/agent/project scope
            # rows). ``estimate_cost_breakdown`` returns None for an unpriced/
            # unknown model (mirrors ``estimate_cost``'s None-sentinel) — skip
            # accumulation rather than treat unknown as free.
            breakdown = estimate_cost_breakdown(model, usage)
            if breakdown is not None:
                self._agent_cost_breakdown[agent] += breakdown

            cap = self._config.per_agent_tokens
            if cap.is_active and cap.warn_threshold is not None:
                if new_tokens >= cap.warn_threshold:
                    self._maybe_warn(warn_dims, "per_agent_tokens", agent)

            cap = self._config.per_agent_cost_usd
            if cap.is_active and cap.warn_threshold is not None:
                if new_cost >= cap.warn_threshold:
                    self._maybe_warn(warn_dims, "per_agent_cost_usd", agent)

        # PR25: update daily / monthly counters and append to ledger
        self._update_period_counters(usage.total_tokens, cost_usd)
        if self._ledger is not None:
            self._ledger.append(
                agent=agent,
                model=model,
                tokens=usage.total_tokens,
                cost_usd=cost_usd,
                purpose=purpose,
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

    # ── FP-0063 PC: embedding cost (independent of record_llm above) ─────

    def record_embedding(
        self,
        *,
        model: str,
        agent: str | None,
        tokens: int,
    ) -> None:
        """Record one embedding call's spend into the INDEPENDENT per-agent
        ``EmbeddingCost`` aggregate (FP-0063 X2b) — never touches
        ``_agent_cost_usd`` / ``_agent_cost_breakdown`` (the chat aggregates)
        and is not itself gated by the LLM per-agent hard-cap checks above
        (embedding is not a chat call; ``check_pre_llm`` is unaffected).

        Mixed-model correctness (X6): prices THIS call at model's own rate via
        ``estimate_embedding_cost`` before folding into the aggregate — never
        pools tokens across models and prices them afterwards at one rate.

        An unpriced/unknown model (``estimate_embedding_cost`` -> ``(None,
        None)``) still counts toward ``tokens``/``calls`` but contributes 0 to
        ``cost_usd``, with ``unpriced_calls`` incremented so the gap stays
        visible rather than silently reading as a real $0.00 call.
        """
        if agent is None:
            return
        cost_usd, _ = estimate_embedding_cost(model, tokens)
        self._agent_embedding_cost[agent] += EmbeddingCost(
            cost_usd=cost_usd or 0.0,
            tokens=tokens,
            calls=1,
            unpriced_calls=0 if cost_usd is not None else 1,
        )

    def agent_embedding_cost(self, agent: str) -> EmbeddingCost:
        """Independent embedding-spend aggregate for ``agent`` (all sessions,
        this process only — same non-durability posture as
        ``agent_cost_breakdown``). Returns an empty (all-zero) ``EmbeddingCost``
        for an agent with no recorded embedding calls this process."""
        return self._agent_embedding_cost.get(agent, EmbeddingCost())

    # ── reset / introspect ──────────────────────────────────────────────

    def reset_all(self) -> dict:
        """Clear per-agent / rate-window counters.

        PR25: daily / monthly counters are NOT reset here — they auto-reset
        at period boundary and are backed by the persistent ledger. Returns
        a dict describing what was reset (for `/budget reset` output).
        """
        before = {
            "agent_tokens": dict(self._agent_tokens),
            "agent_cost_usd": dict(self._agent_cost_usd),
            "rate_window_sizes": {m: len(q) for m, q in self._call_window.items()},
        }
        self._agent_tokens.clear()
        self._agent_cost_usd.clear()
        self._agent_cost_breakdown.clear()
        self._agent_embedding_cost.clear()
        self._call_window.clear()
        self._warned.clear()
        return before

    # ── R-D8: state persistence ─────────────────────────────────────────

    def set_state_path(
        self, path: Path, *, throttle_secs: float = 1.0,
    ) -> None:
        """Enable auto-save: every record_llm after this call
        writes the state file (subject to throttle).

        ``throttle_secs`` collapses rapid consecutive writes (LLM call paths
        are hot in multi-agent scenarios — a per-call fsync would dominate).
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
            # #2945: refresh the compacted checkpoint on the same throttle
            # cadence so a long-running session's next restart still only
            # re-parses a small tail, not everything since the last full
            # hydrate. Best-effort — a checkpoint miss here just means the
            # next hydrate falls back to a full re-scan (P3), never
            # under-counts.
            if self._ledger is not None and self._checkpoint_path is not None:
                write_checkpoint(
                    self._checkpoint_path,
                    self._ledger.path,
                    agent_tokens=dict(self._agent_tokens),
                    agent_cost_usd=dict(self._agent_cost_usd),
                    day_key=self._day_key,
                    daily_tokens=self._daily_tokens,
                    daily_cost_usd=self._daily_cost_usd,
                    month_key=self._month_key,
                    monthly_tokens=self._monthly_tokens,
                    monthly_cost_usd=self._monthly_cost_usd,
                )
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
        restores ``agent_tokens`` / ``agent_cost_usd`` so per-agent cap
        enforcement continues across crash.

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

        #1911: live startup runs ``hydrate`` (durable ledger) *before*
        ``load_state``. For the ledger-backed per-agent tokens/cost counters
        the value already restored from the ledger is the source of truth and
        is always at least as complete as this throttled best-effort state
        file (the ledger is fsync'd before each throttled save). So those
        counters are merged with ``max`` rather than overwritten — a stale
        state file can never under-count a cap below the durable ledger
        value.
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
        # Persisted state has no version-validation (reads "version" but does not
        # gate on it), so a version-skewed / hand-edited file may carry a null /
        # non-numeric counter. Coerce-with-default rather than crash load_state.
        def _coerce_int(v: object) -> int:
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0

        def _coerce_float(v: object) -> float:
            try:
                return float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0.0

        # agent counters — never drop below the durable ledger value (#1911);
        # coerce first so a null/garbage persisted value → 0 → max() keeps ledger.
        for k, v in (data.get("agent_tokens") or {}).items():
            key = str(k)
            self._agent_tokens[key] = max(self._agent_tokens.get(key, 0), _coerce_int(v))
        for k, v in (data.get("agent_cost_usd") or {}).items():
            key = str(k)
            self._agent_cost_usd[key] = max(self._agent_cost_usd.get(key, 0.0), _coerce_float(v))

    def snapshot(self) -> dict:
        """Return a structured view used by `/cost` / `/budget` formatters."""
        return {
            "agent_tokens": dict(self._agent_tokens),
            "agent_cost_usd": dict(self._agent_cost_usd),
            # #1190 stage (iii): per-purpose cost attribution.
            "purpose_tokens": dict(self._purpose_tokens),
            "purpose_cost_usd": dict(self._purpose_cost_usd),
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

    def agent_cost_usd(self, agent: str) -> float:
        """All-time cumulative USD cost for ``agent`` — the durable per-agent total (ledger-hydrated,
        restart-surviving). The single source of truth read by ``/cost`` and, via
        ``registry.agent_cost_usd`` (#cost-restart), the inline status bar. One counter per agent
        (summed across all its sessions in ``record_llm``), so it never N×-counts multiple sessions."""
        return self._agent_cost_usd.get(agent, 0.0)

    def agent_tokens(self, agent: str) -> int:
        """All-time cumulative TOTAL tokens for ``agent`` (durable, ledger-hydrated). Total only —
        the prompt/completion breakdown is not persisted per ledger record (only ``total_tokens``)."""
        return self._agent_tokens.get(agent, 0)

    def agent_cost_breakdown(self, agent: str) -> CostBreakdown:
        """Cache-aware ``CostBreakdown`` accumulated for ``agent`` (cost-panel Input/Output/Saved
        rows). Same-process only (see ``__init__``'s note) — NOT ledger-hydrated, unlike
        ``agent_cost_usd``/``agent_tokens`` above; resets on restart. Returns an empty (all-zero)
        ``CostBreakdown`` for an agent with no recorded calls this process."""
        return self._agent_cost_breakdown.get(agent, CostBreakdown())

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
    lines.append("  • Reset counters with `/budget reset`")
    if check.hard_dimension and check.hard_dimension.startswith(("daily_", "monthly_")):
        lines.append("  • Daily / monthly limits reset automatically at period boundary")
    else:
        lines.append("  • Restart `reyn chat` (limits are per-process)")
    lines.append("  • See current usage with `/budget`")
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
    """`/cost` 1-line output for the attached agent."""
    tokens = snapshot["agent_tokens"].get(agent, 0)
    cost = snapshot["agent_cost_usd"].get(agent, 0.0)
    return f"{agent}: {tokens:,} tokens, ${cost:.4f}  (this session)"


def format_budget_full(snapshot: dict, attached: str | None) -> str:
    """`/budget` full breakdown across all dimensions."""
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
        # Pre-compute the label column width so `tokens` lines up across
        # Today / Month rows. `Today (YYYY-MM-DD):` is 4 chars wider than
        # `Month (YYYY-MM):`; the old hand-rolled "   " spacing assumed an
        # exact label length and broke when day_label / month_label format
        # changed.
        day_label_part = f"Today ({day_label}):" if day_label else ""
        month_label_part = f"Month ({month_label}):" if month_label else ""
        label_col = max(len(day_label_part), len(month_label_part)) + 1

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
            lines.append(
                f"  {day_label_part:<{label_col}}tokens {tok_str} | {cost_str}"
            )

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
            lines.append(
                f"  {month_label_part:<{label_col}}tokens {tok_str} | {cost_str}"
            )

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

    # #1190 stage (iii): per-purpose cost attribution — where the spend went
    # (main / compaction / judge / dogfood).
    purpose_tokens = snapshot.get("purpose_tokens") or {}
    purpose_cost = snapshot.get("purpose_cost_usd") or {}
    if purpose_tokens or purpose_cost:
        lines.append("  By purpose:")
        for p in sorted(set(purpose_tokens) | set(purpose_cost)):
            tok = purpose_tokens.get(p, 0)
            cost = purpose_cost.get(p, 0.0)
            lines.append(f"    {p:<18}{tok:>10,} tok | ${cost:.4f}")
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

    lines.append("  Reset counters with `/budget reset`.")
    return "\n".join(lines)
