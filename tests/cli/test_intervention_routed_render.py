"""Tier 2: events tab renders ``intervention_routed`` events with route + kind + iv_id.

Issue #261 — Phase 4 emits ``intervention_routed`` from
``ChatSession.handle_intervention``'s three branches (self_answer /
parent_delegate / user_channel). The events tab surfaces those as
one-line entries so the user can audit routing decisions.

This test pins the public ``_event_hint`` formatter (= the function
``render_events`` consumes for each event row).
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.widgets.right_panel.events_tab import (
    _EVENT_COLORS,
    _FILTER_GROUPS,
    _event_hint,
)


def _event(route: str, kind: str, iv_id: str) -> dict:
    return {
        "type": "intervention_routed",
        "data": {"route": route, "iv_kind": kind, "iv_id": iv_id},
    }


def test_event_hint_renders_route_kind_short_id() -> None:
    """Tier 2: hint string includes route, iv_kind, and short iv_id."""
    hint = _event_hint(_event("user_channel", "ask_user", "iv-abcd1234"))
    assert "user_channel" in hint
    assert "ask_user" in hint
    # Short-form iv_id (first 8 chars) appears in brackets.
    assert "iv-abcd1" in hint


def test_event_hint_handles_each_route_branch() -> None:
    """Tier 2: all 3 Phase 4 routes render distinct hints (no fallthrough)."""
    seen: set[str] = set()
    for route in ("self_answer", "parent_delegate", "user_channel"):
        hint = _event_hint(_event(route, "ask_user", "iv-1"))
        assert route in hint, (
            f"hint must include route name {route!r}; got {hint!r}"
        )
        seen.add(hint)
    # All 3 produce distinct hints (= same iv_id / kind, route differs).
    assert len(seen) == len({"self_answer", "parent_delegate", "user_channel"}), (
        f"each route must produce a distinct hint; got duplicates in {seen!r}"
    )


def test_event_hint_handles_missing_fields_safely() -> None:
    """Tier 2: missing route / iv_kind / iv_id don't raise; use ``?`` fallback."""
    hint = _event_hint({"type": "intervention_routed", "data": {}})
    # ``?`` placeholders are present for route and iv_kind.
    assert "?" in hint


def test_event_color_registered_for_intervention_routed() -> None:
    """Tier 2: color palette includes ``intervention_routed`` (= rendered, not
    fallthrough-grey).

    Defends against a refactor that adds the formatter but forgets the
    color entry — the event would still render but with the default
    dim grey, hiding it visually next to the colourful neighbours.
    """
    assert "intervention_routed" in _EVENT_COLORS
    # Same amber family as the existing user_intervention_* pair (= UX
    # consistency — all "user gate" events share a colour).
    assert _EVENT_COLORS["intervention_routed"] == _EVENT_COLORS[
        "user_intervention_requested"
    ]


def test_filter_user_group_includes_intervention_routed() -> None:
    """Tier 2: the ``user`` filter group covers the new event type so a
    user filtering for human-touchpoint events sees routing decisions
    alongside intervention requests / receipts.
    """
    user_group = next(s for label, s in _FILTER_GROUPS if label == "user")
    assert "intervention_routed" in user_group
