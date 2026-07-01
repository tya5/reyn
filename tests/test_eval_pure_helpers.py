"""Tier 2: `reyn eval` CLI — _matches_tags + _make_case_id + _format_timestamp helpers.

Three pure helpers in `interfaces/cli/commands/eval.py` with minimal or no direct
coverage.  Pinning them prevents a silent change to the tag-filter logic, case-id
derivation, or timestamp formatting from breaking eval run output.
"""
from __future__ import annotations

from reyn.interfaces.cli.commands.eval import (
    _format_timestamp,
    _make_case_id,
    _matches_tags,
)

# ── _matches_tags ──────────────────────────────────────────────────────────


def test_matches_tags_no_filter_always_true() -> None:
    """Tier 2: tag_filter=None → all cases match (run everything)."""
    assert _matches_tags({"tags": ["slow"]}, None) is True
    assert _matches_tags({}, None) is True


def test_matches_tags_matching_tag() -> None:
    """Tier 2: case tags intersect tag_filter → True."""
    assert _matches_tags({"tags": ["fast", "unit"]}, {"unit"}) is True


def test_matches_tags_no_intersection() -> None:
    """Tier 2: no intersection → False (case is skipped)."""
    assert _matches_tags({"tags": ["slow"]}, {"fast"}) is False


def test_matches_tags_case_has_no_tags() -> None:
    """Tier 2: case with no tags → False when filter is non-empty."""
    assert _matches_tags({}, {"unit"}) is False
    assert _matches_tags({"tags": []}, {"unit"}) is False


def test_matches_tags_case_tags_none_value() -> None:
    """Tier 2: 'tags': None treated as empty set → False when filter non-empty."""
    assert _matches_tags({"tags": None}, {"unit"}) is False


# ── _make_case_id ──────────────────────────────────────────────────────────


def test_make_case_id_explicit_id_wins() -> None:
    """Tier 2: case with explicit 'id' key → that id is returned as-is."""
    case = {"id": "my-case-42", "input": {"q": "hello"}}
    assert _make_case_id(case) == "my-case-42"


def test_make_case_id_with_tags_uses_sorted_tags() -> None:
    """Tier 2: no id + tags → sorted tags joined with '_' as prefix."""
    case = {"tags": ["z_tag", "a_tag"], "input": {}}
    cid = _make_case_id(case)
    # Tags should be sorted ('a_tag_z_tag') before the hash suffix
    assert cid.startswith("a_tag_z_tag_")


def test_make_case_id_no_tags_uses_case_prefix() -> None:
    """Tier 2: no id + no tags → 'case_' prefix before hash suffix."""
    case = {"input": {"x": 1}}
    cid = _make_case_id(case)
    assert cid.startswith("case_")


def test_make_case_id_same_input_is_stable() -> None:
    """Tier 2: same case dict always yields the same id (deterministic)."""
    case = {"input": {"a": 1, "b": 2}}
    assert _make_case_id(case) == _make_case_id(case)


def test_make_case_id_different_input_differs() -> None:
    """Tier 2: different inputs yield different ids (hash distinguishes them)."""
    assert _make_case_id({"input": {"x": 1}}) != _make_case_id({"input": {"x": 2}})


# ── _format_timestamp ──────────────────────────────────────────────────────


def test_format_timestamp_valid_iso_stem() -> None:
    """Tier 2: valid '20260514T213000Z' stem → '2026-05-14 21:30'."""
    assert _format_timestamp("20260514T213000Z") == "2026-05-14 21:30"


def test_format_timestamp_without_trailing_z() -> None:
    """Tier 2: stem without trailing Z still parses (rstrip is idempotent)."""
    assert _format_timestamp("20260514T213000") == "2026-05-14 21:30"


def test_format_timestamp_malformed_falls_back() -> None:
    """Tier 2: unrecognised stem → raw stem returned (no crash)."""
    assert _format_timestamp("not-a-timestamp") == "not-a-timestamp"
    assert _format_timestamp("") == ""
