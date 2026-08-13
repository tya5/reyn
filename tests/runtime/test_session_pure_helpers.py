"""Tier 2: pure helpers in runtime/session.py.

``_no_reply_marker(agent, reason)``   — structured failure string format
``_is_no_reply_marker(text)``         — detect the marker by structural signature
``_parse_no_reply_marker(text)``      — parse into (peer, reason) or None
"""
from __future__ import annotations

import sys

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.runtime.session import (
    _is_no_reply_marker,
    _no_reply_marker,
    _parse_no_reply_marker,
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
# #4552: _ts_iso_to_epoch's own tests lived here. The function was deleted
# as orphaned dead code — its sole caller, _extract_tool_call_records, was
# itself deleted with the hot-list feature (the tool-call-record scan that
# fed the removed ActionUsageTracker; owner directive: discarded).
# ---------------------------------------------------------------------------
