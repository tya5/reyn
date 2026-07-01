"""Tier 2: pure helpers in interfaces/slash/tasks.py.

  ``_format_elapsed(seconds)`` — human-readable elapsed time string
"""
from __future__ import annotations

from reyn.interfaces.slash.tasks import _format_elapsed


def test_format_elapsed_zero_seconds() -> None:
    """Tier 2: 0 seconds renders as '0s'."""
    assert _format_elapsed(0) == "0s"


def test_format_elapsed_seconds_only() -> None:
    """Tier 2: durations under 60s use the Ns format."""
    assert _format_elapsed(30) == "30s"
    assert _format_elapsed(59) == "59s"


def test_format_elapsed_one_minute() -> None:
    """Tier 2: exactly 60s renders as '1m 00s'."""
    assert _format_elapsed(60) == "1m 00s"


def test_format_elapsed_minutes_and_seconds() -> None:
    """Tier 2: 90s renders as '1m 30s' with zero-padded seconds."""
    assert _format_elapsed(90) == "1m 30s"


def test_format_elapsed_hours_and_minutes() -> None:
    """Tier 2: 3600s renders as '1h 00m'."""
    assert _format_elapsed(3600) == "1h 00m"


def test_format_elapsed_hours_with_partial_minutes() -> None:
    """Tier 2: 3661s (1h 1m 1s) renders as '1h 01m' (seconds omitted)."""
    assert _format_elapsed(3661) == "1h 01m"
