"""Tier 1: pure helpers in interfaces/repl/renderer.py.

  ``_meta_prefix(meta)``     — builds [skill#run_id] prefix from meta dict
  ``_short(v, n)``           — collapses whitespace + truncates any value
  ``_summarize_args(args)``  — compact k=v summary of a tool args dict
  ``_summarize_result(tool, result)`` — human one-line tool result summary

#3891: relabeled from a stale "Tier 2" (these are directly-testable function
contracts, not OS-level invariants reyn promises externally — the original
"Tier 2" was a mislabel, not a Tier-4 formatting trivia problem).

#3891 (owner-directed): a relabel alone was rejected — the six-questions Q3
("would it stay green with the mechanism dead?") was run against every test
in `_summarize_result`'s group, since display-formatting helpers are exactly
where a substring assertion coincidentally overlaps the same helper's own
dict/list `repr()` fallback, silently surviving the branch it claims to test
being deleted or subtly broken. 7 of 11 were found vacuous this way and
strengthened; see each test's own docstring for its specific finding.
"""
from __future__ import annotations

import sys

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src"
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
    """Tier 1: actor + run_id_short → '[skill#abcd] ' prefix."""
    assert _meta_prefix({"actor": "research", "run_id_short": "ab12"}) == "[research#ab12] "


def test_meta_prefix_skill_only() -> None:
    """Tier 1: actor without run_id_short → '[skill] ' prefix."""
    assert _meta_prefix({"actor": "finder"}) == "[finder] "


def test_meta_prefix_run_id_only() -> None:
    """Tier 1: run_id_short without actor → '[#abcd] ' prefix."""
    assert _meta_prefix({"run_id_short": "cd34"}) == "[#cd34] "


def test_meta_prefix_empty_meta() -> None:
    """Tier 1: empty meta dict → empty string."""
    assert _meta_prefix({}) == ""


def test_meta_prefix_unrelated_keys_ignored() -> None:
    """Tier 1: keys other than actor/run_id_short produce empty string."""
    assert _meta_prefix({"status": "done", "turn_id": "42"}) == ""


# ---------------------------------------------------------------------------
# _short
# ---------------------------------------------------------------------------


def test_short_none_returns_empty() -> None:
    """Tier 1: None input returns empty string."""
    assert _short(None) == ""


def test_short_short_string_unchanged() -> None:
    """Tier 1: string under the default cap is returned as-is."""
    assert _short("hello world") == "hello world"


def test_short_collapses_whitespace() -> None:
    """Tier 1: multiple spaces/newlines are collapsed to single spaces."""
    assert _short("a  b\n  c") == "a b c"


def test_short_truncates_at_default_limit() -> None:
    """Tier 1: string exceeding 60 chars is truncated with '…' at position 59."""
    long = "x" * 65
    result = _short(long)
    assert result == "x" * 59 + "…"


def test_short_custom_limit() -> None:
    """Tier 1: explicit n truncates at that length (9 chars + '…' = n=10)."""
    result = _short("a" * 20, n=10)
    assert result == "a" * 9 + "…"


def test_short_non_string_uses_repr() -> None:
    """Tier 1: non-string values are repr'd before truncation."""
    result = _short(42)
    assert result == "42"


def test_short_dict_uses_repr() -> None:
    """Tier 1: dict is repr'd (not JSON-encoded)."""
    result = _short({"a": 1}, n=100)
    assert "a" in result
    assert "1" in result


# ---------------------------------------------------------------------------
# _summarize_args
# ---------------------------------------------------------------------------


def test_summarize_args_empty_dict() -> None:
    """Tier 1: empty dict returns empty string."""
    assert _summarize_args({}) == ""


def test_summarize_args_none() -> None:
    """Tier 1: None returns empty string."""
    assert _summarize_args(None) == ""


def test_summarize_args_single_key() -> None:
    """Tier 1: single-key dict renders as 'key=value'.

    The ``"path="`` (with the ``=``) is the discriminating check — a dead
    dict-branch falls to the bare-value path, whose ``repr()`` output uses
    ``'path':`` (a colon, from Python dict repr), never ``path=``."""
    result = _summarize_args({"path": "/tmp/file.txt"})
    assert "path=" in result
    assert "/tmp/file.txt" in result


def test_summarize_args_multiple_keys() -> None:
    """Tier 1: multiple keys render comma-separated. Same ``=``-vs-``:``
    discriminator as the single-key test above."""
    result = _summarize_args({"a": "x", "b": "y"})
    assert "a=" in result
    assert "b=" in result


def test_summarize_args_bare_string() -> None:
    """Tier 1: non-dict arg is shortened to a one-liner."""
    result = _summarize_args("hello")
    assert "hello" in result


# ---------------------------------------------------------------------------
# _summarize_result
# ---------------------------------------------------------------------------


def test_summarize_result_none_returns_done() -> None:
    """Tier 1: None result → 'done'."""
    assert _summarize_result("any_tool", None) == "done"


def test_summarize_result_empty_string_returns_done() -> None:
    """Tier 1: empty-string result → 'done'."""
    assert _summarize_result("any_tool", "") == "done"


def test_summarize_result_list_uses_item_count() -> None:
    """Tier 1: list result → 'N items', exact match.

    #3891 Q3 finding: the prior ``"3" in result`` substring check stayed
    green even with the whole list-counting branch DELETED — ``repr([1, 2,
    3])`` is ``"[1, 2, 3]"``, which itself contains ``"3"``, so the assertion
    could not tell "the branch computed 3" from "the branch never ran and
    Python's own list repr happened to have a 3 in it". Exact equality on
    the branch's own wording closes that gap."""
    result = _summarize_result("any_tool", [1, 2, 3])
    assert result == "3 items"


def test_summarize_result_list_singular() -> None:
    """Tier 1: single-element list uses singular 'item', exact match.

    #3891 Q3 finding: the prior ``"1 item" in result`` check stays green even
    against a BROKEN singular/plural ternary (always appending 's') — "1
    item" is a substring of "1 items", so a pluralization regression was
    invisible to this test. Exact equality closes that gap."""
    result = _summarize_result("any_tool", ["x"])
    assert result == "1 item"


def test_summarize_result_list_search_shows_count() -> None:
    """Tier 1: list from a search tool uses the word 'results', not 'items'.

    #3891 Q3 finding: the prior ``"2" in result`` check never verified the
    thing its own docstring claimed ("includes the item count" — but not
    that a SEARCH tool's word differs from a non-search tool's). ``repr([1,
    2])`` also contains "2", so the assertion passed whether or not the
    ``"search" in t`` branch ran at all. Exact equality on the actual
    'results' wording closes both gaps."""
    result = _summarize_result("web_search", [1, 2])
    assert result == "2 results"


def test_summarize_result_read_op_counts_lines() -> None:
    """Tier 1: dict with op=read and content counts newlines + 1, exact match."""
    result = _summarize_result("read_file", {"op": "read", "content": "line1\nline2\nline3"})
    assert result == "Read 3 lines"


def test_summarize_result_read_op_singular() -> None:
    """Tier 1: single-line content uses 'line' not 'lines', exact match.

    #3891 Q3 finding: same substring-survives-pluralization-bug class as
    ``test_summarize_result_list_singular`` above — "1 line" is a substring
    of "1 lines"."""
    result = _summarize_result("read_file", {"op": "read", "content": "one line"})
    assert result == "Read 1 line"


def test_summarize_result_write_op_with_path() -> None:
    """Tier 1: dict with op=write includes the path in the result, exact match.

    #3891 Q3 finding: the prior ``"/out.txt" in result`` check stayed green
    even with the whole write-branch DELETED — the dict's own
    ``repr({"op": "write", "path": "/out.txt"})`` contains ``/out.txt`` too.
    Exact equality on the branch's own "Wrote ..." wording closes that gap."""
    result = _summarize_result("write_file", {"op": "write", "path": "/out.txt"})
    assert result == "Wrote /out.txt"


def test_summarize_result_edit_op_with_path() -> None:
    """Tier 1: dict with op=edit includes the path in the result, exact match.

    #3891 Q3 finding: same repr-fallback-survives-deletion class as
    ``test_summarize_result_write_op_with_path`` above."""
    result = _summarize_result("edit_file", {"op": "edit", "path": "/src.py"})
    assert result == "Edited /src.py"


def test_summarize_result_dict_with_status() -> None:
    """Tier 1: dict with a status key → the bare status string, exact match.

    #3891 Q3 finding: the prior ``"ok" in result`` check stayed green even
    with the ``if status: return str(status)`` branch DELETED —
    ``repr({"status": "ok"})`` contains ``ok`` too. Exact equality closes
    that gap."""
    result = _summarize_result("any_tool", {"status": "ok"})
    assert result == "ok"


def test_summarize_result_fallback_repr() -> None:
    """Tier 1: unrecognised non-empty value degrades to truncated repr.

    Unlike the dict-branch tests above, this test's OWN subject is the
    fallback path itself (a bare ``42``, matching no dict/list branch at
    all) — there is no earlier branch for it to vacuously survive the
    deletion of."""
    result = _summarize_result("any_tool", 42)
    assert result == "42"
