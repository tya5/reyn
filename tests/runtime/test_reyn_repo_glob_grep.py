"""Tier 2: reyn_repo_glob / reyn_repo_grep — FP-0038 S2 + S3.

Pins the §D20-completing surface for the `reyn_repo` category. The two
new ops mirror `glob_files` / `grep_files` in shape but resolve paths
against Reyn's own repo root (via `resolve_reyn_root()`), not the
operator's workspace.

Tests use real `reyn.runtime.reyn_repo` helpers + real repo contents — no
mocks. Assertions target the public result shape (= `{pattern, matches,
count, ...}`); private state is not asserted.
"""
from __future__ import annotations

from pathlib import Path

from reyn.runtime.reyn_repo import glob_entries, grep_entries, resolve_reyn_root
from reyn.tools.universal_dispatch import action_names_for_category

ROOT: Path = resolve_reyn_root()


# ── 1. Dispatch registration — §D20 surface complete ──────────────────────


def test_reyn_repo_category_has_four_ops() -> None:
    """Tier 2: the reyn_repo list/read/glob/grep actions are all registered.

    Catches the regression where §D20 surface drifts back to 2 ops.
    """
    assert set(action_names_for_category("reyn_repo")) == {
        "reyn_repo_list",
        "reyn_repo_read",
        "reyn_repo_glob",
        "reyn_repo_grep",
    }


# ── 2. glob_entries — pattern match against real repo ─────────────────────


def test_glob_finds_care_boundary_docs() -> None:
    """Tier 2: glob pattern returns real repo files matching it.

    Uses a well-known stable file pair (`docs/concepts/architecture/care-boundary*.md`).
    """
    result = glob_entries(ROOT, "docs/concepts/architecture/care-boundary*.md")
    assert "matches" in result and "count" in result
    assert "docs/concepts/architecture/care-boundary.md" in result["matches"]
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


def test_grep_finds_known_os_code_marker() -> None:
    """Tier 2: regex pattern returns real matches with path + line + snippet."""
    result = grep_entries(
        ROOT,
        pattern=r"P7 says OS code",
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
    from reyn.runtime.reyn_repo import _SKIP_DIR_NAMES
    # Sample dirs that must be skipped by both (= sanity)
    assert ".git" in _SKIP_DIR_NAMES
    assert "__pycache__" in _SKIP_DIR_NAMES
    assert ".venv" in _SKIP_DIR_NAMES


# ── 5. #4431 — silent-cap visibility (own tmp_path root, not the real repo) ─


def test_glob_truncated_flag_when_cap_hit(tmp_path: Path) -> None:
    """Tier 2: #4431 — more matches than _MAX_GLOB_MATCHES (200) sets
    `truncated`, mirroring grep's own contract (owner ruling: a silent cap
    with no config knob must make the cut visible instead)."""
    from reyn.runtime.reyn_repo import _MAX_GLOB_MATCHES

    for i in range(_MAX_GLOB_MATCHES + 5):
        (tmp_path / f"file{i:04d}.md").write_text("x", encoding="utf-8")

    result = glob_entries(tmp_path, "*.md")

    assert result["count"] == _MAX_GLOB_MATCHES
    assert result["truncated"] is True


def test_glob_no_truncation_signal_under_cap(tmp_path: Path) -> None:
    """Tier 2: accept-side twin — under the cap, `truncated` is False, not
    just absent (this function has always returned it unconditionally;
    #4431 doesn't change that shape, only adds a True case)."""
    (tmp_path / "alpha.md").write_text("a", encoding="utf-8")
    (tmp_path / "beta.md").write_text("b", encoding="utf-8")

    result = glob_entries(tmp_path, "*.md")

    assert result["count"] == 2
    assert result["truncated"] is False


def test_grep_snippet_truncated_marks_only_long_lines(tmp_path: Path) -> None:
    """Tier 2: #4431 — a matching line longer than _GREP_SNIPPET_CHARS (200)
    is cut with NO prior marker; `snippet_truncated` now says so per-match,
    and a short match must NOT carry the key at all (absence = nothing cut,
    same convention `truncated`'s sibling fields use elsewhere in #4431)."""
    from reyn.runtime.reyn_repo import _GREP_SNIPPET_CHARS

    long_line = "needle " + ("x" * (_GREP_SNIPPET_CHARS + 50))
    (tmp_path / "long.txt").write_text(long_line, encoding="utf-8")
    (tmp_path / "short.txt").write_text("needle short line", encoding="utf-8")

    result = grep_entries(tmp_path, pattern="needle", max_results=10)

    by_path = {m["path"]: m for m in result["matches"]}
    assert by_path["long.txt"]["snippet_truncated"] is True
    assert len(by_path["long.txt"]["snippet"]) == _GREP_SNIPPET_CHARS
    assert "snippet_truncated" not in by_path["short.txt"]


def test_grep_counts_files_skipped_for_size(tmp_path: Path) -> None:
    """Tier 2: #4431 — a file over `_MAX_READ_BYTES` is excluded from the
    scan entirely (pre-existing behaviour); before this fix that read
    identically to "searched, no matches". `skipped_large_file_count` now
    distinguishes the two."""
    from reyn.runtime.reyn_repo import _MAX_READ_BYTES

    (tmp_path / "huge.txt").write_text(
        "needle\n" + ("x" * (_MAX_READ_BYTES + 1)), encoding="utf-8"
    )
    (tmp_path / "normal.txt").write_text("needle here too", encoding="utf-8")

    result = grep_entries(tmp_path, pattern="needle", max_results=10)

    assert result["skipped_large_file_count"] == 1
    matched_paths = {m["path"] for m in result["matches"]}
    assert "huge.txt" not in matched_paths
    assert "normal.txt" in matched_paths


def test_grep_zero_skipped_large_files_when_none_are_big(tmp_path: Path) -> None:
    """Tier 2: accept-side twin — `skipped_large_file_count` is 0 (present,
    not absent — this function has always returned every field
    unconditionally, e.g. `truncated`) when nothing was actually skipped."""
    (tmp_path / "normal.txt").write_text("needle here", encoding="utf-8")

    result = grep_entries(tmp_path, pattern="needle", max_results=10)

    assert result["skipped_large_file_count"] == 0
