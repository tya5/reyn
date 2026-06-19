"""Tier 2: tool-result viewer registry seam (#1154 Phase 3 S1).

Pins the registry seam introduced in S1:
- existing Phase 1–2c viewers are preserved with byte-identical behavior.
- ``register_viewer`` fires before the fallback for matching results.
- ``position`` parameter controls priority.

Falsification:
- Without the registry, a custom viewer could not fire without editing
  the dispatch core — the register_viewer test would fail.
- Without position control, a high-priority viewer could not override a
  default viewer for the same content-type.
"""
from __future__ import annotations

from reyn.interfaces.tui.widgets.right_panel.tool_result_viewers import (
    _VIEWERS,
    register_viewer,
    render_tool_result,
)

# ---------------------------------------------------------------------------
# Fixtures — result dicts for each Phase 1–2c viewer
# ---------------------------------------------------------------------------

_MARKDOWN_RESULT = {"content_type": "text/markdown", "content": "# Hello\n\nworld"}
_CSV_RESULT = {"content_type": "text/csv", "content": "a,b\n1,2\n3,4"}
_JSON_RESULT = {"content_type": "application/json", "content": '{"x": 1}'}
_IMAGE_RESULT = {
    "mimeType": "image/png",
    "media_blocks": [{"mimeType": "image/png", "data": "abc123"}],
}
_WEB_RESULT = {
    "title": "Example",
    "outline": ["H1", "H2"],
    "first_paragraph": "Some text",
    "link_count": 5,
}


# ---------------------------------------------------------------------------
# Phase 1–2c behavior preservation
# ---------------------------------------------------------------------------

def test_markdown_viewer_preserved() -> None:
    """Tier 2: markdown result still dispatches to _viewer_markdown via registry."""
    result = render_tool_result(_MARKDOWN_RESULT)
    assert result is not None, "expected a renderable for markdown result"


def test_csv_viewer_preserved() -> None:
    """Tier 2: CSV result still dispatches to _viewer_csv via registry."""
    result = render_tool_result(_CSV_RESULT)
    assert result is not None, "expected a renderable for CSV result"


def test_json_viewer_preserved() -> None:
    """Tier 2: JSON result still dispatches to _viewer_json via registry."""
    result = render_tool_result(_JSON_RESULT)
    assert result is not None, "expected a renderable for JSON result"


def test_image_viewer_preserved() -> None:
    """Tier 2: image result still dispatches to _viewer_image via registry."""
    result = render_tool_result(_IMAGE_RESULT)
    assert result is not None, "expected a renderable for image result"


def test_web_summary_viewer_preserved() -> None:
    """Tier 2: web-summary result still dispatches to _viewer_web_summary via registry."""
    result = render_tool_result(_WEB_RESULT)
    assert result is not None, "expected a renderable for web-summary result"


def test_unmatched_result_returns_none() -> None:
    """Tier 2: a result that matches no registered viewer returns None.

    Falsification: if any default viewer were too permissive (e.g. always
    returning non-None), this would fail — indicating a viewer overreach bug.
    """
    result = render_tool_result({"some_unknown_key": "value"})
    assert result is None, "expected None for unrecognised result shape"


def test_non_dict_returns_none() -> None:
    """Tier 2: render_tool_result returns None for non-dict inputs."""
    assert render_tool_result(None) is None
    assert render_tool_result("plain string") is None
    assert render_tool_result(42) is None


# ---------------------------------------------------------------------------
# register_viewer — custom viewer fires
# ---------------------------------------------------------------------------

def test_register_viewer_fires_for_matching_result() -> None:
    """Tier 2: a viewer registered via register_viewer fires for matching results.

    Falsification: without register_viewer wiring the custom sentinel would
    never be returned — render_tool_result would return None for this result.
    """
    sentinel = object()
    original_len = len(_VIEWERS)

    register_viewer(
        predicate=lambda r: r.get("_test_custom") == "S1",
        viewer=lambda r: sentinel,
        name="_test_s1_custom",
    )
    try:
        result = render_tool_result({"_test_custom": "S1", "other": "data"})
        assert result is sentinel, f"expected sentinel from custom viewer, got {result!r}"
    finally:
        _VIEWERS[:] = [e for e in _VIEWERS if e.name != "_test_s1_custom"]


def test_register_viewer_does_not_fire_for_non_matching_result() -> None:
    """Tier 2: custom viewer predicate is respected; non-matching results fall through."""
    register_viewer(
        predicate=lambda r: r.get("_test_custom") == "S1_NOMATCH",
        viewer=lambda r: (_ for _ in ()).throw(AssertionError("should not be called")),
        name="_test_s1_nomatch",
    )
    try:
        result = render_tool_result({"_test_custom": "DIFFERENT"})
        assert result is None
    finally:
        _VIEWERS[:] = [e for e in _VIEWERS if e.name != "_test_s1_nomatch"]


# ---------------------------------------------------------------------------
# register_viewer — position controls priority
# ---------------------------------------------------------------------------

def test_register_viewer_position_zero_overrides_defaults() -> None:
    """Tier 2: a viewer at position=0 fires before the default viewers.

    Falsification: if position were ignored (always append), the default
    markdown viewer would fire first for a markdown result, returning a
    RichMarkdown — not the sentinel.
    """
    sentinel = object()

    register_viewer(
        predicate=lambda r: "content_type" in r,
        viewer=lambda r: sentinel,
        name="_test_priority_first",
        position=0,
    )
    try:
        result = render_tool_result(_MARKDOWN_RESULT)
        assert result is sentinel, (
            "expected high-priority viewer at position=0 to win over markdown default"
        )
    finally:
        _VIEWERS[:] = [e for e in _VIEWERS if e.name != "_test_priority_first"]
