"""Tier 2: chat_compactor postprocessor pins seq extraction correctness.

The chat_compactor skill delegates `covers_through_seq` (= max seq among
the input new_turns) from the LLM to a deterministic Python postprocessor
step. Getting this value wrong corrupts ChatSession.history — a too-low
value re-includes already-summarized turns (duplication); a too-high value
skips turns that have not been folded into a summary (loss).

This file pins the contract of `compute_covers_through_seq`:

  - max from a normal list → returns the largest seq
  - empty / missing list → returns 0 (ChatSession falls back gracefully)
  - single-entry list → returns that entry's seq
  - non-monotonic order → still returns the maximum
  - ``new_turn_seqs`` is dropped from the returned data dict (transit only)
  - other section content (topic_arc / decisions / etc.) survives unchanged

These are the invariants ChatSession depends on. The postprocessor's wiring
into the skill (skill.md frontmatter) and its end-to-end behavior with the
OS runtime are covered by `test_skill_postprocessor_e2e.py`; here we only
contract-test the function itself.
"""
from __future__ import annotations

from reyn.stdlib.skills.chat_compactor.postprocessor import (
    compute_covers_through_seq,
)


def test_max_of_normal_list() -> None:
    """Tier 2: returns the largest seq from a typical input."""
    artifact = {
        "data": {
            "topic_arc": "discussing widgets",
            "new_turn_seqs": [1, 5, 3],
        }
    }
    out = compute_covers_through_seq(artifact)
    assert out["covers_through_seq"] == 5


def test_empty_list_returns_zero() -> None:
    """Tier 2: empty seq list yields 0 (caller falls back to candidates[-1].seq)."""
    artifact = {"data": {"topic_arc": "empty", "new_turn_seqs": []}}
    out = compute_covers_through_seq(artifact)
    assert out["covers_through_seq"] == 0


def test_missing_field_returns_zero() -> None:
    """Tier 2: absent new_turn_seqs key still returns 0, never raises."""
    artifact = {"data": {"topic_arc": "absent"}}
    out = compute_covers_through_seq(artifact)
    assert out["covers_through_seq"] == 0


def test_single_entry() -> None:
    """Tier 2: single-element list returns that element verbatim."""
    artifact = {"data": {"topic_arc": "one", "new_turn_seqs": [42]}}
    out = compute_covers_through_seq(artifact)
    assert out["covers_through_seq"] == 42


def test_non_monotonic_order() -> None:
    """Tier 2: max() is order-independent — out-of-order input still works."""
    artifact = {
        "data": {
            "topic_arc": "nonmono",
            "new_turn_seqs": [7, 2, 9, 4, 1],
        }
    }
    out = compute_covers_through_seq(artifact)
    assert out["covers_through_seq"] == 9


def test_new_turn_seqs_dropped_from_output() -> None:
    """Tier 2: transit-only field is removed before caller-facing output."""
    artifact = {
        "data": {
            "topic_arc": "drop me",
            "new_turn_seqs": [1, 2, 3],
        }
    }
    out = compute_covers_through_seq(artifact)
    assert "new_turn_seqs" not in out


def test_section_content_preserved() -> None:
    """Tier 2: non-seq fields survive postprocessor unchanged."""
    artifact = {
        "data": {
            "topic_arc": "the arc",
            "decisions": ["d1", "d2"],
            "pending": ["p1"],
            "session_user_facts": ["uf1"],
            "artifacts_referenced": ["ar1"],
            "new_turn_seqs": [1, 2],
        }
    }
    out = compute_covers_through_seq(artifact)
    assert out["topic_arc"] == "the arc"
    assert out["decisions"] == ["d1", "d2"]
    assert out["pending"] == ["p1"]
    assert out["session_user_facts"] == ["uf1"]
    assert out["artifacts_referenced"] == ["ar1"]
    assert out["covers_through_seq"] == 2


def test_string_seqs_coerced_to_int() -> None:
    """Tier 2: defensive — JSON-roundtripped numbers can be ints either way.

    The contract requires integers per the LLM-output schema, but if the
    JSON harness ever yields strings (e.g. from a sloppy LLM cassette),
    int() coercion keeps the postprocessor from raising on a recoverable
    surface error. The output_schema validation downstream catches truly
    bad values.
    """
    artifact = {"data": {"topic_arc": "x", "new_turn_seqs": ["3", "1", "5"]}}
    out = compute_covers_through_seq(artifact)
    assert out["covers_through_seq"] == 5
