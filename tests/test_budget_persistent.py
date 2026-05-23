"""Tests for PR25: persistent budget — daily / monthly quota.

Covers:
1. BudgetLedger append + BudgetTracker.hydrate round-trip
2. Period boundary: yesterday's records are excluded from today's counters
3. Daily hard limit triggers refusal with correct dimension
4. Monthly hit with daily warn simultaneously
5. Broken JSON lines in ledger are skipped silently

All assertions go through snapshot() / public API (check_pre_llm / record_llm).
Direct access to private state is forbidden per the testing policy (Tier 4).
Exception: test_check_pre_llm_rolls_period_across_midnight writes private state
to simulate a stale clock — annotated explicitly as Tier 2 (OS invariant).
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from reyn.budget.budget import (
    BudgetTracker,
    CostConfig,
    CostLimitConfig,
    format_refusal_message,
    format_warn_message,
)
from reyn.llm.pricing import TokenUsage

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_cfg(
    daily_tokens=None,
    daily_cost=None,
    monthly_tokens=None,
    monthly_cost=None,
) -> CostConfig:
    return CostConfig(
        daily_tokens=CostLimitConfig(hard_limit=daily_tokens),
        daily_cost_usd=CostLimitConfig(hard_limit=daily_cost),
        monthly_tokens=CostLimitConfig(hard_limit=monthly_tokens),
        monthly_cost_usd=CostLimitConfig(hard_limit=monthly_cost),
    )


def _usage(n: int) -> TokenUsage:
    return TokenUsage(prompt_tokens=n // 2, completion_tokens=n // 2)


def _ledger_line(ts_str: str, tokens: int, cost: float, agent: str = "alice") -> str:
    """Build a raw JSONL line for the ledger."""
    rec = {"ts": ts_str, "agent": agent, "model": "openai/test", "tokens": tokens, "cost_usd": cost}
    return json.dumps(rec)


def _write_ledger(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _ts_from_localtime(lt) -> str:
    """Format a time.struct_time with UTC offset into an ISO 8601 string."""
    offset_sec = lt.tm_gmtoff
    sign = "+" if offset_sec >= 0 else "-"
    offset_abs = abs(offset_sec)
    return (
        f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
        f"T{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
        f"{sign}{offset_abs // 3600:02d}:{(offset_abs % 3600) // 60:02d}"
    )


# ── 1. round-trip: append then hydrate ───────────────────────────────────────


def test_ledger_append_and_hydrate():
    """Tier 1: ledger JSONL round-trip — append then hydrate restores counters."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / ".reyn" / "state" / "budget_ledger.jsonl"
        cfg = _make_cfg(daily_tokens=100_000, monthly_tokens=1_000_000)

        # tracker 1: record two LLM calls
        t1 = BudgetTracker(cfg)
        t1.hydrate(ledger_path)
        t1.record_llm(model="openai/test", agent="alice", usage=_usage(200))
        t1.record_llm(model="openai/test", agent="alice", usage=_usage(300))

        # tracker 2: fresh instance, hydrate from same ledger
        t2 = BudgetTracker(cfg)
        t2.hydrate(ledger_path)

        snap = t2.snapshot()
        assert snap["daily_tokens"] == 500, f"expected 500, got {snap['daily_tokens']}"
        assert snap["monthly_tokens"] == 500
        assert snap["daily_cost_usd"] >= 0.0


def test_hydrate_noop_if_no_ledger():
    """Tier 1: hydrate() is a no-op when the ledger file does not exist."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "nonexistent" / "budget_ledger.jsonl"
        cfg = _make_cfg(daily_tokens=100_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        snap = t.snapshot()
        assert snap["daily_tokens"] == 0
        assert snap["monthly_tokens"] == 0


# ── 2. period boundary ────────────────────────────────────────────────────────


def test_period_boundary_yesterday_excluded():
    """Tier 2: records from yesterday are not counted in today's daily counter."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"

        now = time.time()
        yesterday = now - 86400
        lt_yesterday = time.localtime(yesterday)
        lt_today = time.localtime(now)

        lines = [
            _ledger_line(_ts_from_localtime(lt_yesterday), tokens=1000, cost=0.1),
            _ledger_line(_ts_from_localtime(lt_today), tokens=200, cost=0.02),
        ]
        _write_ledger(ledger_path, lines)

        cfg = _make_cfg(daily_tokens=100_000, monthly_tokens=1_000_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        snap = t.snapshot()
        # Daily should only include today's record (200 tokens, 0.02 USD)
        assert snap["daily_tokens"] == 200, (
            f"daily_tokens should be 200, got {snap['daily_tokens']}"
        )
        assert abs(snap["daily_cost_usd"] - 0.02) < 1e-6
        # Monthly includes both if same month (or just today if different month).
        # The invariant: monthly_tokens >= daily_tokens always.
        assert snap["monthly_tokens"] >= snap["daily_tokens"]


def test_period_boundary_month():
    """Tier 2: records from last month are excluded from this month's counter."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        now = time.time()
        last_month = now - 32 * 86400  # 32 days ago, safely in a different month

        lt_last = time.localtime(last_month)
        lt_now = time.localtime(now)

        lines = [
            _ledger_line(_ts_from_localtime(lt_last), tokens=5000, cost=0.50),
            _ledger_line(_ts_from_localtime(lt_now), tokens=100, cost=0.01),
        ]
        _write_ledger(ledger_path, lines)

        cfg = _make_cfg(monthly_tokens=1_000_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        snap = t.snapshot()
        assert snap["monthly_tokens"] == 100, (
            f"monthly_tokens should be 100, got {snap['monthly_tokens']}"
        )


# ── 3. daily hard limit → refusal ─────────────────────────────────────────────


def test_daily_token_hard_limit_refuses():
    """Tier 1: exceeding daily_tokens hard limit causes check_pre_llm to refuse."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        cfg = _make_cfg(daily_tokens=100)  # very low

        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)
        t.record_llm(model="openai/test", agent="alice", usage=_usage(200))

        snap = t.snapshot()
        assert snap["daily_tokens"] == 200

        check = t.check_pre_llm(model="openai/test", agent="alice")
        assert not check.allowed
        assert check.hard_dimension == "daily_tokens"

        msg = format_refusal_message(check)
        assert "daily" in msg.lower()
        assert "Triggered:" in msg


def test_daily_cost_hard_limit_refuses():
    """Tier 1: exceeding daily_cost_usd hard limit causes refusal."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"

        ts_str = _ts_from_localtime(time.localtime(time.time()))
        _write_ledger(ledger_path, [
            json.dumps({"ts": ts_str, "agent": "alice", "model": "x", "tokens": 10, "cost_usd": 10.0})
        ])

        cfg = _make_cfg(daily_cost=5.0)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        snap = t.snapshot()
        assert snap["daily_cost_usd"] >= 10.0

        check = t.check_pre_llm(model="openai/test", agent="alice")
        assert not check.allowed
        assert check.hard_dimension == "daily_cost_usd"
        msg = format_refusal_message(check)
        assert "daily" in msg.lower()


# ── 4. monthly hit + daily warn simultaneously ───────────────────────────────


def test_monthly_refusal_with_context():
    """Tier 1: monthly_tokens hard limit refuses; message mentions monthly."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        cfg = _make_cfg(monthly_tokens=50, daily_tokens=10_000)

        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)
        t.record_llm(model="openai/test", agent="alice", usage=_usage(100))

        check = t.check_pre_llm(model="openai/test", agent="alice")
        assert not check.allowed
        assert check.hard_dimension == "monthly_tokens"
        msg = format_refusal_message(check)
        assert "monthly" in msg.lower()
        assert "Triggered:" in msg


def test_daily_warn_threshold():
    """Tier 1: crossing daily warn threshold emits a warn dimension in record_llm result."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        cfg = CostConfig(
            daily_tokens=CostLimitConfig(hard_limit=1000, warn_ratio=0.8),
        )
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        # 800 tokens = exactly at warn threshold
        result = t.record_llm(model="openai/test", agent="alice", usage=_usage(800))
        assert "daily_tokens" in result.warn_dimensions

        snap = t.snapshot()
        # format_warn_message for daily_tokens needs the hard-limit key too.
        # We build a minimal context from snapshot() plus the config value we
        # already know from the test setup.
        ctx = {"daily_tokens": snap["daily_tokens"], "daily_tokens_hard": 1000}
        warn_msg = format_warn_message("daily_tokens", ctx)
        assert "daily" in warn_msg.lower()
        assert "800" in warn_msg or "1,000" in warn_msg


def test_check_pre_llm_rolls_period_across_midnight():
    """Tier 2: stale period counters from yesterday must not
    cause a wrongful refusal when check_pre_llm is called after the local-time
    period boundary.

    Implementation note: this test writes to private state directly (_daily_tokens,
    _daily_cost_usd, _day_key) in order to simulate a stale-clock scenario that
    cannot be constructed through the public API alone (record_llm always stamps
    with the current wall-clock time). This is an intentional exception to the
    Tier 4 rule against private state access — the invariant being guarded is
    OS-level (period roll must happen in check_pre_llm, not only in record_llm),
    and there is no other way to create a tracker that appears to be from
    yesterday without manipulating time itself.
    """
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        cfg = _make_cfg(daily_tokens=100, monthly_tokens=100_000)

        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        # Directly inject "yesterday's exhausted state". See docstring for rationale.
        t._daily_tokens = 999
        t._daily_cost_usd = 99.0
        t._day_key = ("day", "1999-01-01")  # forced to be in the past

        check = t.check_pre_llm(model="openai/test", agent="alice")
        assert check.allowed, (
            f"check_pre_llm should roll the day before deciding; "
            f"got hard_dimension={check.hard_dimension}"
        )
        # The period should now reflect today, with zero usage.
        snap = t.snapshot()
        assert snap["daily_tokens"] == 0
        assert snap["day_key"] is not None and snap["day_key"] != "1999-01-01"


def test_monthly_warn_threshold():
    """Tier 1: monthly warn threshold emits the correct warn dimension."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        cfg = CostConfig(
            monthly_tokens=CostLimitConfig(hard_limit=1000, warn_ratio=0.8),
        )
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        result = t.record_llm(model="openai/test", agent="alice", usage=_usage(900))
        assert "monthly_tokens" in result.warn_dimensions


# ── 5. broken JSON lines skipped silently ────────────────────────────────────


def test_broken_ledger_lines_skipped():
    """Tier 1: corrupt / partial lines in the ledger are skipped without error."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"

        ts_str = _ts_from_localtime(time.localtime(time.time()))
        lines = [
            "{broken json",
            "",
            "not a dict at all",
            json.dumps({"ts": ts_str, "agent": "alice", "model": "x", "tokens": 400, "cost_usd": 0.04}),
            '{"ts": "garbage-ts", "tokens": 99, "cost_usd": 0.01, "agent": "a", "model": "x"}',
        ]
        _write_ledger(ledger_path, lines)

        cfg = _make_cfg(daily_tokens=100_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)  # should not raise

        snap = t.snapshot()
        assert snap["daily_tokens"] == 400


# ── 6. snapshot includes daily/monthly fields ─────────────────────────────────


def test_snapshot_includes_period_fields():
    """Tier 1: snapshot() exposes daily_tokens, monthly_tokens, day_key, month_key."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        cfg = _make_cfg(daily_tokens=100_000, monthly_tokens=1_000_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)
        t.record_llm(model="openai/test", agent="alice", usage=_usage(100))

        snap = t.snapshot()
        assert "daily_tokens" in snap
        assert "monthly_tokens" in snap
        assert "day_key" in snap
        assert "month_key" in snap
        assert snap["daily_tokens"] == 100
        assert snap["monthly_tokens"] == 100


# ── 7. reset_all does NOT clear daily / monthly ───────────────────────────────


def test_reset_all_preserves_daily_monthly():
    """Tier 1: reset_all() clears per-agent counters but leaves daily/monthly unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        cfg = _make_cfg(daily_tokens=100_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)
        t.record_llm(model="openai/test", agent="alice", usage=_usage(500))

        before_daily = t.snapshot()["daily_tokens"]
        t.reset_all()

        snap = t.snapshot()
        # Per-agent cleared
        assert snap["agent_tokens"].get("alice", 0) == 0
        # Daily / monthly NOT cleared
        assert snap["daily_tokens"] == before_daily


# ── 8. ledger file is created on first append ─────────────────────────────────


def test_ledger_created_on_first_append():
    """Tier 1: the ledger file and parent dirs are created automatically."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "deep" / "nested" / "budget_ledger.jsonl"
        assert not ledger_path.exists()

        cfg = _make_cfg(daily_tokens=100_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)
        t.record_llm(model="openai/test", agent="alice", usage=_usage(10))

        assert ledger_path.exists()
        content = ledger_path.read_text(encoding="utf-8")
        records = [json.loads(l) for l in content.splitlines() if l.strip()]
        (only,) = records
        assert only["tokens"] == 10


if __name__ == "__main__":
    import sys

    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
