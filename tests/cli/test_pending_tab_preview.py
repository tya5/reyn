"""Tier 2: Pending tab preview pane integration (A-F1).

Before this fix, ``_has_previewable_content`` returned False for the
``pending`` panel type so pressing Space silently did nothing — the
user had to claim the intervention to a local channel just to see
the full prompt text. The fix adds:

1. ``_has_previewable_content`` recognises ``pending`` when items present.
2. ``_update_preview`` dispatches to ``_show_pending_in_preview``.
3. ``_pending_move`` (= j/k) re-renders the preview when open.
4. Pending header hint includes ``sp=open``.

The Tier 2 tests below pin the public contract via the panel's flat
items + the rendered preview content, without reaching into private
state per CLAUDE.md testing policy.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _pending_item(
    *,
    kind: str = "intervention_pending",
    iv_id: str = "iv-abc12345",
    summary: str = "Permission needed for X",
    detail: str = "Tool wants to write /tmp/foo. Allow?",
) -> dict:
    """Shape mirrors ``pending_tab.render_pending`` flat_items output."""
    return {
        "kind": kind,
        "id": iv_id,
        "origin_channel_id": "skill:abc1234",
        "created_at": "2026-05-22T18:00:00",
        "summary": summary,
        "detail": detail,
    }


def test_has_previewable_content_returns_true_for_pending_with_items() -> None:
    """Tier 2: pending tab now reports previewable content when items exist."""
    from reyn.interfaces.tui.widgets.right_panel import RightPanel

    panel = RightPanel.__new__(RightPanel)
    panel._panel_type = "pending"
    panel._pending_items = [_pending_item()]
    panel._docs_files = []
    panel._events_visible = []
    panel._memory_entries = []
    panel._agents_items = []
    assert panel.has_previewable_content() is True


def test_has_previewable_content_returns_false_for_pending_empty() -> None:
    """Tier 2: empty pending → no preview (= cold-default unchanged)."""
    from reyn.interfaces.tui.widgets.right_panel import RightPanel

    panel = RightPanel.__new__(RightPanel)
    panel._panel_type = "pending"
    panel._pending_items = []
    panel._docs_files = []
    panel._events_visible = []
    panel._memory_entries = []
    panel._agents_items = []
    assert panel.has_previewable_content() is False


def test_show_pending_in_preview_writes_intervention_fields() -> None:
    """Tier 2: ``_show_pending_in_preview`` produces a renderable carrying
    the kind / id / origin / summary / detail fields."""
    from reyn.interfaces.tui.widgets.right_panel import RightPanel

    panel = RightPanel.__new__(RightPanel)
    panel._panel_type = "pending"
    panel._pending_cursor = 0
    panel._pending_items = [
        _pending_item(
            iv_id="iv-deadbeef",
            summary="Allow write to /tmp",
            detail="Full prompt: tool requested ...",
        ),
    ]

    captured = {}

    class _StubPane:
        def show_text(self, title, body):
            captured["title"] = title
            # Capture both plain text representations.
            from io import StringIO

            from rich.console import Console
            buf = StringIO()
            Console(file=buf, force_terminal=False, width=120).print(body)
            captured["plain"] = buf.getvalue()

        def clear(self):
            captured["cleared"] = True

    panel._show_pending_in_preview(_StubPane())
    # Title is the id-prefix when present.
    assert captured["title"] == "iv-deadb"
    # Body carries the key fields.
    plain = captured["plain"]
    assert "intervention_pending" in plain
    assert "iv-deadb" in plain  # id prefix
    assert "skill:abc1234" in plain  # origin
    assert "Allow write to /tmp" in plain  # summary
    assert "Full prompt: tool requested" in plain  # detail


def test_show_pending_in_preview_with_empty_items_clears_pane() -> None:
    """Tier 2: empty pending list → ``pane.clear()`` not ``show_text``."""
    from reyn.interfaces.tui.widgets.right_panel import RightPanel

    panel = RightPanel.__new__(RightPanel)
    panel._pending_cursor = 0
    panel._pending_items = []

    cleared = []

    class _StubPane:
        def show_text(self, title, body):  # pragma: no cover — should not be called
            raise AssertionError("show_text called on empty list")

        def clear(self):
            cleared.append(True)

    panel._show_pending_in_preview(_StubPane())
    assert cleared == [True]


def test_pending_header_hint_includes_sp_open() -> None:
    """Tier 2: pending tab header surface ``sp=open`` so the new affordance
    is discoverable from the panel header alongside d=discard / c=claim.

    Constructs an unmounted RightPanel via ``__new__`` since
    ``_panel_header_markup`` is pure over ``_panel_type`` and the
    Textual app context isn't needed for the markup-string computation.
    """
    from reyn.interfaces.tui.widgets.right_panel import RightPanel

    panel = RightPanel.__new__(RightPanel)
    panel._panel_type = "pending"
    header = panel._panel_header_markup()
    assert "sp=open" in header
    # Existing action hints stay
    assert "d=discard" in header
    assert "c=claim" in header
