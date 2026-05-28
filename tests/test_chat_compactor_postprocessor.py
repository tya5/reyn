"""Tier 2: covers_through_seq derivation pins seq extraction correctness.

PR-N3: `compute_covers_through_seq` moved from the retired
`chat_compactor` skill postprocessor to
`reyn.chat.services.chat_compaction_engine`. The function signature
changed: it now takes ``new_turn_seqs: list`` directly instead of
an ``artifact`` dict wrapper.

Getting this value wrong corrupts ChatSession.history — a too-low
value re-includes already-summarized turns (duplication); a too-high value
skips turns that have not been folded into a summary (loss).

Contract:
  - max from a normal list → returns the largest seq
  - empty / missing list → returns 0 (ChatSession falls back gracefully)
  - single-entry list → returns that entry's seq
  - non-monotonic order → still returns the maximum
"""
from __future__ import annotations

from reyn.chat.services.chat_compaction_engine import compute_covers_through_seq


def test_max_of_normal_list() -> None:
    """Tier 2: returns the largest seq from a typical input."""
    assert compute_covers_through_seq([1, 5, 3]) == 5


def test_empty_list_returns_zero() -> None:
    """Tier 2: empty seq list yields 0 (caller falls back to candidates[-1].seq)."""
    assert compute_covers_through_seq([]) == 0


def test_missing_field_returns_zero() -> None:
    """Tier 2: None treated as empty → returns 0, never raises."""
    assert compute_covers_through_seq(None) == 0  # type: ignore[arg-type]


def test_single_entry() -> None:
    """Tier 2: single-element list returns that element verbatim."""
    assert compute_covers_through_seq([42]) == 42


def test_non_monotonic_order() -> None:
    """Tier 2: max() is order-independent — out-of-order input still works."""
    assert compute_covers_through_seq([7, 2, 9, 4, 1]) == 9


def test_string_seqs_coerced_to_int() -> None:
    """Tier 2: defensive — JSON-roundtripped numbers can arrive as strings;
    int() coercion keeps the function from raising on a recoverable surface error.
    """
    assert compute_covers_through_seq(["3", "1", "5"]) == 5
