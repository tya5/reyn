"""Tests for PR25: persistent budget — daily / monthly quota.

Covers:
1. BudgetLedger append + BudgetTracker.hydrate round-trip
2. Period boundary: yesterday's records are excluded from today's counters
3. Daily hard limit triggers refusal with correct dimension
4. Monthly hit with daily warn simultaneously
5. Broken JSON lines in ledger are skipped silently
"""
from __future__ import annotations
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from reyn.budget import (
    BudgetLedger,
    BudgetTracker,
    CostConfig,
    CostLimitConfig,
    format_refusal_message,
    format_warn_message,
    _period_key,
    _parse_iso_ts,
)
from reyn.pricing import TokenUsage


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


# ── 1. round-trip: append then hydrate ───────────────────────────────────────


def test_ledger_append_and_hydrate():
    """Appending via record_llm then hydrating in a new tracker restores counters."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / ".reyn" / "state" / "budget_ledger.jsonl"
        cfg = _make_cfg(daily_tokens=100_000, monthly_tokens=1_000_000)

        # tracker 1: record two LLM calls
        t1 = BudgetTracker(cfg)
        t1.hydrate(ledger_path)
        t1.record_llm(model="openai/test", agent="alice", usage=_usage(200))
        t1.record_llm(model="openai/test", agent="alice", usage=_usage(300))

        # tracker 2: fresh instance, hydrate from ledger
        t2 = BudgetTracker(cfg)
        t2.hydrate(ledger_path)

        assert t2._daily_tokens == 500, f"expected 500, got {t2._daily_tokens}"
        assert t2._monthly_tokens == 500
        # cost may be 0 since estimate_cost may return 0.0 for unknown test model
        assert t2._daily_cost_usd >= 0.0


def test_hydrate_noop_if_no_ledger():
    """hydrate() is a no-op when the ledger file does not exist."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "nonexistent" / "budget_ledger.jsonl"
        cfg = _make_cfg(daily_tokens=100_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)
        assert t._daily_tokens == 0
        assert t._monthly_tokens == 0


# ── 2. period boundary ────────────────────────────────────────────────────────


def test_period_boundary_yesterday_excluded():
    """Records from yesterday are not counted in today's daily counter."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"

        # Build timestamps: yesterday and today
        now = time.time()
        yesterday = now - 86400  # exactly 24h ago
        lt_yesterday = time.localtime(yesterday)
        lt_today = time.localtime(now)

        def _ts(lt) -> str:
            offset_sec = lt.tm_gmtoff
            sign = "+" if offset_sec >= 0 else "-"
            offset_abs = abs(offset_sec)
            return (
                f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
                f"T{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
                f"{sign}{offset_abs // 3600:02d}:{(offset_abs % 3600) // 60:02d}"
            )

        lines = [
            _ledger_line(_ts(lt_yesterday), tokens=1000, cost=0.1),
            _ledger_line(_ts(lt_today), tokens=200, cost=0.02),
        ]
        _write_ledger(ledger_path, lines)

        cfg = _make_cfg(daily_tokens=100_000, monthly_tokens=1_000_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        # Daily should only include today's record (200 tokens, 0.02 USD)
        assert t._daily_tokens == 200, f"daily_tokens should be 200, got {t._daily_tokens}"
        assert abs(t._daily_cost_usd - 0.02) < 1e-6

        # Monthly includes both if same month, or just today if different month.
        # We check: monthly_tokens >= daily_tokens always.
        assert t._monthly_tokens >= t._daily_tokens


def test_period_boundary_month():
    """Records from last month are excluded from this month's counter."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        now = time.time()
        last_month = now - 32 * 86400  # 32 days ago, safely in a different month

        lt_last = time.localtime(last_month)
        lt_now = time.localtime(now)

        def _ts(lt) -> str:
            offset_sec = lt.tm_gmtoff
            sign = "+" if offset_sec >= 0 else "-"
            offset_abs = abs(offset_sec)
            return (
                f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
                f"T{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
                f"{sign}{offset_abs // 3600:02d}:{(offset_abs % 3600) // 60:02d}"
            )

        lines = [
            _ledger_line(_ts(lt_last), tokens=5000, cost=0.50),
            _ledger_line(_ts(lt_now), tokens=100, cost=0.01),
        ]
        _write_ledger(ledger_path, lines)

        cfg = _make_cfg(monthly_tokens=1_000_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        # Monthly should NOT include last month's 5000 tokens.
        assert t._monthly_tokens == 100, f"monthly_tokens should be 100, got {t._monthly_tokens}"


# ── 3. daily hard limit → refusal ─────────────────────────────────────────────


def test_daily_token_hard_limit_refuses():
    """Exceeding daily_tokens hard limit causes check_pre_llm to refuse."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        cfg = _make_cfg(daily_tokens=100)  # very low

        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        # Record enough to exceed limit
        t.record_llm(model="openai/test", agent="alice", usage=_usage(200))
        assert t._daily_tokens == 200

        # Next pre-check should refuse
        check = t.check_pre_llm(model="openai/test", agent="alice")
        assert not check.allowed
        assert check.hard_dimension == "daily_tokens"

        # format_refusal_message should mention daily
        msg = format_refusal_message(check)
        assert "daily" in msg.lower()
        assert "Triggered:" in msg


def test_daily_cost_hard_limit_refuses():
    """Exceeding daily_cost_usd hard limit causes refusal."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"

        # Write a ledger record with a large cost
        now = time.time()
        lt = time.localtime(now)
        offset_sec = lt.tm_gmtoff
        sign = "+" if offset_sec >= 0 else "-"
        offset_abs = abs(offset_sec)
        ts_str = (
            f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
            f"T{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
            f"{sign}{offset_abs // 3600:02d}:{(offset_abs % 3600) // 60:02d}"
        )
        _write_ledger(ledger_path, [
            json.dumps({"ts": ts_str, "agent": "alice", "model": "x", "tokens": 10, "cost_usd": 10.0})
        ])

        cfg = _make_cfg(daily_cost=5.0)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)

        assert t._daily_cost_usd >= 10.0

        check = t.check_pre_llm(model="openai/test", agent="alice")
        assert not check.allowed
        assert check.hard_dimension == "daily_cost_usd"
        msg = format_refusal_message(check)
        assert "daily" in msg.lower()


# ── 4. monthly hit + daily warn simultaneously ───────────────────────────────


def test_monthly_refusal_with_context():
    """Monthly_tokens hard limit also refuses; format_refusal_message mentions monthly."""
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
    """Crossing daily warn threshold emits a warn dimension in record_llm result."""
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

        warn_msg = format_warn_message("daily_tokens", t._period_context())
        assert "daily" in warn_msg.lower()
        assert "800" in warn_msg or "1,000" in warn_msg


def test_monthly_warn_threshold():
    """Monthly warn threshold emits the correct warn dimension."""
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
    """Corrupt / partial lines in the ledger are skipped without error."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"

        now = time.time()
        lt = time.localtime(now)
        offset_sec = lt.tm_gmtoff
        sign = "+" if offset_sec >= 0 else "-"
        offset_abs = abs(offset_sec)
        ts_str = (
            f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
            f"T{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
            f"{sign}{offset_abs // 3600:02d}:{(offset_abs % 3600) // 60:02d}"
        )

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

        # Only the valid line with a parseable timestamp should count
        assert t._daily_tokens == 400


# ── 6. _parse_iso_ts round-trip ──────────────────────────────────────────────


def test_parse_iso_ts_round_trip():
    """_parse_iso_ts returns a float close to the time it was generated from."""
    now = time.time()
    lt = time.localtime(now)
    offset_sec = lt.tm_gmtoff
    sign = "+" if offset_sec >= 0 else "-"
    offset_abs = abs(offset_sec)
    ts_str = (
        f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
        f"T{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}"
        f"{sign}{offset_abs // 3600:02d}:{(offset_abs % 3600) // 60:02d}"
    )
    parsed = _parse_iso_ts(ts_str)
    # Allow ±1 second (truncation from seconds)
    assert abs(parsed - now) < 2, f"parsed={parsed}, now={now}"


def test_parse_iso_ts_invalid():
    with pytest.raises(ValueError):
        _parse_iso_ts("not-a-timestamp")


# ── 7. snapshot includes daily/monthly fields ─────────────────────────────────


def test_snapshot_includes_period_fields():
    """snapshot() includes daily_tokens, monthly_tokens, day_key, month_key."""
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


# ── 8. reset_all does NOT clear daily / monthly ───────────────────────────────


def test_reset_all_preserves_daily_monthly():
    """reset_all() clears per-agent counters but leaves daily/monthly unchanged."""
    with tempfile.TemporaryDirectory() as tmp:
        ledger_path = Path(tmp) / "budget_ledger.jsonl"
        cfg = _make_cfg(daily_tokens=100_000)
        t = BudgetTracker(cfg)
        t.hydrate(ledger_path)
        t.record_llm(model="openai/test", agent="alice", usage=_usage(500))

        before_daily = t._daily_tokens
        t.reset_all()

        # Per-agent cleared
        assert t._agent_tokens.get("alice", 0) == 0
        # Daily / monthly NOT cleared
        assert t._daily_tokens == before_daily


# ── 9. ledger file is created on first append ─────────────────────────────────


def test_ledger_created_on_first_append():
    """The ledger file (and parent dirs) are created automatically."""
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
        assert len(records) == 1
        assert records[0]["tokens"] == 10


if __name__ == "__main__":
    import sys
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v"]))
