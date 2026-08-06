"""Tier 1: scripts/test_tier_audit.py's cached source-segment extraction (#3670).

#3670: main's `test-tier audit` CI job timed out (15-minute limit) on 4 of
5 consecutive pushes. Profiled root cause: `_check_mock_in_func` called
`ast.get_source_segment(source, stmt)` for EVERY AST node walked in a test
function body — most of them thrown away immediately, since only the
`Import`/`ImportFrom` branch actually used the result — and every call
internally re-split the WHOLE file's source from scratch
(`ast.get_source_segment` -> `ast._splitlines_no_ff`). Full-tree profile:
564k calls, 121 of 147 total seconds. Two independent fixes: (1) only
compute the source segment where it's actually used (a laziness fix, no
new code needed — see the module diff), and (2) `_split_source_lines` +
`_cached_source_segment` here, which pre-split each file's source ONCE and
reuse it across every remaining call, for the call sites that legitimately
need the source text for every match (decorators, `with` items).

`_cached_source_segment` must behave IDENTICALLY to `ast.get_source_segment`
— these tests assert exact equivalence against the real stdlib function
across single-line, multi-line, and (the reason a naive `str.splitlines()`
reimplementation would have been WRONG) a source containing a form-feed
character, which the AST compiler's own tokenizer does NOT treat as a line
break but `str.splitlines()` does.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_audit_module():
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "test_tier_audit.py"
    spec = importlib.util.spec_from_file_location("_audit_tier_audit_segcache", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit_mod():
    return _load_audit_module()


def _first_node(source: str, kind) -> ast.AST:
    tree = ast.parse(source)
    return next(n for n in ast.walk(tree) if isinstance(n, kind))


def test_single_line_node_matches_stdlib(audit_mod) -> None:
    """Tier 1: FALSIFY — cached extraction must equal ast.get_source_segment
    for an ordinary single-line node."""
    source = "x = 1\ncall(a, b, c)\ny = 2\n"
    node = _first_node(source, ast.Call)
    lines = audit_mod._split_source_lines(source)

    cached = audit_mod._cached_source_segment(lines, node)
    stdlib = ast.get_source_segment(source, node)
    assert cached == stdlib


def test_multiline_node_matches_stdlib(audit_mod) -> None:
    """Tier 1: a node spanning multiple lines (decorator with a multi-line
    call) extracts identically to the stdlib function."""
    source = (
        "@patch(\n"
        "    'a.b',\n"
        "    'c.d',\n"
        ")\n"
        "def f():\n"
        "    pass\n"
    )
    tree = ast.parse(source)
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    dec = func.decorator_list[0]
    lines = audit_mod._split_source_lines(source)

    cached = audit_mod._cached_source_segment(lines, dec)
    stdlib = ast.get_source_segment(source, dec)
    assert cached == stdlib


def test_form_feed_in_source_does_not_shift_line_counting(audit_mod) -> None:
    """Tier 1: FALSIFY the exact hazard a naive str.splitlines() reimplementation
    would hit — a form-feed character inside a string literal is NOT a line
    break to the AST compiler's own tokenizer (unlike str.splitlines(), which
    treats \\x0c as a break). A node on a LATER real line must still resolve
    to the correct text, matching ast.get_source_segment exactly."""
    source = "x = 'a\x0cb'\ncall(1, 2, 3)\n"
    node = _first_node(source, ast.Call)
    lines = audit_mod._split_source_lines(source)

    cached = audit_mod._cached_source_segment(lines, node)
    stdlib = ast.get_source_segment(source, node)
    assert cached == stdlib
    assert cached == "call(1, 2, 3)"


def test_multibyte_utf8_column_offsets_match_stdlib(audit_mod) -> None:
    """Tier 1: node column offsets are byte offsets (UTF-8) — a line with
    multi-byte characters (Japanese text, common in this repo's comments)
    before the node must still slice at the correct byte boundary."""
    source = '"日本語のコメント"; call(1, 2)\n'
    node = _first_node(source, ast.Call)
    lines = audit_mod._split_source_lines(source)

    cached = audit_mod._cached_source_segment(lines, node)
    stdlib = ast.get_source_segment(source, node)
    assert cached == stdlib


def test_missing_location_info_returns_empty_string(audit_mod) -> None:
    """Tier 1: a node with no end_lineno/end_col_offset (e.g. a bare
    ast.AST() with no fields set) returns "" — matching this script's own
    `ast.get_source_segment(...) or ""` call convention, never None."""
    lines = ["x = 1\n"]
    bare = ast.AST()

    result = audit_mod._cached_source_segment(lines, bare)
    assert result == ""


def test_split_source_lines_preserves_line_endings(audit_mod) -> None:
    """Tier 1: split lines keep their trailing newline (keepends semantics)
    — required for the multi-line join in _cached_source_segment to
    reproduce the original source exactly. Matches ast._splitlines_no_ff's
    OWN behavior exactly, trailing empty-string entry included (verified
    directly against the real stdlib function, not assumed)."""
    source = "a = 1\nb = 2\nc = 3"
    lines = audit_mod._split_source_lines(source)
    stdlib_lines = ast._splitlines_no_ff(source)

    assert lines == stdlib_lines
    assert lines == ["a = 1\n", "b = 2\n", "c = 3", ""]


def test_a_full_test_tier_audit_run_is_unchanged_by_the_cache(audit_mod, tmp_path: Path) -> None:
    """Tier 1: end-to-end — auditing a real file with a mock-usage
    violation produces the SAME finding (message text included, which
    depends on _cached_source_segment) with the cache as it would from the
    original ast.get_source_segment call, on a case actually exercising
    the with-patch/MagicMock branches (not just the Import branch the
    laziness fix already covers)."""
    test_file = tmp_path / "test_example.py"
    test_file.write_text(
        '"""Tier 1: example."""\n'
        "from unittest.mock import MagicMock\n"
        "\n"
        "\n"
        "def test_something():\n"
        "    \"\"\"Tier 1: uses a mock.\"\"\"\n"
        "    m = MagicMock()\n"
        "    with patch('litellm.completion'):\n"
        "        pass\n",
        encoding="utf-8",
    )
    auditor = audit_mod.TestAuditor(check_rules={"mock"})
    report = auditor.audit_file(test_file)

    rules = sorted(f.rule for r in report.results for f in r.findings)
    assert rules == ["mock", "mock"]
    messages = [f.message for r in report.results for f in r.findings]
    assert any("MagicMock" in m for m in messages)
    assert any("patch" in m.lower() for m in messages)
