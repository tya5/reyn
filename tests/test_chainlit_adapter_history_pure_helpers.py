"""Tier 2: pure helpers in chainlit_app/adapter.py and chainlit_app/history.py.

  ``adapter._truncate_one_line(s, limit)``    — whitespace-collapse + char-cap
  ``adapter._preview_tool_content(v, limit)`` — compact k=v or scalar preview
  ``history._flatten_content(content)``       — str/list-of-parts → plain text
  ``history._truncation_marker(omitted)``     — system HistoryEntry stub
"""
from __future__ import annotations

from reyn.interfaces.chainlit_app.adapter import _preview_tool_content, _truncate_one_line
from reyn.interfaces.chainlit_app.history import HistoryEntry, _flatten_content, _truncation_marker

# ---------------------------------------------------------------------------
# _truncate_one_line
# ---------------------------------------------------------------------------


def test_truncate_one_line_short_unchanged() -> None:
    """Tier 2: string shorter than limit is returned unchanged."""
    assert _truncate_one_line("hello", 10) == "hello"


def test_truncate_one_line_exactly_at_limit_unchanged() -> None:
    """Tier 2: string exactly at limit is returned unchanged."""
    assert _truncate_one_line("abcde", 5) == "abcde"


def test_truncate_one_line_exceeds_limit_gets_ellipsis() -> None:
    """Tier 2: string exceeding limit is capped with trailing ellipsis."""
    result = _truncate_one_line("abcdefgh", 5)
    assert result == "abcd…"


def test_truncate_one_line_collapses_internal_whitespace() -> None:
    """Tier 2: internal whitespace (spaces/newlines) collapses to single spaces."""
    assert _truncate_one_line("hello\n  world", 50) == "hello world"


def test_truncate_one_line_collapses_then_truncates() -> None:
    """Tier 2: whitespace collapse happens before length cap."""
    # 'a  b  c' → 'a b c' (5 chars) → fits limit=5
    assert _truncate_one_line("a  b  c", 5) == "a b c"


# ---------------------------------------------------------------------------
# _preview_tool_content
# ---------------------------------------------------------------------------


def test_preview_tool_content_none_returns_empty() -> None:
    """Tier 2: None value produces empty string."""
    assert _preview_tool_content(None, 100) == ""


def test_preview_tool_content_empty_dict_returns_empty() -> None:
    """Tier 2: empty dict produces empty string."""
    assert _preview_tool_content({}, 100) == ""


def test_preview_tool_content_empty_string_returns_empty() -> None:
    """Tier 2: empty string produces empty string."""
    assert _preview_tool_content("", 100) == ""


def test_preview_tool_content_dict_renders_kv_pairs() -> None:
    """Tier 2: dict renders as comma-separated k=v pairs."""
    result = _preview_tool_content({"path": "/foo"}, 100)
    assert "path=" in result
    assert "/foo" in result


def test_preview_tool_content_dict_multiple_keys() -> None:
    """Tier 2: dict with multiple keys renders all pairs."""
    result = _preview_tool_content({"a": "1", "b": "2"}, 200)
    assert "a=1" in result
    assert "b=2" in result


def test_preview_tool_content_scalar_string_returned() -> None:
    """Tier 2: plain string value is returned directly (truncated at limit)."""
    result = _preview_tool_content("hello world", 100)
    assert "hello world" in result


def test_preview_tool_content_scalar_int_uses_repr() -> None:
    """Tier 2: non-string scalar uses repr and is returned as string."""
    result = _preview_tool_content(42, 100)
    assert "42" in result


def test_preview_tool_content_truncated_at_limit() -> None:
    """Tier 2: a string longer than limit is truncated to limit-1 chars + ellipsis."""
    result = _preview_tool_content("x" * 200, 10)
    assert result == "x" * 9 + "…"


# ---------------------------------------------------------------------------
# _flatten_content
# ---------------------------------------------------------------------------


def test_flatten_content_string_returned_verbatim() -> None:
    """Tier 2: plain string content is returned unchanged."""
    assert _flatten_content("hello") == "hello"


def test_flatten_content_non_str_non_list_returns_empty() -> None:
    """Tier 2: content that is neither str nor list produces empty string."""
    assert _flatten_content(42) == ""  # type: ignore[arg-type]


def test_flatten_content_list_of_text_parts_joined() -> None:
    """Tier 2: text parts in a list are joined by newline."""
    parts = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    assert _flatten_content(parts) == "hello\nworld"


def test_flatten_content_image_path_part_produces_marker() -> None:
    """Tier 2: image part with a path produces '[image: filename]' marker."""
    parts = [{"type": "image", "path": "/uploads/photo.png"}]
    assert _flatten_content(parts) == "[image: photo.png]"


def test_flatten_content_image_url_part_produces_marker() -> None:
    """Tier 2: image_url part produces generic '[image]' marker."""
    parts = [{"type": "image_url", "url": "https://example.com/img.png"}]
    assert _flatten_content(parts) == "[image]"


def test_flatten_content_non_dict_list_entries_skipped() -> None:
    """Tier 2: non-dict entries in the list are silently ignored."""
    parts = ["not-a-dict", {"type": "text", "text": "ok"}]
    assert _flatten_content(parts) == "ok"


def test_flatten_content_empty_list_returns_empty() -> None:
    """Tier 2: empty list of parts returns empty string."""
    assert _flatten_content([]) == ""


# ---------------------------------------------------------------------------
# _truncation_marker
# ---------------------------------------------------------------------------


def test_truncation_marker_author_is_system() -> None:
    """Tier 2: truncation marker has author='system'."""
    entry = _truncation_marker(5)
    assert isinstance(entry, HistoryEntry)
    assert entry.author == "system"


def test_truncation_marker_content_contains_count() -> None:
    """Tier 2: truncation marker content mentions the omitted count."""
    entry = _truncation_marker(12)
    assert "12" in entry.content
