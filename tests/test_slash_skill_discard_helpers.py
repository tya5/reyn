"""Tier 2: /skill discard — _parse_discard_args + _format_elapsed pure helper contracts.

`_parse_discard_args` splits discard args into (run_id, force); `_format_elapsed`
formats a monotonic-elapsed duration.  Both are module-level pure functions with
zero test coverage; pinning them prevents silent regressions in the /skill discard
two-step and its elapsed-time display.
"""
from __future__ import annotations

import time

from reyn.interfaces.slash.skill import _format_elapsed, _parse_discard_args

# ── _parse_discard_args ────────────────────────────────────────────────────


def test_parse_bare_run_id() -> None:
    """Tier 2: bare run id with no flags → (run_id, force=False)."""
    assert _parse_discard_args("abc123") == ("abc123", False)


def test_parse_force_flag_before_id() -> None:
    """Tier 2: --force before the id sets force=True."""
    assert _parse_discard_args("--force abc123") == ("abc123", True)


def test_parse_force_flag_after_id() -> None:
    """Tier 2: --force after the id still sets force=True (order-independent)."""
    assert _parse_discard_args("abc123 --force") == ("abc123", True)


def test_parse_empty_args() -> None:
    """Tier 2: empty args → ('', False) — upstream caller shows usage error."""
    assert _parse_discard_args("") == ("", False)


def test_parse_only_force_flag() -> None:
    """Tier 2: only --force with no run_id → ('', True)."""
    assert _parse_discard_args("--force") == ("", True)


def test_parse_extra_positional_tokens_ignored() -> None:
    """Tier 2: extra positional tokens after the first id are silently ignored."""
    run_id, force = _parse_discard_args("abc123 extra noise")
    assert run_id == "abc123"
    assert not force


# ── _format_elapsed (slash/skill.py variant) ───────────────────────────────
#
# Note: this is the /skill discard variant — different from the /tasks variant
# (`tasks._format_elapsed`). Differences: None → "?s", no space between
# minutes and seconds ("1m30s" vs "1m 30s").


def test_format_elapsed_none_returns_unknown() -> None:
    """Tier 2: None started_at → '?s' (resumed run with unknown start)."""
    assert _format_elapsed(None) == "?s"


def test_format_elapsed_seconds_range() -> None:
    """Tier 2: < 60 seconds elapsed → '…s'."""
    now = time.monotonic()
    result = _format_elapsed(now - 5)
    assert result.endswith("s")
    assert "m" not in result


def test_format_elapsed_exactly_0_elapsed() -> None:
    """Tier 2: started right now (0 elapsed) renders as '0s' (not negative)."""
    now = time.monotonic()
    result = _format_elapsed(now)
    assert result in ("0s", "1s")  # timing jitter tolerance


def test_format_elapsed_minutes_format_has_no_space() -> None:
    """Tier 2: 61-3599s renders as '…m…s' (NO space — distinct from tasks variant)."""
    now = time.monotonic()
    result = _format_elapsed(now - 90)
    assert "m" in result
    assert " " not in result
    assert "s" in result


def test_format_elapsed_hours_format() -> None:
    """Tier 2: ≥ 3600s renders as '…h…m' (hours+minutes, no seconds)."""
    now = time.monotonic()
    result = _format_elapsed(now - 3700)
    assert "h" in result
    assert "m" in result
