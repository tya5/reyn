"""Tier 2: support_bundle._rec_time + _line_included; topology._format_members.

support_bundle._rec_time: extracts a timezone-aware datetime from a JSON record
by scanning a fixed priority list of timestamp field names.  Returns None when
no recognised field is present.

support_bundle._line_included: best-effort filter — includes a record unless it
clearly fails a `since` (time) or `session` filter.  Favours completeness: a
record with no parseable timestamp is INCLUDED even when a `since` filter is set.

topology._format_members: pure string formatter for a Topology's member list —
team uses ``*`` on the leader, pipeline uses `` → ``, network uses ``, ``.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from reyn.interfaces.cli.commands.support_bundle import _line_included, _rec_time
from reyn.interfaces.cli.commands.topology import _format_members
from reyn.runtime.topology import Topology

# ── helpers ───────────────────────────────────────────────────────────────────

def _dt(iso: str) -> datetime:
    """Parse an ISO timestamp; attach UTC if naive."""
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ── _rec_time ─────────────────────────────────────────────────────────────────


def test_rec_time_timestamp_field() -> None:
    """Tier 2: 'timestamp' key with ISO value → correct datetime."""
    rec = {"timestamp": "2026-05-14T21:30:00+00:00"}
    result = _rec_time(rec)
    assert result == _dt("2026-05-14T21:30:00+00:00")


def test_rec_time_ts_field() -> None:
    """Tier 2: 'ts' key is recognised (second in priority list)."""
    rec = {"ts": "2026-06-01T12:00:00+00:00"}
    result = _rec_time(rec)
    assert result == _dt("2026-06-01T12:00:00+00:00")


def test_rec_time_sent_at_iso_field() -> None:
    """Tier 2: 'sent_at_iso' key is recognised."""
    rec = {"sent_at_iso": "2026-03-10T08:00:00+00:00"}
    result = _rec_time(rec)
    assert result == _dt("2026-03-10T08:00:00+00:00")


def test_rec_time_no_known_fields_returns_none() -> None:
    """Tier 2: record with no recognised timestamp field → None."""
    result = _rec_time({"event_type": "turn_started", "data": {}})
    assert result is None


def test_rec_time_non_string_timestamp_skipped() -> None:
    """Tier 2: integer value in 'timestamp' field is skipped → None (no next field)."""
    result = _rec_time({"timestamp": 1234567890})
    assert result is None


def test_rec_time_naive_datetime_gets_utc() -> None:
    """Tier 2: timezone-naive ISO string → UTC-aware datetime attached."""
    rec = {"timestamp": "2026-05-14T21:30:00"}
    result = _rec_time(rec)
    assert result is not None
    assert result.tzinfo is not None


def test_rec_time_malformed_value_skipped_tries_next() -> None:
    """Tier 2: malformed 'timestamp' → skipped; 'ts' is tried next."""
    rec = {"timestamp": "not-a-date", "ts": "2026-05-01T10:00:00+00:00"}
    result = _rec_time(rec)
    assert result == _dt("2026-05-01T10:00:00+00:00")


# ── _line_included ────────────────────────────────────────────────────────────


def test_line_included_no_filters() -> None:
    """Tier 2: no since / session filters → always included."""
    assert _line_included({"timestamp": "2026-01-01T00:00:00Z"}, None, None) is True


def test_line_included_since_filter_old_record_excluded() -> None:
    """Tier 2: since set and record is before since → excluded."""
    since = _dt("2026-06-01T00:00:00+00:00")
    rec = {"timestamp": "2026-05-01T00:00:00+00:00"}
    assert _line_included(rec, since, None) is False


def test_line_included_since_filter_newer_record_included() -> None:
    """Tier 2: since set and record is at/after since → included."""
    since = _dt("2026-05-01T00:00:00+00:00")
    rec = {"timestamp": "2026-06-01T00:00:00+00:00"}
    assert _line_included(rec, since, None) is True


def test_line_included_since_filter_no_timestamp_included() -> None:
    """Tier 2: since set but record has no parseable timestamp → included (completeness bias)."""
    since = _dt("2026-06-01T00:00:00+00:00")
    assert _line_included({"event_type": "turn"}, since, None) is True


def test_line_included_session_filter_match_included() -> None:
    """Tier 2: session filter set and session_id matches → included."""
    assert _line_included({"session_id": "sess-abc"}, None, "sess-abc") is True


def test_line_included_session_filter_no_match_excluded() -> None:
    """Tier 2: session filter set and no session field matches → excluded."""
    assert _line_included({"session_id": "sess-xyz"}, None, "sess-abc") is False


def test_line_included_session_filter_no_session_fields_included() -> None:
    """Tier 2: session filter set but record has none of the recognised session fields → included."""
    assert _line_included({"unrelated_key": "value"}, None, "sess-abc") is True


# ── _format_members ───────────────────────────────────────────────────────────


def test_format_members_team_marks_leader() -> None:
    """Tier 2: team topology → leader name has '*' suffix; others don't."""
    topo = Topology(
        name="squad", kind="team",
        members=("alice", "bob", "carol"), leader="alice",
    )
    result = _format_members(topo)
    assert "alice*" in result
    assert "bob" in result
    assert "carol" in result


def test_format_members_team_non_leader_has_no_star() -> None:
    """Tier 2: team members who are not leader have no '*'."""
    topo = Topology(
        name="squad", kind="team",
        members=("alice", "bob"), leader="bob",
    )
    result = _format_members(topo)
    assert "alice*" not in result
    assert "bob*" in result


def test_format_members_pipeline_uses_arrow() -> None:
    """Tier 2: pipeline topology → members joined with ' → '."""
    topo = Topology(
        name="pipe", kind="pipeline",
        members=("stage1", "stage2", "stage3"),
    )
    result = _format_members(topo)
    assert " → " in result
    assert "stage1" in result
    assert "stage3" in result


def test_format_members_network_uses_comma() -> None:
    """Tier 2: network topology → members joined with ', '."""
    topo = Topology(
        name="net", kind="network",
        members=("a", "b", "c"),
    )
    result = _format_members(topo)
    assert " → " not in result
    parts = [p.strip() for p in result.split(",")]
    assert "a" in parts
    assert "b" in parts
