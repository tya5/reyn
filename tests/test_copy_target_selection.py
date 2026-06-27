"""Tier 2: /copy reply selection (_copy_target).

The output loop buffers recent agent replies newest-first; `/copy [N|list]`
resolves which reply (or a list view / error) to act on. This pins that pure
selection: newest by default, 1-based indexing back through the ring, a list
view, and clean messages for empty buffer / out-of-range / bad args (no silent
no-op, no clipboard call when there is nothing to copy).
"""
from __future__ import annotations

from collections import deque

from reyn.interfaces.repl.repl import _copy_target


def test_default_selects_newest_reply() -> None:
    """Tier 2: no arg → the newest reply (index 0)."""
    text, status = _copy_target(deque(["newest", "older"]), "")
    assert text == "newest"
    assert status == ""


def test_numeric_arg_indexes_back_one_based() -> None:
    """Tier 2: /copy 2 → one reply back; 1 = newest."""
    buf = deque(["r1", "r2", "r3"])
    assert _copy_target(buf, "1")[0] == "r1"
    assert _copy_target(buf, "2")[0] == "r2"
    assert _copy_target(buf, "3")[0] == "r3"


def test_empty_buffer_reports_nothing_to_copy() -> None:
    """Tier 2: nothing buffered → no text, a clear status (not a clipboard call)."""
    text, status = _copy_target(deque(), "")
    assert text is None
    assert "no agent reply" in status


def test_out_of_range_reports_buffer_size() -> None:
    """Tier 2: N beyond the buffer → no text, says how many are buffered."""
    text, status = _copy_target(deque(["only"]), "5")
    assert text is None
    assert "only 1 reply" in status


def test_list_shows_buffered_count() -> None:
    """Tier 2: /copy list → a count view, never a copy."""
    text, status = _copy_target(deque(["a", "b"]), "list")
    assert text is None
    assert "2 replies buffered" in status
    assert _copy_target(deque(), "list")[1] == "no replies buffered yet"


def test_bad_arg_is_rejected_clearly() -> None:
    """Tier 2: a non-numeric / zero arg → a clear error, no copy."""
    for bad in ("xyz", "0", "-1"):
        text, status = _copy_target(deque(["r"]), bad)
        assert text is None
        assert "bad /copy arg" in status
