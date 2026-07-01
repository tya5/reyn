"""Tier 2: runtime/budget/budget.py pure helper contracts.

_period_key(ts, kind) maps a POSIX timestamp to a local-calendar period key
tuple used for daily/monthly quota bucketing.

_parse_iso_ts(ts_str) parses an ISO-8601 timestamp with explicit UTC offset
to a POSIX float; the format written by BudgetLedger.append.
"""
from __future__ import annotations

import time

import pytest

from reyn.runtime.budget.budget import _parse_iso_ts, _period_key

# ── _period_key ───────────────────────────────────────────────────────────────


def test_period_key_day_first_element() -> None:
    """Tier 2: _period_key(ts, 'day') returns a tuple whose first element is 'day'."""
    key = _period_key(1705321800.0, "day")
    assert key[0] == "day"


def test_period_key_month_first_element() -> None:
    """Tier 2: _period_key(ts, 'month') returns a tuple whose first element is 'month'."""
    key = _period_key(1705321800.0, "month")
    assert key[0] == "month"



def test_period_key_month_is_year_month_prefix_of_day() -> None:
    """Tier 2: month key YYYY-MM is a prefix of day key YYYY-MM-DD for the same timestamp."""
    ts = 1705321800.0
    _, day_str = _period_key(ts, "day")
    _, month_str = _period_key(ts, "month")
    assert day_str.startswith(month_str)


def test_period_key_matches_localtime() -> None:
    """Tier 2: day and month key strings match the time.localtime() components for the ts."""
    ts = 1705321800.0
    lt = time.localtime(ts)
    _, day_str = _period_key(ts, "day")
    _, month_str = _period_key(ts, "month")
    expected_day = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
    expected_month = f"{lt.tm_year:04d}-{lt.tm_mon:02d}"
    assert day_str == expected_day
    assert month_str == expected_month


def test_period_key_deterministic() -> None:
    """Tier 2: repeated calls with the same timestamp return identical keys."""
    ts = 1705321800.0
    assert _period_key(ts, "day") == _period_key(ts, "day")
    assert _period_key(ts, "month") == _period_key(ts, "month")


def test_period_key_unknown_kind_raises() -> None:
    """Tier 2: unsupported kind raises ValueError with 'unknown period kind' in message."""
    with pytest.raises(ValueError, match="unknown period kind"):
        _period_key(1705321800.0, "week")


# ── _parse_iso_ts ─────────────────────────────────────────────────────────────


def test_parse_iso_ts_utc_exact_epoch() -> None:
    """Tier 2: UTC offset '+00:00' → exact POSIX epoch 1705321800.0."""
    assert _parse_iso_ts("2024-01-15T12:30:00+00:00") == 1705321800.0


def test_parse_iso_ts_positive_offset_same_instant() -> None:
    """Tier 2: +09:00 offset (21:30 JST) maps to same UTC instant as 12:30 UTC."""
    assert _parse_iso_ts("2024-01-15T21:30:00+09:00") == 1705321800.0


def test_parse_iso_ts_negative_offset_same_instant() -> None:
    """Tier 2: -09:00 offset (03:30 local) maps to same UTC instant as 12:30 UTC."""
    assert _parse_iso_ts("2024-01-15T03:30:00-09:00") == 1705321800.0


def test_parse_iso_ts_later_ts_greater_epoch() -> None:
    """Tier 2: a later timestamp yields a strictly greater POSIX epoch."""
    earlier = _parse_iso_ts("2024-01-15T12:30:00+00:00")
    later = _parse_iso_ts("2024-01-15T12:31:00+00:00")
    assert later > earlier


def test_parse_iso_ts_invalid_string_raises() -> None:
    """Tier 2: non-ISO input raises ValueError."""
    with pytest.raises(ValueError):
        _parse_iso_ts("not-a-date")


def test_parse_iso_ts_bare_date_raises() -> None:
    """Tier 2: date-only string without time/offset raises ValueError."""
    with pytest.raises(ValueError):
        _parse_iso_ts("2024-01-15")
