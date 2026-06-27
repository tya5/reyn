"""Tier 2: tool-result summary — CC-style one-liners + graceful fallback.

`summarize_tool_result` turns a raw op result into a human line (``Read 42
lines``) per tool/shape, and ALWAYS degrades gracefully on an unknown / empty /
oversized / malformed result (never raises, never dumps raw for a known shape).
"""
from __future__ import annotations

from reyn.interfaces.repl.renderer import summarize_tool_result


def test_file_read_reports_line_count() -> None:
    """Tier 2: a file read summarises to 'Read N lines'."""
    out = summarize_tool_result(
        "file__read", {"op": "read", "status": "ok", "content": "a\nb\nc"}
    )
    assert out == "Read 3 lines"


def test_read_with_none_content_no_status_is_clean_not_raw_repr() -> None:
    """Tier 2: a read result whose content is None and carries no status gets a
    clean note, not a raw-dict dump."""
    out = summarize_tool_result("file__read", {"op": "read", "content": None})
    assert out == "Read (no content)"
    assert "{" not in out and "None" not in out


def test_read_with_none_content_but_status_shows_status() -> None:
    """Tier 2: a read that errored (content None) still surfaces its status."""
    out = summarize_tool_result(
        "file__read", {"op": "read", "content": None, "status": "error"}
    )
    assert out == "error"


def test_file_read_truncated_is_flagged() -> None:
    """Tier 2: a truncated read says so."""
    out = summarize_tool_result(
        "file__read", {"op": "read", "status": "truncated", "content": "x\ny"}
    )
    assert "truncated" in out


def test_file_write_and_edit_name_the_path() -> None:
    """Tier 2: write / edit name the path."""
    assert summarize_tool_result("file__write", {"op": "write", "path": "f.py"}) == "Wrote f.py"
    assert summarize_tool_result("file__edit", {"op": "edit", "path": "g.py"}) == "Edited g.py"


def test_web_search_counts_results() -> None:
    """Tier 2: a search list summarises to 'N results'."""
    assert summarize_tool_result("web__search", ["a", "b", "c"]) == "3 results"


def test_generic_list_counts_items_with_pluralisation() -> None:
    """Tier 2: a generic list → 'N items', singular for one."""
    assert summarize_tool_result("anything", [1, 2]) == "2 items"
    assert summarize_tool_result("anything", ["only"]) == "1 item"


def test_dict_with_status_shows_status() -> None:
    """Tier 2: an opaque dict with a status field shows the status."""
    assert summarize_tool_result("mcp__call", {"status": "ok", "x": 1}) == "ok"


def test_empty_or_none_reports_done() -> None:
    """Tier 2: empty / None result → 'done', not a blank line."""
    assert summarize_tool_result("x", None) == "done"
    assert summarize_tool_result("x", "") == "done"


def test_oversized_result_is_truncated_one_line() -> None:
    """Tier 2: a huge / multiline result collapses to a truncated single line."""
    out = summarize_tool_result("x", "z" * 500 + "\ntail")
    assert "\n" not in out
    assert "…" in out
    assert "tail" not in out


def test_unknown_shape_degrades_without_raising() -> None:
    """Tier 2: a malformed/unknown result returns a string, never raises."""
    weird = {"op": object(), "nested": [object()]}
    out = summarize_tool_result("x", weird)
    assert isinstance(out, str) and out  # some non-empty summary, no crash
