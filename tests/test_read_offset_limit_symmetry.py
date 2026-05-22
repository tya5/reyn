"""Tier 2: read-side ``offset`` / ``limit`` symmetry across the three
"read one entry" surfaces — ``read_file``, ``reyn_src_read``,
``read_memory_body``.

Before this contract: only ``read_file``'s underlying ``FileIROp`` slice
support existed, hidden from the router schema; ``reyn_src_read`` had a
hard 256-KB error path with no slice escape; ``read_memory_body`` had no
slice at all. That meant the LLM either got the full content (potentially
blowing up its context window) or nothing.

These tests pin the new symmetric behaviour:

- All three accept optional ``offset`` (0-indexed line number) and
  ``limit`` (line count).
- Slicing is line-based and operates on the content the LLM would
  otherwise see (= after frontmatter strip for memory entries).
- ``offset`` past EOF returns an empty body (= not an error).
- ``reyn_src_read``: when slice args are present the 256-KB byte cap is
  bypassed (= a giant file is partially readable).
- Argument-omitted shape is unchanged (= existing callers still get the
  full body).

Tier 2 because these are OS-level read-surface invariants the chat
router and phase Control IR depend on; a regression would alter
LLM-observable behaviour without notice.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.chat.reyn_src import read_text, resolve_reyn_root, safe_resolve_inside

# ── reyn_src_read slice semantics ────────────────────────────────────────────


def test_reyn_src_read_full_body_when_no_slice_args(tmp_path: Path) -> None:
    """Tier 2: omitting ``offset`` / ``limit`` reads the whole file —
    backwards-compatible with prior callers."""
    f = tmp_path / "small.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    result = read_text(f, "small.txt")
    assert result["content"] == "alpha\nbeta\ngamma\n"


def test_reyn_src_read_offset_only_skips_leading_lines(tmp_path: Path) -> None:
    """Tier 2: ``offset=N`` starts at line N (0-indexed); ``limit`` omitted
    means "read to end-of-file"."""
    f = tmp_path / "five.txt"
    f.write_text("L0\nL1\nL2\nL3\nL4\n", encoding="utf-8")
    result = read_text(f, "five.txt", offset=2)
    assert result["content"] == "L2\nL3\nL4\n"


def test_reyn_src_read_limit_only_takes_first_n(tmp_path: Path) -> None:
    """Tier 2: ``limit=N`` without ``offset`` takes the first N lines."""
    f = tmp_path / "five.txt"
    f.write_text("L0\nL1\nL2\nL3\nL4\n", encoding="utf-8")
    result = read_text(f, "five.txt", limit=2)
    assert result["content"] == "L0\nL1\n"


def test_reyn_src_read_offset_and_limit_window(tmp_path: Path) -> None:
    """Tier 2: combining ``offset`` + ``limit`` materialises the
    ``[offset, offset+limit)`` line window."""
    f = tmp_path / "five.txt"
    f.write_text("L0\nL1\nL2\nL3\nL4\n", encoding="utf-8")
    result = read_text(f, "five.txt", offset=1, limit=2)
    assert result["content"] == "L1\nL2\n"


def test_reyn_src_read_offset_past_eof_is_empty(tmp_path: Path) -> None:
    """Tier 2: ``offset`` greater than the line count returns empty
    content — never an error. The LLM can detect "out of range" without
    a structured failure path."""
    f = tmp_path / "three.txt"
    f.write_text("L0\nL1\nL2\n", encoding="utf-8")
    result = read_text(f, "three.txt", offset=99)
    assert result.get("error") is None
    assert result["content"] == ""


def test_reyn_src_read_slice_bypasses_byte_cap(tmp_path: Path) -> None:
    """Tier 2: when slice args are present, the 256-KB hard cap is bypassed.

    Without slice args a >256-KB file errors with "larger than the cap";
    with ``offset`` / ``limit`` the file is line-streamed and only the
    requested slice is materialised — so the LLM can inspect a giant
    log or generated artifact piecewise instead of receiving an error.
    """
    big = tmp_path / "big.txt"
    big.write_text("\n".join(f"line {i}" for i in range(50_000)) + "\n", encoding="utf-8")
    assert big.stat().st_size > 300 * 1024  # safely over the 256-KB cap

    # Without slice: cap error (existing behaviour).
    full_attempt = read_text(big, "big.txt")
    assert "error" in full_attempt
    assert "cap" in full_attempt["error"].lower() or "larger" in full_attempt["error"].lower()

    # With slice: returns the requested window only, no error.
    sliced = read_text(big, "big.txt", offset=10, limit=3)
    assert "error" not in sliced
    assert sliced["content"] == "line 10\nline 11\nline 12\n"


def test_reyn_src_read_slice_on_actual_repo_file() -> None:
    """Tier 2: end-to-end slice against a real file in the Reyn repo
    (= README.md). Confirms ``resolve_reyn_root`` + ``safe_resolve_inside``
    + sliced ``read_text`` compose correctly for the LLM-visible path."""
    resolve_reyn_root.cache_clear()
    root = resolve_reyn_root()
    target = safe_resolve_inside(root, "README.md")
    result = read_text(target, "README.md", offset=0, limit=3)
    assert "error" not in result
    # Three lines of README; exact content varies but line count is fixed.
    line_count = result["content"].count("\n")
    assert line_count == 3, f"expected exactly 3 lines, got {line_count}: {result['content']!r}"


# ── read_file underlying slice — verifies FileIROp accepts offset/limit ────


def test_file_irop_accepts_offset_and_limit() -> None:
    """Tier 2: ``FileIROp`` exposes ``offset`` / ``limit`` fields so the
    router-side ``_handle_read`` adapter can forward the schema's args
    without translation. If these fields disappear from FileIROp, the
    router schema (= public LLM contract) cannot be honoured.
    """
    from reyn.schemas.models import FileIROp

    op = FileIROp(kind="file", op="read", path="x", offset=5, limit=10)
    assert op.offset == 5
    assert op.limit == 10
    op_default = FileIROp(kind="file", op="read", path="x")
    assert op_default.offset is None
    assert op_default.limit is None


# ── read_memory_body slice semantics ─────────────────────────────────────────


def test_memory_slice_body_lines_no_args_returns_unchanged() -> None:
    """Tier 2: the memory body slicer is a no-op when neither offset nor
    limit is provided — backwards-compatible default."""
    from reyn.tools.memory import _slice_body_lines

    body = "line0\nline1\nline2\n"
    assert _slice_body_lines(body, None, None) == body


def test_memory_slice_body_lines_offset_only() -> None:
    """Tier 2: offset alone skips leading lines, returns the rest."""
    from reyn.tools.memory import _slice_body_lines

    body = "line0\nline1\nline2\nline3\n"
    assert _slice_body_lines(body, 2, None) == "line2\nline3\n"


def test_memory_slice_body_lines_limit_only() -> None:
    """Tier 2: limit alone caps the number of leading lines."""
    from reyn.tools.memory import _slice_body_lines

    body = "line0\nline1\nline2\nline3\n"
    assert _slice_body_lines(body, None, 2) == "line0\nline1\n"


def test_memory_slice_body_lines_window() -> None:
    """Tier 2: offset + limit returns the ``[offset, offset+limit)`` window."""
    from reyn.tools.memory import _slice_body_lines

    body = "line0\nline1\nline2\nline3\n"
    assert _slice_body_lines(body, 1, 2) == "line1\nline2\n"


def test_memory_slice_body_lines_past_eof_is_empty() -> None:
    """Tier 2: offset past the body's last line returns empty string,
    matching the reyn_src_read past-EOF semantic."""
    from reyn.tools.memory import _slice_body_lines

    body = "line0\nline1\n"
    assert _slice_body_lines(body, 99, None) == ""


# ── cross-surface symmetry (= the contract this PR enforces) ────────────────


def test_all_four_read_schemas_share_offset_limit_shape() -> None:
    """Tier 2: all four "read one entry" LLM-callable surfaces —
    ``read_file``, ``reyn_src_read``, ``read_memory_body``, and
    ``read_tool_result`` — expose the same ``offset`` / ``limit``
    line-slice arguments in their LLM-visible parameter schemas, with
    identical types (integer) and optional status (not in ``required``).

    This is the symmetry contract — adding a slice arg to one surface and
    not the others would silently regress to the pre-PR asymmetry. The
    four surfaces were established by PR #409 (= read_file /
    reyn_src_read / read_memory_body) and completed by #385 Q7
    adoption (= read_tool_result).
    """
    from reyn.tools.file import _READ_FILE_PARAMETERS
    from reyn.tools.memory import _READ_MEMORY_BODY_PARAMETERS
    from reyn.tools.read_tool_result import _READ_TOOL_RESULT_PARAMETERS
    from reyn.tools.reyn_src import _REYN_SRC_READ_PARAMETERS

    for label, schema in [
        ("read_file", _READ_FILE_PARAMETERS),
        ("reyn_src_read", _REYN_SRC_READ_PARAMETERS),
        ("read_memory_body", _READ_MEMORY_BODY_PARAMETERS),
        ("read_tool_result", _READ_TOOL_RESULT_PARAMETERS),
    ]:
        props = schema["properties"]
        required = schema.get("required", [])
        assert "offset" in props, f"{label} missing offset"
        assert "limit" in props, f"{label} missing limit"
        assert props["offset"]["type"] == "integer", f"{label} offset wrong type"
        assert props["limit"]["type"] == "integer", f"{label} limit wrong type"
        assert "offset" not in required, f"{label} offset must be optional"
        assert "limit" not in required, f"{label} limit must be optional"
