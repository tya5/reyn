"""Tier 2: events._filename_start_date + eval._hash_display.

Two untested pure helpers across two CLI command modules:

- events._filename_start_date(name): extracts a date from a JSONL event
  filename prefix (format: YYYY-MM-DD[THHMMSS]…); returns None for
  non-matching names.

- eval._hash_display(h): truncates a version hash to 8 chars; "unknown" /
  empty string → "unknown".
"""
from __future__ import annotations

from datetime import date

from reyn.interfaces.cli.commands.eval import _hash_display
from reyn.interfaces.cli.commands.events import _filename_start_date

# ── _filename_start_date ──────────────────────────────────────────────────────


def test_filename_start_date_with_timestamp_suffix() -> None:
    """Tier 2: filename with YYYY-MM-DDTHHmmss prefix → correct date."""
    result = _filename_start_date("2026-05-14T213000.jsonl")
    assert result == date(2026, 5, 14)


def test_filename_start_date_date_only_prefix() -> None:
    """Tier 2: filename with YYYY-MM-DD prefix (no time component) → correct date."""
    result = _filename_start_date("2026-01-31.jsonl")
    assert result == date(2026, 1, 31)


def test_filename_start_date_with_underscore_suffix() -> None:
    """Tier 2: filename with YYYY-MM-DD_suffix form → date extracted from prefix."""
    result = _filename_start_date("2026-05-14_agent_session.jsonl")
    assert result == date(2026, 5, 14)


def test_filename_start_date_non_matching_returns_none() -> None:
    """Tier 2: filename not starting with date pattern → None."""
    result = _filename_start_date("session_notes.txt")
    assert result is None


def test_filename_start_date_empty_string_returns_none() -> None:
    """Tier 2: empty filename → None."""
    result = _filename_start_date("")
    assert result is None


# ── _hash_display ─────────────────────────────────────────────────────────────


def test_hash_display_empty_string_returns_unknown() -> None:
    """Tier 2: empty hash → 'unknown'."""
    assert _hash_display("") == "unknown"


def test_hash_display_unknown_literal_returns_unknown() -> None:
    """Tier 2: literal 'unknown' → 'unknown'."""
    assert _hash_display("unknown") == "unknown"


def test_hash_display_truncates_to_eight_chars() -> None:
    """Tier 2: hash longer than 8 chars → first 8 chars returned."""
    h = "abcdef1234567890"
    result = _hash_display(h)
    assert result == "abcdef12"


def test_hash_display_short_hash_returned_as_is() -> None:
    """Tier 2: hash of exactly 8 chars → returned unchanged."""
    h = "abcdef12"
    assert _hash_display(h) == h
