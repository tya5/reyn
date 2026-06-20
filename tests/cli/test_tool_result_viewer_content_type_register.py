"""Tier 2: content-type-keyed viewer registration (#1154 registry generalization).

`register_content_type_viewer` is the ergonomic extension point for mapping a
content-type / MIME to a viewer without hand-writing a `_content_type_of`
predicate. It delegates to `register_viewer`, so name / position / first-match
semantics are unchanged; it just builds the predicate under one of three match
modes (exact / prefix / substring), single value or sequence, case-insensitive.

Falsification anchors:
- exact does NOT match a superstring ("application/x-foo" ≠ "application/x-foobar").
- prefix matches only at the start ("audio/" matches "audio/mpeg", not "x-audio/").
- substring matches anywhere; a sequence matches ANY listed value.
- a result with no content-type never matches (falls through).
- position is honoured (a position=0 content-type viewer wins over defaults).
"""
from __future__ import annotations

from reyn.interfaces.tui.widgets.right_panel.tool_result_viewers import (
    _VIEWERS,
    register_content_type_viewer,
    render_tool_result,
)


def _cleanup(name: str) -> None:
    _VIEWERS[:] = [e for e in _VIEWERS if e.name != name]


def test_exact_match_dispatches_and_rejects_superstring() -> None:
    """Tier 2: match='exact' matches the exact MIME only, not a superstring."""
    sentinel = object()
    register_content_type_viewer(
        "application/x-foo", lambda r: sentinel, name="_t_exact", match="exact",
    )
    try:
        assert render_tool_result({"content_type": "application/x-foo"}) is sentinel
        # a superstring must NOT match under exact
        assert render_tool_result({"content_type": "application/x-foobar"}) is not sentinel
    finally:
        _cleanup("_t_exact")


def test_prefix_match() -> None:
    """Tier 2: match='prefix' matches a leading value, not a non-prefix occurrence."""
    sentinel = object()
    register_content_type_viewer(
        "audio/", lambda r: sentinel, name="_t_prefix", match="prefix", position=0,
    )
    try:
        assert render_tool_result({"content_type": "audio/mpeg"}) is sentinel
        assert render_tool_result({"content_type": "x-audio/wav"}) is not sentinel
    finally:
        _cleanup("_t_prefix")


def test_substring_match() -> None:
    """Tier 2: match='substring' matches the value anywhere in the MIME."""
    sentinel = object()
    register_content_type_viewer(
        "yaml", lambda r: sentinel, name="_t_sub", match="substring", position=0,
    )
    try:
        assert render_tool_result({"content_type": "application/yaml"}) is sentinel
        assert render_tool_result({"content_type": "text/x-yaml"}) is sentinel
    finally:
        _cleanup("_t_sub")


def test_sequence_matches_any_listed_value() -> None:
    """Tier 2: a sequence of content-types matches if ANY entry matches."""
    sentinel = object()
    register_content_type_viewer(
        ("application/toml", "text/x-toml"),
        lambda r: sentinel,
        name="_t_seq",
        match="exact",
        position=0,
    )
    try:
        assert render_tool_result({"content_type": "application/toml"}) is sentinel
        assert render_tool_result({"content_type": "text/x-toml"}) is sentinel
        assert render_tool_result({"content_type": "application/json"}) is not sentinel
    finally:
        _cleanup("_t_seq")


def test_case_insensitive() -> None:
    """Tier 2: matching is case-insensitive (registered lower, MIME upper)."""
    sentinel = object()
    register_content_type_viewer(
        "application/x-case", lambda r: sentinel, name="_t_case", match="exact", position=0,
    )
    try:
        assert render_tool_result({"content_type": "APPLICATION/X-CASE"}) is sentinel
    finally:
        _cleanup("_t_case")


def test_no_content_type_does_not_match() -> None:
    """Tier 2: a result without any content-type field never matches the helper.

    Falsification: if the predicate did not guard the empty content-type, a
    typeless dict could wrongly dispatch to a content-type viewer.
    """
    sentinel = object()
    register_content_type_viewer(
        "text/plain", lambda r: sentinel, name="_t_none", match="exact", position=0,
    )
    try:
        assert render_tool_result({"some_field": "value"}) is not sentinel
    finally:
        _cleanup("_t_none")


def test_position_zero_overrides_default_viewers() -> None:
    """Tier 2: a content-type viewer at position=0 wins over the default chain.

    A JSON result normally dispatches to the built-in json viewer; a
    position=0 registration for the same MIME must win (first match).
    """
    sentinel = object()
    register_content_type_viewer(
        "json", lambda r: sentinel, name="_t_priority", match="substring", position=0,
    )
    try:
        result = render_tool_result({"content_type": "application/json", "content": "{}"})
        assert result is sentinel, "expected the position=0 viewer to win over the default json viewer"
    finally:
        _cleanup("_t_priority")
