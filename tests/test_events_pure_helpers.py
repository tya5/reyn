"""Tier 2: pure helpers in core/events/.

``config_generations._encode``         — path → filename-safe encoding
``config_generations._decode``         — reverse: filename-safe → path
``snapshot_generations._abandoned_intervals`` — rewind list → abandoned (N,R) intervals
``snapshot_generations._make_is_active``     — closure: seq in abandoned? → bool
``snapshot_generations._branch_of_seq``      — owning branch_id of a seq number
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.core.events.config_generations import _decode, _encode
from reyn.core.events.snapshot_generations import (
    ACTIVE_BRANCH_ID,
    _abandoned_intervals,
    _branch_of_seq,
    _make_is_active,
)

# ---------------------------------------------------------------------------
# config_generations._encode / _decode
# ---------------------------------------------------------------------------


def test_encode_flat_filename_unchanged() -> None:
    """Tier 2: a filename with no slashes encodes to itself."""
    assert _encode("mcp.yaml") == "mcp.yaml"


def test_encode_path_separators_become_double_underscore() -> None:
    """Tier 2: slashes are replaced with '__'."""
    assert _encode("config/mcp.yaml") == "config__mcp.yaml"


def test_encode_nested_path() -> None:
    """Tier 2: multiple path segments all converted."""
    assert _encode("a/b/c.yaml") == "a__b__c.yaml"


def test_decode_reverses_encode() -> None:
    """Tier 2: decode(_encode(x)) == x for a '/'-separated path with no '__' in any segment.

    _encode replaces '/' with '__' and _decode is the inverse replacement.
    Existing '__' in a path segment would collide (encoding is not escape-safe),
    but real config-registry paths (e.g. 'config/mcp.yaml') do not contain '__'.
    """
    original = "config/mcp.yaml"
    assert _decode(_encode(original)) == original


def test_decode_flat_unchanged() -> None:
    """Tier 2: already-flat filename decodes to itself."""
    assert _decode("mcp.yaml") == "mcp.yaml"


def test_encode_decode_roundtrip_nested() -> None:
    """Tier 2: round-trip preserves a nested path whose segments contain no '__'."""
    path = "a/b/c/d.yaml"
    assert _decode(_encode(path)) == path


def test_encode_decode_roundtrips_path_with_double_underscore() -> None:
    """Tier 2: #2993 — a rel_path containing '__' now round-trips correctly instead of the
    #2352 guard raising. The #2352 guard assumed every real caller passes a fixed literal
    path with no '__' in it; that assumption broke for agent-scoped paths like
    'agents/<name>/hooks.yaml' where '<name>' may legally contain '__' (_AGENT_NAME_RE
    permits it), so the guard raised AFTER the .yaml write had already landed (a recovery
    hole, not a rejected write). The new encoding escapes '_' → '%5F' (and '%' → '%25')
    BEFORE mapping '/' → '__', so '__' in the original content never collides with the
    '/' separator marker."""
    original = "a__b/c.yaml"
    assert _decode(_encode(original)) == original


_INJECTIVE_ROUNDTRIP_CASES = [
    "config/mcp.yaml",
    "a__b/c.yaml",
    "a/b/c",
    "agents/my__agent/hooks.yaml",
    "agents/my_agent/hooks.yaml",
    "a%b/c.yaml",
    "a%5Fb/c.yaml",
    "a%25b/c.yaml",
    "a_b__c/d.yaml",
    "%__%5F__%25",
    "___",
    "____",
    "/",
    "",
    "a/b__c/d_e%f",
]


@pytest.mark.parametrize("original", _INJECTIVE_ROUNDTRIP_CASES)
def test_encode_decode_injective_roundtrip(original: str) -> None:
    """Tier 2: #2993 — _decode(_encode(x)) == x for ANY string, including combinations of
    '_', '__', '%', '%5F', '%25', and '/' — no assumption about the input alphabet (the
    escape-then-map scheme is injective by construction, unlike the old direct '/' → '__'
    map which only round-tripped '__'-free paths)."""
    assert _decode(_encode(original)) == original


def test_encode_distinguishes_former_collision_pair() -> None:
    """Tier 2: #2993 non-vacuous witness — under the OLD encoding, 'a__b/c' and 'a/b/c' both
    mapped to the same safe-rel 'a__b__c' (a real collision: two distinct config paths would
    share one generation file). Under the new encoding they must map to DIFFERENT safe-rels."""
    assert _encode("a__b/c") != _encode("a/b/c")


# ---------------------------------------------------------------------------
# snapshot_generations._abandoned_intervals
# ---------------------------------------------------------------------------


def test_abandoned_intervals_empty_rewinds() -> None:
    """Tier 2: no rewinds → empty abandoned list."""
    assert _abandoned_intervals([]) == []


def test_abandoned_intervals_single_rewind() -> None:
    """Tier 2: single rewind (R=5, N=2) → interval (2, 5) abandoned."""
    result = _abandoned_intervals([(5, 2)])
    assert (2, 5) in result


def test_abandoned_intervals_later_rewind_subsumes_earlier() -> None:
    """Tier 2: a later rewind (R=10, N=1) that includes an earlier (R=5, N=2):
    the R=5 record is inside interval (1, 10), so it's subsumed (moot)."""
    # R=10 target=1, R=5 target=2
    result = _abandoned_intervals([(5, 2), (10, 1)])
    # (1, 10) should be present; (2, 5) is subsumed since 1 < 5 < 10
    assert (1, 10) in result
    assert (2, 5) not in result


def test_abandoned_intervals_non_overlapping_both_active() -> None:
    """Tier 2: two non-overlapping rewinds both produce intervals."""
    # R=5 target=3 → (3,5), R=10 target=7 → (7,10) — no nesting
    result = _abandoned_intervals([(5, 3), (10, 7)])
    assert (3, 5) in result
    assert (7, 10) in result


# ---------------------------------------------------------------------------
# snapshot_generations._make_is_active
# ---------------------------------------------------------------------------


def test_make_is_active_no_abandoned() -> None:
    """Tier 2: all seqs active when abandoned list is empty."""
    is_active = _make_is_active([])
    assert is_active(1) is True
    assert is_active(99) is True


def test_make_is_active_seq_inside_abandoned_interval() -> None:
    """Tier 2: seq inside an abandoned interval → not active."""
    is_active = _make_is_active([(2, 5)])
    assert is_active(3) is False
    assert is_active(4) is False


def test_make_is_active_boundary_seqs_not_inside() -> None:
    """Tier 2: interval boundaries (N and R) are excluded (strict < comparison)."""
    is_active = _make_is_active([(2, 5)])
    # Interval is open: lo < seq < hi
    assert is_active(2) is True
    assert is_active(5) is True


def test_make_is_active_seq_outside_interval() -> None:
    """Tier 2: seq before or after interval → active."""
    is_active = _make_is_active([(2, 5)])
    assert is_active(1) is True
    assert is_active(6) is True


# ---------------------------------------------------------------------------
# snapshot_generations._branch_of_seq
# ---------------------------------------------------------------------------


def test_branch_of_seq_no_abandoned_returns_active() -> None:
    """Tier 2: no abandoned intervals → every seq is on the active branch (0)."""
    assert _branch_of_seq(3, []) == ACTIVE_BRANCH_ID


def test_branch_of_seq_inside_abandoned_interval() -> None:
    """Tier 2: seq inside (N=2, R=5) → branch_id is R=5."""
    assert _branch_of_seq(3, [(2, 5)]) == 5


def test_branch_of_seq_boundary_is_active() -> None:
    """Tier 2: interval boundaries are excluded (strict <); seq=2 and seq=5 are active."""
    assert _branch_of_seq(2, [(2, 5)]) == ACTIVE_BRANCH_ID
    assert _branch_of_seq(5, [(2, 5)]) == ACTIVE_BRANCH_ID


def test_branch_of_seq_outside_all_intervals() -> None:
    """Tier 2: seq outside all abandoned intervals → active branch."""
    assert _branch_of_seq(10, [(2, 5), (7, 9)]) == ACTIVE_BRANCH_ID


def test_branch_of_seq_innermost_interval_wins() -> None:
    """Tier 2: for nested intervals the tightest (largest N) interval wins."""
    # Outer: (1, 10), Inner: (3, 7). seq=4 is in both; tightest = (3,7) → R=7
    assert _branch_of_seq(4, [(1, 10), (3, 7)]) == 7
