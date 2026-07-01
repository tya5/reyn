"""Tier 2: audit._is_out_of_zone + events._filename_start_date + eval._hash_display.

Three untested pure helpers across three CLI command modules:

- audit._is_out_of_zone(path): returns True when a literal preprocessor
  file-op path is absolute or escapes upward via ".."; False otherwise.
  Non-string / empty / None inputs return False safely.

- events._filename_start_date(name): extracts a date from a JSONL event
  filename prefix (format: YYYY-MM-DD[THHMMSS]…); returns None for
  non-matching names.

- eval._hash_display(h): truncates a version hash to 8 chars; "unknown" /
  empty string → "unknown".
"""
from __future__ import annotations

from datetime import date

from reyn.interfaces.cli.commands.audit import _is_out_of_zone
from reyn.interfaces.cli.commands.eval import _hash_display
from reyn.interfaces.cli.commands.events import _filename_start_date

# ── _is_out_of_zone ───────────────────────────────────────────────────────────


def test_is_out_of_zone_none_returns_false() -> None:
    """Tier 2: None is not a string → returns False."""
    assert _is_out_of_zone(None) is False


def test_is_out_of_zone_empty_string_returns_false() -> None:
    """Tier 2: empty string → returns False."""
    assert _is_out_of_zone("") is False


def test_is_out_of_zone_relative_safe_path_returns_false() -> None:
    """Tier 2: normal relative path with no '..' → within zone, returns False."""
    assert _is_out_of_zone("workspace/data/file.txt") is False


def test_is_out_of_zone_absolute_path_returns_true() -> None:
    """Tier 2: absolute path → out of zone, returns True."""
    assert _is_out_of_zone("/etc/passwd") is True


def test_is_out_of_zone_dotdot_escape_returns_true() -> None:
    """Tier 2: path with '..' component → out of zone, returns True."""
    assert _is_out_of_zone("../escape") is True


def test_is_out_of_zone_embedded_dotdot_returns_true() -> None:
    """Tier 2: path with embedded '..' → out of zone, returns True."""
    assert _is_out_of_zone("subdir/../../../etc/secret") is True


def test_is_out_of_zone_non_string_int_returns_false() -> None:
    """Tier 2: non-string (int) is not a path → returns False."""
    assert _is_out_of_zone(42) is False


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
