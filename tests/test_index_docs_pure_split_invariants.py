"""Tier 2 OS invariant tests for R-PURE-MODE-REDEFINE Class A split.

Guards the active two-step chain (post-FP-0042 Phase 2.1 / 2.2):
  - extract_and_split (mode: safe): glob enum, no file content read
  - write_chunks_with_lock (mode: unsafe, minimal): lock + content + jsonl write

Invariants tested:
  1. extract_and_split returns a list of path dicts without reading file content
     (no Path.read_text call trace — tested by verifying output contains only
     source_path keys and by running the function on a real temp directory).
  2. skill.md permissions.python declares extract_and_split as mode=safe and
     write_chunks_with_lock as mode=unsafe with an unsafe_reason annotation.

Testing policy (docs/deep-dives/contributing/testing.ja.md):
  - No mocks (real instances only)
  - No private-state assertions
  - No algorithm-level pins
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.compiler.loader import load_dsl_skill

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SKILL_MD_PATH = (
    Path(__file__).parent.parent
    / "src" / "reyn" / "stdlib" / "skills" / "index_docs" / "skill.md"
)
_SKILL_ROOT = _SKILL_MD_PATH.parent.parent.parent  # src/reyn/stdlib/
_CHUNKERS_PATH = _SKILL_MD_PATH.parent / "chunkers.py"


def _load_skill():
    return load_dsl_skill(_SKILL_MD_PATH, skill_root=_SKILL_ROOT)


# ---------------------------------------------------------------------------
# Tier 2: extract_and_split is pure (no file content read)
# ---------------------------------------------------------------------------


def test_extract_and_split_returns_path_list_without_content_read(tmp_path):
    """Tier 2: extract_and_split returns [{source_path}] dicts — no file content in output.

    Verifies that:
    - extract_and_split accepts a real glob pattern pointing at real files
    - Each returned item has exactly 'source_path' (no 'text', no 'content')
    - The function does not raise, even for an empty glob result

    This is a structural purity check: the output shape enforces that no file
    content was read (reading content would require placing it somewhere in the
    return value or side-effecting the artifact). An empty-glob case is also
    tested to confirm no error is raised for missing files.
    """
    from reyn.stdlib.skills.index_docs.chunkers_safe import extract_and_split

    # ── Case 1: real files ────────────────────────────────────────────────────
    (tmp_path / "a.md").write_text("hello world", encoding="utf-8")
    (tmp_path / "b.md").write_text("another doc", encoding="utf-8")

    glob_pattern = str(tmp_path / "*.md")
    artifact = {
        "type": "chunk_strategy",
        "data": {
            "path": glob_pattern,
            "source": "test_source",
            "boundary": "blank_line",
            "max_chunk_size_tokens": 600,
        },
    }

    result = extract_and_split(artifact)

    assert isinstance(result, list), f"Expected list, got {type(result)}"
    expected_paths = {str(tmp_path / "a.md"), str(tmp_path / "b.md")}
    returned_paths = {e["source_path"] for e in result if isinstance(e, dict) and "source_path" in e}
    assert returned_paths == expected_paths, (
        f"Expected one entry per created file, got paths {returned_paths}"
    )

    for entry in result:
        assert isinstance(entry, dict), f"Each entry must be a dict, got {type(entry)}"
        assert "source_path" in entry, f"Entry missing 'source_path': {entry}"
        # Structural purity check: no file content field should appear
        assert "text" not in entry, f"'text' field in entry — content was read: {entry}"
        assert "content" not in entry, f"'content' field in entry — content was read: {entry}"
        assert "excerpt" not in entry, f"'excerpt' field in entry — content was read: {entry}"
        # Each source_path must be an actual file path (string, non-empty)
        fp = entry["source_path"]
        assert isinstance(fp, str) and fp, f"source_path must be non-empty string, got {fp!r}"

    # ── Case 2: empty glob ────────────────────────────────────────────────────
    artifact_empty = {
        "type": "chunk_strategy",
        "data": {
            "path": str(tmp_path / "nonexistent_*.txt"),
            "source": "test_source",
            "boundary": "blank_line",
        },
    }
    result_empty = extract_and_split(artifact_empty)
    assert result_empty == [], f"Empty glob must return [], got {result_empty}"


# ---------------------------------------------------------------------------
# Tier 2: skill.md permissions.python declares safe + unsafe correctly
# ---------------------------------------------------------------------------


def test_index_docs_skill_permissions_python_all_steps_safe():
    """Tier 2: skill.md permissions.python has every active step as mode=safe.

    Post-FP-0042 Phase 2.8 (2026-05-23) the active two-step
    postprocessor chain (extract_and_split + write_chunks_with_lock)
    plus the two preprocessor steps all run mode: safe. The deprecated
    ``apply_strategy`` was retired in the same phase, closing the last
    grandfathered exemption.
    """
    skill = _load_skill()
    fn_modes = {p.function: p.mode for p in skill.permissions.python}

    assert fn_modes.get("extract_and_split") == "safe", (
        f"extract_and_split must be mode=safe, got {fn_modes.get('extract_and_split')!r}."
    )
    assert fn_modes.get("write_chunks_with_lock") == "safe", (
        f"write_chunks_with_lock must be mode=safe post-FP-0042, "
        f"got {fn_modes.get('write_chunks_with_lock')!r}."
    )
    assert "apply_strategy" not in fn_modes, (
        "apply_strategy was retired in FP-0042 Phase 2.8 — its skill.md "
        "permission entry must stay removed."
    )
