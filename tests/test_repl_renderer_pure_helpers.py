"""Tier 2: pure helpers in interfaces/repl/renderer.py.

  ``_meta_prefix(meta)``     — builds [skill#run_id] prefix from meta dict
  ``_short(v, n)``           — collapses whitespace + truncates any value
  ``_summarize_args(args)``  — compact k=v summary of a tool args dict
  ``_summarize_result(tool, result)`` — human one-line tool result summary
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.repl.renderer import (
    _meta_prefix,
    _short,
    _summarize_args,
    _summarize_result,
)

# ---------------------------------------------------------------------------
# _meta_prefix
# ---------------------------------------------------------------------------


def test_meta_prefix_both_skill_and_run_id() -> None:
    """Tier 2: skill_name + run_id_short → '[skill#abcd] ' prefix."""
    assert _meta_prefix({"skill_name": "research", "run_id_short": "ab12"}) == "[research#ab12] "


def test_meta_prefix_skill_only() -> None:
    """Tier 2: skill_name without run_id_short → '[skill] ' prefix."""
    assert _meta_prefix({"skill_name": "finder"}) == "[finder] "


def test_meta_prefix_run_id_only() -> None:
    """Tier 2: run_id_short without skill_name → '[#abcd] ' prefix."""
    assert _meta_prefix({"run_id_short": "cd34"}) == "[#cd34] "


def test_meta_prefix_empty_meta() -> None:
    """Tier 2: empty meta dict → empty string."""
    assert _meta_prefix({}) == ""


def test_meta_prefix_unrelated_keys_ignored() -> None:
    """Tier 2: keys other than skill_name/run_id_short produce empty string."""
    assert _meta_prefix({"status": "done", "turn_id": "42"}) == ""


# ---------------------------------------------------------------------------
# _short
# ---------------------------------------------------------------------------


def test_short_none_returns_empty() -> None:
    """Tier 2: None input returns empty string."""
    assert _short(None) == ""


def test_short_short_string_unchanged() -> None:
    """Tier 2: string under the default cap is returned as-is."""
    assert _short("hello world") == "hello world"


def test_short_collapses_whitespace() -> None:
    """Tier 2: multiple spaces/newlines are collapsed to single spaces."""
    assert _short("a  b\n  c") == "a b c"


def test_short_truncates_at_default_limit() -> None:
    """Tier 2: string exceeding 60 chars is truncated with '…' at position 59."""
    long = "x" * 65
    result = _short(long)
    assert result == "x" * 59 + "…"


def test_short_custom_limit() -> None:
    """Tier 2: explicit n truncates at that length (9 chars + '…' = n=10)."""
    result = _short("a" * 20, n=10)
    assert result == "a" * 9 + "…"


def test_short_non_string_uses_repr() -> None:
    """Tier 2: non-string values are repr'd before truncation."""
    result = _short(42)
    assert result == "42"


def test_short_dict_uses_repr() -> None:
    """Tier 2: dict is repr'd (not JSON-encoded)."""
    result = _short({"a": 1}, n=100)
    assert "a" in result
    assert "1" in result


# ---------------------------------------------------------------------------
# _summarize_args
# ---------------------------------------------------------------------------


def test_summarize_args_empty_dict() -> None:
    """Tier 2: empty dict returns empty string."""
    assert _summarize_args({}) == ""


def test_summarize_args_none() -> None:
    """Tier 2: None returns empty string."""
    assert _summarize_args(None) == ""


def test_summarize_args_single_key() -> None:
    """Tier 2: single-key dict renders as 'key=value'."""
    result = _summarize_args({"path": "/tmp/file.txt"})
    assert "path=" in result
    assert "/tmp/file.txt" in result


def test_summarize_args_multiple_keys() -> None:
    """Tier 2: multiple keys render comma-separated."""
    result = _summarize_args({"a": "x", "b": "y"})
    assert "a=" in result
    assert "b=" in result


def test_summarize_args_bare_string() -> None:
    """Tier 2: non-dict arg is shortened to a one-liner."""
    result = _summarize_args("hello")
    assert "hello" in result


# ---------------------------------------------------------------------------
# _summarize_result
# ---------------------------------------------------------------------------


def test_summarize_result_none_returns_done() -> None:
    """Tier 2: None result → 'done'."""
    assert _summarize_result("any_tool", None) == "done"


def test_summarize_result_empty_string_returns_done() -> None:
    """Tier 2: empty-string result → 'done'."""
    assert _summarize_result("any_tool", "") == "done"


def test_summarize_result_list_uses_item_count() -> None:
    """Tier 2: list result → 'N items'."""
    result = _summarize_result("any_tool", [1, 2, 3])
    assert "3" in result


def test_summarize_result_list_singular() -> None:
    """Tier 2: single-element list uses singular 'item'."""
    result = _summarize_result("any_tool", ["x"])
    assert "1 item" in result


def test_summarize_result_list_search_uses_results_word() -> None:
    """Tier 2: list from a search tool uses 'result' not 'item'."""
    result = _summarize_result("web_search", [1, 2])
    assert "result" in result


def test_summarize_result_read_op_counts_lines() -> None:
    """Tier 2: dict with op=read and content counts newlines + 1."""
    result = _summarize_result("file__read", {"op": "read", "content": "line1\nline2\nline3"})
    assert "Read 3 lines" in result or "3 line" in result


def test_summarize_result_read_op_singular() -> None:
    """Tier 2: single-line content uses 'line' not 'lines'."""
    result = _summarize_result("file__read", {"op": "read", "content": "one line"})
    assert "1 line" in result


def test_summarize_result_write_op_with_path() -> None:
    """Tier 2: dict with op=write and path → 'Wrote <path>'."""
    result = _summarize_result("file__write", {"op": "write", "path": "/out.txt"})
    assert "Wrote" in result
    assert "/out.txt" in result


def test_summarize_result_edit_op_with_path() -> None:
    """Tier 2: dict with op=edit and path → 'Edited <path>'."""
    result = _summarize_result("file__edit", {"op": "edit", "path": "/src.py"})
    assert "Edited" in result
    assert "/src.py" in result


def test_summarize_result_dict_with_status() -> None:
    """Tier 2: dict with a status key → status string."""
    result = _summarize_result("any_tool", {"status": "ok"})
    assert "ok" in result


def test_summarize_result_fallback_repr() -> None:
    """Tier 2: unrecognised non-empty value degrades to truncated repr."""
    result = _summarize_result("any_tool", 42)
    assert "42" in result
