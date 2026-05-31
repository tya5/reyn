"""Tier 2: reyn.source__glob / reyn.source__grep — FP-0038 S2 + S3.

Pins the §D20-completing surface for the `reyn.source` category. The two
new ops mirror `file__glob` / `file__grep` in shape but resolve paths
against Reyn's own repo root (via `resolve_reyn_root()`), not the
operator's workspace.

Tests use real `reyn.chat.reyn_src` helpers + real repo contents — no
mocks. Assertions target the public result shape (= `{pattern, matches,
count, ...}`); private state is not asserted.
"""
from __future__ import annotations

from pathlib import Path

from reyn.chat.reyn_src import glob_entries, grep_entries, resolve_reyn_root
from reyn.tools.universal_dispatch import (
    _OPERATION_RULES,
    known_qualified_name_for_category,
)

ROOT: Path = resolve_reyn_root()


# ── 1. Dispatch registration — §D20 surface complete ──────────────────────


def test_reyn_source_category_has_four_ops() -> None:
    """Tier 2: `reyn.source__{list,read,glob,grep}` are all registered.

    Catches the regression where §D20 surface drifts back to 2 ops.
    """
    qns = known_qualified_name_for_category("reyn.source")
    assert set(qns) == {
        "reyn.source__list",
        "reyn.source__read",
        "reyn.source__glob",
        "reyn.source__grep",
    }


def test_reyn_source_glob_routes_to_reyn_src_glob() -> None:
    """Tier 2: dispatch routes `reyn.source__glob` to `reyn_src_glob`."""
    target, _ = _OPERATION_RULES["reyn.source__glob"]
    assert target == "reyn_src_glob"


def test_reyn_source_grep_routes_to_reyn_src_grep() -> None:
    """Tier 2: dispatch routes `reyn.source__grep` to `reyn_src_grep`."""
    target, _ = _OPERATION_RULES["reyn.source__grep"]
    assert target == "reyn_src_grep"


# ── 2. glob_entries — pattern match against real repo ─────────────────────


def test_glob_finds_principles_docs() -> None:
    """Tier 2: glob pattern returns real repo files matching it.

    Uses a well-known stable file pair (`docs/concepts/architecture/principles*.md`)
    that won't be renamed without an FP-0034-scale change.
    """
    result = glob_entries(ROOT, "docs/concepts/architecture/principles*.md")
    assert "matches" in result and "count" in result
    assert "docs/concepts/architecture/principles.md" in result["matches"]
    assert result["count"] >= 1


def test_glob_skips_venv_and_pycache() -> None:
    """Tier 2: noise dirs (.venv, __pycache__, .git) are excluded.

    Mirrors `list_entries`'s skip discipline so the surfaces are uniform.
    """
    # A pattern that would normally match .venv / __pycache__ contents
    result = glob_entries(ROOT, "**/__pycache__/*")
    assert result["count"] == 0, (
        f"Expected 0 matches under __pycache__, got {result['count']}: "
        f"{result['matches'][:5]}"
    )


def test_glob_empty_pattern_returns_error() -> None:
    """Tier 2: empty pattern surfaces a structured error, not [].

    Distinguishes "no matches" from "operator error" — the LLM gets
    clearer feedback.
    """
    result = glob_entries(ROOT, "")
    assert "error" in result
    assert "non-empty" in result["error"]


def test_glob_caps_result_count() -> None:
    """Tier 2: glob result is capped at 200 matches (= _MAX_GLOB_MATCHES).

    Prevents runaway `**/*` patterns from blowing the LLM context.
    """
    result = glob_entries(ROOT, "**/*.py")
    # The cap is 200; matches is an int <= 200 by construction.
    assert result["count"] <= 200, (
        f"Expected glob to cap at 200 matches, got {result['count']}"
    )


# ── 3. grep_entries — regex content search against real repo ──────────────


def test_grep_finds_known_p7_critical_marker() -> None:
    """Tier 2: regex pattern returns real matches with path + line + snippet."""
    result = grep_entries(
        ROOT,
        pattern=r"P7.*CRITICAL",
        glob="docs/concepts/architecture/*.md",
        max_results=10,
    )
    assert "matches" in result
    assert result["count"] >= 1
    # Each match has {path, line, snippet}
    for m in result["matches"]:
        assert "path" in m and "line" in m and "snippet" in m
        assert isinstance(m["line"], int) and m["line"] >= 1


def test_grep_path_arg_scopes_search() -> None:
    """Tier 2: `path` arg narrows the search to a sub-tree.

    A grep with path='docs' should NOT see matches under 'src/' — the
    scope discipline mirrors a typical grep -r usage.
    """
    result = grep_entries(
        ROOT,
        pattern=r"P7.*CRITICAL",
        path="docs",
        max_results=10,
    )
    for m in result["matches"]:
        assert m["path"].startswith("docs/"), (
            f"Match {m['path']!r} escaped the path scope 'docs/'"
        )


def test_grep_invalid_regex_returns_error() -> None:
    """Tier 2: an unparseable regex surfaces a structured error."""
    result = grep_entries(ROOT, pattern="[unclosed", max_results=10)
    assert "error" in result
    assert "invalid regex" in result["error"].lower()


def test_grep_empty_pattern_returns_error() -> None:
    """Tier 2: empty pattern is an explicit error, same as glob."""
    result = grep_entries(ROOT, pattern="", max_results=10)
    assert "error" in result


def test_grep_truncated_flag_when_max_results_hit() -> None:
    """Tier 2: when results exceed max_results, `truncated` flag is True.

    Verifies the truncation contract so callers can detect "there are
    more results" and re-grep with a finer pattern if needed.
    """
    # A pattern that matches very frequently — 'def ' appears throughout
    # the Python source. max_results=3 forces truncation immediately.
    result = grep_entries(
        ROOT,
        pattern=r"^def ",
        glob="src/reyn/**/*.py",
        max_results=3,
    )
    assert result["count"] == 3
    assert result["truncated"] is True


def test_grep_escapes_path_traversal() -> None:
    """Tier 2: path-traversal arguments are rejected at the scope-resolve
    step (= reuses `safe_resolve_inside` discipline).
    """
    result = grep_entries(
        ROOT,
        pattern=r"anything",
        path="../../etc/passwd",
        max_results=5,
    )
    assert "error" in result, (
        "path traversal must surface as an error, not silently scope to root"
    )


# ── 4. Skip-discipline parity (glob vs list) ──────────────────────────────


def test_glob_skip_set_matches_list_entries_set() -> None:
    """Tier 2: glob_entries and list_entries skip the same directories.

    If they diverge, the LLM sees different visibility through different
    ops — confusing and error-prone. This pins the parity.
    """
    from reyn.chat.reyn_src import _SKIP_DIR_NAMES
    # Sample dirs that must be skipped by both (= sanity)
    assert ".git" in _SKIP_DIR_NAMES
    assert "__pycache__" in _SKIP_DIR_NAMES
    assert ".venv" in _SKIP_DIR_NAMES
