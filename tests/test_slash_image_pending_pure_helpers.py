"""Tier 2: pure helpers in slash/image.py and slash/pending.py.

Covers four stateless helpers with no session or filesystem dependency:

slash/image.py
  - ``_mime_for_path``   — extension → MIME type lookup
  - ``_file_size_human`` — byte count formatter (B / KB / MB)

slash/pending.py
  - ``_render_list``           — stalled-op list renderer
  - ``_render_needs_attention`` — stuck-skill tail-section renderer
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.slash.image import _file_size_human, _mime_for_path
from reyn.interfaces.slash.pending import _render_list, _render_needs_attention

# ---------------------------------------------------------------------------
# _mime_for_path
# ---------------------------------------------------------------------------


def test_mime_for_path_png() -> None:
    """Tier 2: .png suffix returns image/png."""
    assert _mime_for_path(Path("photo.png")) == "image/png"


def test_mime_for_path_jpg() -> None:
    """Tier 2: .jpg suffix returns image/jpeg."""
    assert _mime_for_path(Path("photo.jpg")) == "image/jpeg"


def test_mime_for_path_jpeg() -> None:
    """Tier 2: .jpeg suffix also returns image/jpeg."""
    assert _mime_for_path(Path("photo.jpeg")) == "image/jpeg"


def test_mime_for_path_gif() -> None:
    """Tier 2: .gif suffix returns image/gif."""
    assert _mime_for_path(Path("anim.gif")) == "image/gif"


def test_mime_for_path_webp() -> None:
    """Tier 2: .webp suffix returns image/webp."""
    assert _mime_for_path(Path("modern.webp")) == "image/webp"


def test_mime_for_path_svg() -> None:
    """Tier 2: .svg suffix returns image/svg+xml."""
    assert _mime_for_path(Path("logo.svg")) == "image/svg+xml"


def test_mime_for_path_uppercase_extension() -> None:
    """Tier 2: extension matching is case-insensitive (.PNG → image/png)."""
    assert _mime_for_path(Path("PHOTO.PNG")) == "image/png"


def test_mime_for_path_unknown_extension() -> None:
    """Tier 2: unknown extension returns None."""
    assert _mime_for_path(Path("document.pdf")) is None


def test_mime_for_path_no_extension() -> None:
    """Tier 2: file with no extension returns None."""
    assert _mime_for_path(Path("README")) is None


# ---------------------------------------------------------------------------
# _file_size_human
# ---------------------------------------------------------------------------


def test_file_size_human_bytes_zero() -> None:
    """Tier 2: 0 bytes renders as '0 bytes'."""
    assert _file_size_human(0) == "0 bytes"


def test_file_size_human_bytes_small() -> None:
    """Tier 2: values under 1 000 render with the 'bytes' suffix."""
    assert _file_size_human(512) == "512 bytes"


def test_file_size_human_bytes_boundary() -> None:
    """Tier 2: 999 still renders as bytes (threshold is 1 000)."""
    assert _file_size_human(999) == "999 bytes"


def test_file_size_human_kilobytes() -> None:
    """Tier 2: 1 000 renders as 1.0KB."""
    assert _file_size_human(1_000) == "1.0KB"


def test_file_size_human_kilobytes_fractional() -> None:
    """Tier 2: 1 500 renders as 1.5KB (1 decimal place)."""
    assert _file_size_human(1_500) == "1.5KB"


def test_file_size_human_megabytes() -> None:
    """Tier 2: 1 000 000 renders as 1.0MB."""
    assert _file_size_human(1_000_000) == "1.0MB"


def test_file_size_human_megabytes_fractional() -> None:
    """Tier 2: 2 500 000 renders as 2.5MB (1 decimal place)."""
    assert _file_size_human(2_500_000) == "2.5MB"


# ---------------------------------------------------------------------------
# _render_list
# ---------------------------------------------------------------------------


def test_render_list_empty() -> None:
    """Tier 2: empty list returns the canonical 'no pending operations' message."""
    assert _render_list([]) == "no pending operations"


def test_render_list_singular_count() -> None:
    """Tier 2: 1 entry produces 'pending operation' (not 'operations')."""
    op = SimpleNamespace(kind="ask_user", id="abc12345", origin_channel_id="ch1", summary="")
    result = _render_list([op])
    assert "1 pending operation:" in result
    assert "operations" not in result


def test_render_list_plural_count() -> None:
    """Tier 2: 2 entries produce 'pending operations'."""
    ops = [
        SimpleNamespace(kind="ask_user", id="abc12345", origin_channel_id="ch1", summary=""),
        SimpleNamespace(kind="ask_user", id="def67890", origin_channel_id="ch2", summary=""),
    ]
    result = _render_list(ops)
    assert "2 pending operations:" in result


def test_render_list_includes_kind_and_id() -> None:
    """Tier 2: kind and first 8 chars of id appear in each entry row."""
    op = SimpleNamespace(kind="approval", id="deadbeef1234", origin_channel_id="main", summary="")
    result = _render_list([op])
    assert "approval" in result
    assert "deadbeef" in result  # first 8 chars of id


def test_render_list_includes_summary_line() -> None:
    """Tier 2: non-empty summary produces an indented arrow line."""
    op = SimpleNamespace(
        kind="ask_user", id="aaaa0000", origin_channel_id="ch1",
        summary="Do you want to continue?",
    )
    result = _render_list([op])
    assert "Do you want to continue?" in result
    assert "↳" in result


def test_render_list_no_summary_line_when_empty() -> None:
    """Tier 2: empty summary omits the arrow line."""
    op = SimpleNamespace(kind="ask_user", id="bbbb1111", origin_channel_id="ch1", summary="")
    result = _render_list([op])
    assert "↳" not in result


def test_render_list_accepts_dict_ops() -> None:
    """Tier 2: dict-shaped ops (legacy / test path) are handled gracefully."""
    op = {"kind": "approval", "id": "cccc2222", "origin_channel_id": "web", "summary": ""}
    result = _render_list([op])
    assert "approval" in result
    assert "cccc2222" in result


# ---------------------------------------------------------------------------
# _render_needs_attention
# ---------------------------------------------------------------------------


def test_render_needs_attention_empty_dict() -> None:
    """Tier 2: empty summary dict returns empty string."""
    assert _render_needs_attention({}) == ""


def test_render_needs_attention_no_stuck_skills() -> None:
    """Tier 2: stuck_skills=[] returns empty string (no spurious header)."""
    assert _render_needs_attention({"stuck_skills": []}) == ""


def test_render_needs_attention_one_stuck_skill() -> None:
    """Tier 2: one stuck skill produces the 'needs attention:' header + 1 line."""
    summary = {"stuck_skills": [
        {"skill_name": "research", "run_id": "run-01", "stuck_at": "phase_b"},
    ]}
    result = _render_needs_attention(summary)
    assert result.startswith("needs attention:")
    assert "research" in result
    assert "run-01" in result
    assert "phase_b" in result
    assert result.count("⊘") == 1


def test_render_needs_attention_two_stuck_skills() -> None:
    """Tier 2: two stuck skills produce two ⊘ lines."""
    summary = {"stuck_skills": [
        {"skill_name": "alpha", "run_id": "r1", "stuck_at": "s1"},
        {"skill_name": "beta", "run_id": "r2", "stuck_at": "s2"},
    ]}
    result = _render_needs_attention(summary)
    assert result.count("⊘") == 2
    assert "alpha" in result
    assert "beta" in result


def test_render_needs_attention_missing_fields_use_question_mark() -> None:
    """Tier 2: missing skill_name/run_id/stuck_at default to '?'."""
    summary = {"stuck_skills": [{}]}
    result = _render_needs_attention(summary)
    assert "?" in result
    assert "needs attention:" in result
