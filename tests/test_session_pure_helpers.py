"""Tier 2: pure helpers in runtime/session.py.

``_no_reply_marker(agent, reason)``   — structured failure string format
``_is_no_reply_marker(text)``         — detect the marker by structural signature
``_parse_no_reply_marker(text)``      — parse into (peer, reason) or None
``_ts_iso_to_epoch(ts)``              — ISO-8601 → epoch float, None on failure
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.runtime.session import (
    _is_no_reply_marker,
    _no_reply_marker,
    _parse_no_reply_marker,
    _ts_iso_to_epoch,
)

# ---------------------------------------------------------------------------
# _no_reply_marker
# ---------------------------------------------------------------------------


def test_no_reply_marker_format() -> None:
    """Tier 2: marker contains agent name and reason in structural form."""
    text = _no_reply_marker("specialist", "router completed without reply")
    assert "specialist" in text
    assert "could not produce a reply" in text
    assert "router completed without reply" in text


def test_no_reply_marker_is_bracketed() -> None:
    """Tier 2: marker starts with '[' (structural signature used by detector)."""
    text = _no_reply_marker("agent_x", "reason")
    assert text.strip().startswith("[")


# ---------------------------------------------------------------------------
# _is_no_reply_marker
# ---------------------------------------------------------------------------


def test_is_no_reply_marker_detects_own_output() -> None:
    """Tier 2: _is_no_reply_marker recognises its own generator's output."""
    marker = _no_reply_marker("specialist", "router completed without reply")
    assert _is_no_reply_marker(marker) is True


def test_is_no_reply_marker_plain_text_rejected() -> None:
    """Tier 2: ordinary reply text → False."""
    assert _is_no_reply_marker("The answer is 42.") is False


def test_is_no_reply_marker_none_rejected() -> None:
    """Tier 2: None → False (no crash)."""
    assert _is_no_reply_marker(None) is False  # type: ignore[arg-type]


def test_is_no_reply_marker_empty_rejected() -> None:
    """Tier 2: empty string → False."""
    assert _is_no_reply_marker("") is False


def test_is_no_reply_marker_partial_match_rejected() -> None:
    """Tier 2: starts with '[' but missing the phrase → False."""
    assert _is_no_reply_marker("[some:other message]") is False


def test_is_no_reply_marker_phrase_without_bracket_rejected() -> None:
    """Tier 2: phrase present but not starting with '[' → False."""
    assert _is_no_reply_marker("agent: could not produce a reply") is False


# ---------------------------------------------------------------------------
# _parse_no_reply_marker
# ---------------------------------------------------------------------------


def test_parse_no_reply_marker_valid_round_trip() -> None:
    """Tier 2: parse extracts the agent name and reason from a valid marker."""
    marker = _no_reply_marker("specialist", "router completed without reply")
    result = _parse_no_reply_marker(marker)
    assert result is not None
    peer, reason = result
    assert peer == "specialist"
    assert "router completed" in reason


def test_parse_no_reply_marker_plain_text_returns_none() -> None:
    """Tier 2: non-marker text → None."""
    assert _parse_no_reply_marker("Just a normal reply.") is None


def test_parse_no_reply_marker_empty_returns_none() -> None:
    """Tier 2: empty string → None."""
    assert _parse_no_reply_marker("") is None


def test_parse_no_reply_marker_none_returns_none() -> None:
    """Tier 2: None input → None (no crash)."""
    assert _parse_no_reply_marker(None) is None  # type: ignore[arg-type]


def test_parse_no_reply_marker_partial_bracket_returns_none() -> None:
    """Tier 2: bracket without the canonical phrase → None."""
    assert _parse_no_reply_marker("[agent: did not reply]") is None


# ---------------------------------------------------------------------------
# _ts_iso_to_epoch
# ---------------------------------------------------------------------------


def test_ts_iso_to_epoch_valid_utc() -> None:
    """Tier 2: valid UTC ISO-8601 string converts to a positive epoch float."""
    epoch = _ts_iso_to_epoch("2024-01-15T12:30:00+00:00")
    assert epoch is not None
    assert epoch > 0


def test_ts_iso_to_epoch_valid_with_z_suffix() -> None:
    """Tier 2: 'Z' UTC suffix (Python 3.11+ fromisoformat) converts correctly."""
    import sys
    if sys.version_info >= (3, 11):
        epoch = _ts_iso_to_epoch("2024-01-15T12:30:00Z")
        assert epoch is not None
        assert epoch > 0


def test_ts_iso_to_epoch_none_returns_none() -> None:
    """Tier 2: None → None."""
    assert _ts_iso_to_epoch(None) is None


def test_ts_iso_to_epoch_empty_string_returns_none() -> None:
    """Tier 2: empty string → None."""
    assert _ts_iso_to_epoch("") is None


def test_ts_iso_to_epoch_invalid_string_returns_none() -> None:
    """Tier 2: unparseable string → None (no crash)."""
    assert _ts_iso_to_epoch("not-a-date") is None


def test_ts_iso_to_epoch_two_distinct_times_order_preserved() -> None:
    """Tier 2: later timestamp produces a larger epoch value."""
    earlier = _ts_iso_to_epoch("2024-01-15T12:00:00+00:00")
    later = _ts_iso_to_epoch("2024-01-15T13:00:00+00:00")
    assert earlier is not None
    assert later is not None
    assert later > earlier
