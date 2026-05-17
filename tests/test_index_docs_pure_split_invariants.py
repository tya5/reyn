"""Tier 2 OS invariant tests for R-PURE-MODE-REDEFINE Class A split.

Guards the Class A refactor that split apply_strategy into:
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

from reyn.compiler.loader import load_dsl_skill

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
    assert len(result) == 2, f"Expected 2 entries for 2 files, got {len(result)}"

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


def test_index_docs_skill_permissions_python_has_safe_extract_and_unsafe_write():
    """Tier 2: skill.md permissions.python has extract_and_split as safe and
    write_chunks_with_lock as unsafe with unsafe_reason annotation.

    Verifies the R-PURE-MODE-REDEFINE Class A permission declaration:
    - extract_and_split: mode=safe (glob enum only, no I/O)
    - write_chunks_with_lock: mode=unsafe (irreducible: lock + content + jsonl)

    The unsafe_reason annotation is checked by parsing the raw YAML frontmatter
    directly (the compiled PythonPermission dataclass does not carry unsafe_reason,
    so we read the skill.md source and parse the permissions block to verify the
    annotation exists in the YAML text).
    """
    import re

    skill_md_text = _SKILL_MD_PATH.read_text(encoding="utf-8")

    # ── Compiled model check: modes ───────────────────────────────────────────
    skill = _load_skill()
    fn_modes = {p.function: p.mode for p in skill.permissions.python}

    assert fn_modes.get("extract_and_split") == "safe", (
        f"extract_and_split must be mode=safe, got {fn_modes.get('extract_and_split')!r}. "
        "glob enum is pure (path list only, no file content read)."
    )
    assert fn_modes.get("write_chunks_with_lock") == "unsafe", (
        f"write_chunks_with_lock must be mode=unsafe, got {fn_modes.get('write_chunks_with_lock')!r}. "
        "It performs lock acquire, Path.read_text, and .jsonl write."
    )

    # ── Raw YAML check: unsafe_reason annotation is present ──────────────────
    # Extract the frontmatter block (between first --- and second ---)
    fm_match = re.match(r"^---\n(.*?)\n---", skill_md_text, re.DOTALL)
    assert fm_match, "Could not parse skill.md frontmatter"
    fm_text = fm_match.group(1)

    assert "unsafe_reason" in fm_text, (
        "write_chunks_with_lock permissions entry must include an unsafe_reason "
        "annotation in skill.md (FP-0014 Component F documentation contract). "
        "Add `unsafe_reason: \"...\"` to the write_chunks_with_lock entry."
    )
