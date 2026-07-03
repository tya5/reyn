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


def test_file_read_singular_line() -> None:
    """Tier 2: a single-line read says 'Read 1 line', not 'Read 1 lines'."""
    out = summarize_tool_result(
        "file__read", {"op": "read", "status": "ok", "content": "only one line"}
    )
    assert out == "Read 1 line"


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


def test_file_create_treated_same_as_write() -> None:
    """Tier 2: op='create' uses the same 'Wrote …' branch as 'write'.

    A create op arriving from an MCP tool (or a future OS create op) must
    not fall through to the raw-repr fallback — it shares the 'write|create'
    branch. Pinning this prevents the branch being silently narrowed to
    write-only.
    """
    assert summarize_tool_result("file__create", {"op": "create", "path": "new.py"}) == "Wrote new.py"


def test_write_without_path_degrades_cleanly() -> None:
    """Tier 2: a write result with no path field → 'Wrote file', not a crash."""
    assert summarize_tool_result("file__write", {"op": "write"}) == "Wrote file"


def test_edit_without_path_degrades_cleanly() -> None:
    """Tier 2: an edit result with no path field → 'Edited file', not a crash."""
    assert summarize_tool_result("file__edit", {"op": "edit"}) == "Edited file"


def test_web_search_counts_results() -> None:
    """Tier 2: a search list summarises to 'N results'."""
    assert summarize_tool_result("web__search", ["a", "b", "c"]) == "3 results"


def test_generic_list_counts_items_with_pluralisation() -> None:
    """Tier 2: a generic list → 'N items', singular for one."""
    assert summarize_tool_result("anything", [1, 2]) == "2 items"
    assert summarize_tool_result("anything", ["only"]) == "1 item"


def test_task_list_result_shows_count() -> None:
    """Tier 2: task__list result counts tasks rather than falling through to 'ok'."""
    assert summarize_tool_result(
        "task__list", {"kind": "task.list", "status": "ok", "tasks": []}
    ) == "0 tasks"
    assert summarize_tool_result(
        "task__list", {"kind": "task.list", "status": "ok", "tasks": [{"id": "t1"}]}
    ) == "1 task"
    assert summarize_tool_result(
        "task__list", {"kind": "task.list", "status": "ok", "tasks": [{"id": "t1"}, {"id": "t2"}]}
    ) == "2 tasks"


def test_dict_with_error_key_shows_error_not_raw_repr() -> None:
    """Tier 2: a dict with an 'error' key shows the error message, not raw repr.

    `file__list` outside the project returns `{'error': 'glob not permitted…'}`.
    The summarizer must surface the error string, not dump the raw dict.
    """
    out = summarize_tool_result(
        "file__list",
        {"error": "glob not permitted: '/tmp/*' (outside project, no read permission)"},
    )
    assert "glob not permitted" in out
    assert "{" not in out, "raw dict repr must not leak into ⎿ row"


def test_file_read_not_found_shows_error_not_zero_lines() -> None:
    """Tier 2: file__read for a missing file shows the error, not 'Read 0 lines'.

    file__read returns {"op": "read", "content": "", "error": "file not found: …"}
    for a non-existent path.  Without the error-first guard the read branch fires
    on op=="read" and returns "Read 0 lines" (empty content → 0 lines), which looks
    identical to an empty file and gives the user no signal that the file is absent.
    """
    out = summarize_tool_result(
        "file__read",
        {"op": "read", "content": "", "error": "file not found: README.md"},
    )
    assert "0 lines" not in out, "missing file must not look like empty file"
    assert "not found" in out or "README" in out, "must surface the error"
    assert "{" not in out, "raw dict repr must not leak"


def test_file_list_shows_entry_count() -> None:
    """Tier 2: file__list result ({path, entries}) shows 'Listed N entries'.

    Without this branch the raw dict repr leaked into the ⎿ row.
    """
    assert summarize_tool_result(
        "file__list", {"path": "src/", "entries": ["a.py", "b.py", "c.py"]}
    ) == "Listed 3 entries"
    assert summarize_tool_result(
        "file__list", {"path": "src/", "entries": ["only.py"]}
    ) == "Listed 1 entry"


def test_file_grep_shows_match_count() -> None:
    """Tier 2: file__grep result (op='grep', count=N) shows 'N matches'.

    Without this branch grep fell through to status='ok' which is uninformative.
    """
    assert summarize_tool_result(
        "file__grep",
        {"op": "grep", "status": "ok", "count": 7, "matches": []},
    ) == "7 matches"
    assert summarize_tool_result(
        "file__grep",
        {"op": "grep", "status": "ok", "count": 1, "matches": []},
    ) == "1 match"


def test_file_glob_shows_match_count() -> None:
    """Tier 2: file__glob result ({pattern, matches, count}) shows 'N matches'.

    Without this branch the raw dict repr leaked into the ⎿ row.
    """
    assert summarize_tool_result(
        "file__glob",
        {"pattern": "src/**/*.py", "matches": ["a.py", "b.py"], "count": 2},
    ) == "2 matches"
    assert summarize_tool_result(
        "file__glob",
        {"pattern": "*.md", "matches": ["README.md"], "count": 1},
    ) == "1 match"


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
