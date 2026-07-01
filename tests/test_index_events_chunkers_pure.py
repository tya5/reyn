"""Tier 2: stdlib/skills/index_events/chunkers.py pure-helper contracts.

Field-extraction, phase-chain, error-collection, caller-extraction,
ISO timestamp parsing, token approximation, and path helper.

All helpers are pure functions (no I/O, no side-effects).
"""
from __future__ import annotations

import pytest

from reyn.stdlib.skills.index_events.chunkers import (
    _approx_tokens,
    _dirname,
    _extract_caller,
    _extract_errors,
    _extract_phase_chain,
    _extract_run_id,
    _get_field,
    _parse_iso_safe,
)

# ── _extract_run_id ───────────────────────────────────────────────────────────


def test_extract_run_id_from_data_run_id() -> None:
    """Tier 2: run_id present in event.data → returned as string."""
    event = {"data": {"run_id": "abc-123"}}
    assert _extract_run_id(event) == "abc-123"


def test_extract_run_id_fallback_uses_skill_and_ts() -> None:
    """Tier 2: missing run_id falls back to '<skill>::<ts>' format."""
    event = {"data": {"skill": "indexer"}, "timestamp": "2024-01-15T00:00:00Z"}
    result = _extract_run_id(event)
    assert result == "indexer::2024-01-15T00:00:00Z"


def test_extract_run_id_fallback_unknown_skill() -> None:
    """Tier 2: missing skill falls back to 'unknown' in the composite key."""
    event = {"timestamp": "2024-01-15T00:00:00Z"}
    result = _extract_run_id(event)
    assert result.startswith("unknown::")


def test_extract_run_id_converts_non_string_to_str() -> None:
    """Tier 2: integer run_id is returned as string."""
    event = {"data": {"run_id": 42}}
    assert _extract_run_id(event) == "42"


# ── _get_field ────────────────────────────────────────────────────────────────


def test_get_field_from_data_layer() -> None:
    """Tier 2: field in event.data takes priority over top-level."""
    event = {"data": {"skill": "indexer"}, "skill": "other"}
    assert _get_field(event, "skill") == "indexer"


def test_get_field_fallback_to_top_level() -> None:
    """Tier 2: missing from data falls through to top-level field."""
    event = {"kind": "phase_started"}
    assert _get_field(event, "kind") == "phase_started"


def test_get_field_none_event_returns_none() -> None:
    """Tier 2: None input returns None without error."""
    assert _get_field(None, "skill") is None


def test_get_field_missing_key_returns_none() -> None:
    """Tier 2: key absent at both layers returns None."""
    assert _get_field({"data": {}}, "skill") is None


# ── _extract_phase_chain ──────────────────────────────────────────────────────


def test_extract_phase_chain_collects_started_nodes_in_order() -> None:
    """Tier 2: phase names from 'started' events returned in encounter order."""
    events = [
        {"type": "phase_started", "data": {"node": "alpha"}},
        {"type": "phase_started", "data": {"node": "beta"}},
        {"type": "phase_completed", "data": {"node": "alpha"}},
    ]
    assert _extract_phase_chain(events) == ["alpha", "beta"]


def test_extract_phase_chain_deduplicates_repeat_starts() -> None:
    """Tier 2: a phase that restarts is not listed twice."""
    events = [
        {"type": "phase_started", "data": {"node": "alpha"}},
        {"type": "phase_started", "data": {"node": "alpha"}},
    ]
    assert _extract_phase_chain(events) == ["alpha"]


def test_extract_phase_chain_ignores_non_started_events() -> None:
    """Tier 2: events without 'started' in type contribute no phase name."""
    events = [
        {"type": "phase_completed", "data": {"node": "alpha"}},
        {"type": "turn_ended", "data": {}},
    ]
    assert _extract_phase_chain(events) == []


def test_extract_phase_chain_empty_list() -> None:
    """Tier 2: empty input returns empty list."""
    assert _extract_phase_chain([]) == []


# ── _extract_errors ───────────────────────────────────────────────────────────


def test_extract_errors_from_error_events() -> None:
    """Tier 2: error message extracted from event.data.error."""
    events = [{"data": {"error": "timeout"}}]
    assert _extract_errors(events, None) == ["timeout"]


def test_extract_errors_includes_failed_event_message() -> None:
    """Tier 2: failed_event contributes its message after error_events."""
    errors = _extract_errors([], {"data": {"message": "skill failed"}})
    assert errors == ["skill failed"]


def test_extract_errors_deduplicates_identical_messages() -> None:
    """Tier 2: same error message from two sources appears once."""
    events = [{"data": {"error": "oom"}}]
    failed = {"data": {"error": "oom"}}
    result = _extract_errors(events, failed)
    assert result == ["oom"]


def test_extract_errors_empty_inputs() -> None:
    """Tier 2: no events and no failed_event → empty list."""
    assert _extract_errors([], None) == []


# ── _extract_caller ───────────────────────────────────────────────────────────


def test_extract_caller_from_started_event_data() -> None:
    """Tier 2: caller field in started event's data is preferred."""
    started = {"data": {"caller": "orchestrator"}}
    assert _extract_caller("any__run", started) == "orchestrator"


def test_extract_caller_from_run_id_prefix() -> None:
    """Tier 2: 'caller__runid' pattern parsed when no started event."""
    assert _extract_caller("myagent__20240115", None) == "myagent"


def test_extract_caller_fallback_direct() -> None:
    """Tier 2: run_id with no '__' and no started event → 'direct'."""
    assert _extract_caller("plain-run-id", None) == "direct"


# ── _parse_iso_safe ───────────────────────────────────────────────────────────


def test_parse_iso_safe_utc_timestamp() -> None:
    """Tier 2: valid UTC timestamp returns aware datetime."""
    from datetime import timezone

    dt = _parse_iso_safe("2024-01-15T12:00:00Z")
    assert dt is not None
    assert dt.tzinfo == timezone.utc
    assert dt.year == 2024 and dt.month == 1 and dt.day == 15


def test_parse_iso_safe_date_only() -> None:
    """Tier 2: date-only string '2024-01-15' is accepted."""
    dt = _parse_iso_safe("2024-01-15")
    assert dt is not None
    assert dt.year == 2024


def test_parse_iso_safe_invalid_returns_none() -> None:
    """Tier 2: unparseable string returns None without raising."""
    assert _parse_iso_safe("not-a-date") is None


def test_parse_iso_safe_empty_returns_none() -> None:
    """Tier 2: empty string returns None."""
    assert _parse_iso_safe("") is None


# ── _approx_tokens ────────────────────────────────────────────────────────────


def test_approx_tokens_returns_positive_for_nonempty() -> None:
    """Tier 2: any non-empty text yields a positive token count."""
    assert _approx_tokens("hello") >= 1


def test_approx_tokens_scales_with_length() -> None:
    """Tier 2: longer text yields a strictly larger count than shorter text."""
    short_count = _approx_tokens("hi")
    long_count = _approx_tokens("x" * 400)
    assert long_count > short_count


def test_approx_tokens_minimum_is_one() -> None:
    """Tier 2: even a single character returns at least 1."""
    assert _approx_tokens("x") == 1


# ── _dirname ──────────────────────────────────────────────────────────────────


def test_dirname_returns_parent_directory() -> None:
    """Tier 2: '/a/b/c.jsonl' → '/a/b'."""
    assert _dirname("/a/b/c.jsonl") == "/a/b"


def test_dirname_single_component_returns_empty() -> None:
    """Tier 2: 'file.txt' has no parent → ''."""
    assert _dirname("file.txt") == ""


def test_dirname_root_file_returns_empty() -> None:
    """Tier 2: '/file.txt' parent is '/' but idx==0 → ''."""
    assert _dirname("/file.txt") == ""
