"""Tier 2: /skill pure helper functions — _parse_discard_args + _format_elapsed.

``_parse_discard_args`` parses the discard sub-command's tokens into
``(run_id, force)``, accepting ``--force`` anywhere in the token list so
Tab-completion ordering doesn't break the flow.

``_format_elapsed`` formats an elapsed duration from a monotonic start time
(or None when the run pre-dates the session) into a compact human string.
"""
from __future__ import annotations

import time

from reyn.interfaces.slash.skill import _format_elapsed, _parse_discard_args

# ── _parse_discard_args ───────────────────────────────────────────────────────


def test_parse_run_id_only() -> None:
    """Tier 2: bare run_id → (run_id, False)."""
    run_id, force = _parse_discard_args("abc123")
    assert run_id == "abc123"
    assert force is False


def test_parse_force_after_run_id() -> None:
    """Tier 2: run_id then --force → force=True."""
    run_id, force = _parse_discard_args("abc123 --force")
    assert run_id == "abc123"
    assert force is True


def test_parse_force_before_run_id() -> None:
    """Tier 2: --force before run_id is also accepted (Tab-completion order)."""
    run_id, force = _parse_discard_args("--force abc123")
    assert run_id == "abc123"
    assert force is True


def test_parse_empty_args() -> None:
    """Tier 2: empty args → ('', False) — the handler reports a usage error."""
    run_id, force = _parse_discard_args("")
    assert run_id == ""
    assert force is False


def test_parse_force_only_no_run_id() -> None:
    """Tier 2: bare --force with no run_id → run_id=''."""
    run_id, force = _parse_discard_args("--force")
    assert run_id == ""
    assert force is True


def test_parse_extra_positional_tokens_ignored() -> None:
    """Tier 2: extra positional tokens after the first id are ignored."""
    run_id, force = _parse_discard_args("id1 id2 id3")
    assert run_id == "id1"
    assert force is False


# ── _format_elapsed ───────────────────────────────────────────────────────────


def test_format_elapsed_none_is_question_mark() -> None:
    """Tier 2: None start time → '?s' (run pre-dates session / tracking)."""
    assert _format_elapsed(None) == "?s"


def test_format_elapsed_zero_seconds() -> None:
    """Tier 2: just-started run shows 0s."""
    assert _format_elapsed(time.monotonic()) == "0s"


def test_format_elapsed_sub_minute() -> None:
    """Tier 2: under 60 s → '{n}s'."""
    start = time.monotonic() - 45
    out = _format_elapsed(start)
    assert out.endswith("s") and not "m" in out


def test_format_elapsed_minutes() -> None:
    """Tier 2: 90 s → '1m30s' (minutes + zero-padded seconds)."""
    start = time.monotonic() - 90
    out = _format_elapsed(start)
    assert out == "1m30s", f"expected '1m30s', got {out!r}"


def test_format_elapsed_hours() -> None:
    """Tier 2: 3660 s (1h1m) → '1h01m'."""
    start = time.monotonic() - 3660
    out = _format_elapsed(start)
    assert out == "1h01m", f"expected '1h01m', got {out!r}"
