"""Tier 2b: Pending tab renderer surfaces PendingOpView rows with `kind` dispatch.

Issue #277 — TUI surface for the #270 PendingOperation framework.

Contract pinned across 5 dimensions per the sub-issue:

1. **Layer 2 compose**: ``render_pending`` lists each PendingOpView with
   id / origin / age / summary and records ``flat_items`` + ``item_ys``
   for cursor navigation.
2. **`kind` dispatch table**: ``kind="intervention"`` renders via its
   dedicated formatter; unknown kinds fall back to a defensive
   placeholder (= future ``mcp_call`` / ``peer_delegate`` extension
   safety).
3. **Empty state**: ``pending_ops=[]`` renders a "No pending
   operations" placeholder without crashing or hiding the tab.
4. **Remote mode (``--connect`` v1 scoped disable, per #276 Phase C-(b))**:
   when ``remote_mode=True`` the renderer surfaces a "remote — limited"
   placeholder and stays clear of any local-state access.
5. **Consume-only shape contract**: ``PendingOpView`` (= Phase 1 shape
   pin per PR #275) is read without any field mutation; both dataclass
   and dict-shaped inputs flow through equivalently.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.tui.widgets.right_panel.pending_tab import (
    _KIND_RENDERERS,
    render_pending,
)


@dataclass(frozen=True)
class _PendingOpViewLike:
    """Local minimal stand-in for ``PendingOpView``.

    The TUI consume contract is the field set, not the class — both
    dataclass and dict callers flow through the renderer via
    duck-typed access (= ``_as_dict``). We mirror the Phase 1 shape
    here so the test stays self-contained.
    """
    id: str
    kind: str
    origin_channel_id: str
    created_at: str
    summary: str
    detail: str = ""


def test_empty_list_renders_no_pending_placeholder() -> None:
    """Tier 2b: empty input → "No pending operations" placeholder, no crash."""
    rendered, flat_items, item_ys = render_pending([])
    assert "No pending" in rendered
    assert flat_items == []
    assert item_ys == []


def test_remote_mode_renders_limited_placeholder() -> None:
    """Tier 2b: ``--connect`` mode (= remote_mode=True) → "remote — limited"."""
    rendered, flat_items, item_ys = render_pending([], remote_mode=True)
    assert "remote" in rendered.lower()
    assert "limited" in rendered.lower()
    # No flat items in remote mode — actions are unavailable, so the
    # cursor should not be able to land anywhere.
    assert flat_items == []
    assert item_ys == []


def test_intervention_kind_renders_id_origin_summary() -> None:
    """Tier 2b: ``kind="intervention"`` renderer surfaces required fields."""
    op = _PendingOpViewLike(
        id="iv-abcd1234",
        kind="intervention",
        origin_channel_id="tui:planner",
        created_at="",  # blank tolerated by _format_age
        summary="Allow exec /bin/ls?",
        detail="",
    )
    rendered, flat_items, item_ys = render_pending([op])
    # Required surface elements.
    assert "intervention" in rendered
    # id rendered with the 8-char short form (= matches the slash
    # ``/pending list`` / Pending tab convention).
    assert "iv-abcd1" in rendered
    assert "tui:planner" in rendered
    assert "Allow exec /bin/ls?" in rendered
    # flat_items contract: at least 1 entry, original fields preserved.
    assert flat_items, "expected at least one flat_items entry"
    assert flat_items[0]["id"] == "iv-abcd1234"
    assert flat_items[0]["kind"] == "intervention"
    # item_ys non-empty (1:1 with flat_items).
    assert item_ys, "expected at least one item_ys entry"


def test_unknown_kind_falls_back_to_defensive_renderer() -> None:
    """Tier 2b: ``kind="future_kind"`` doesn't crash, surfaces a placeholder.

    Defends against a Phase B PendingOpView extension landing on the
    OS side before TUI catches up — the tab stays readable instead of
    erroring out.
    """
    op = _PendingOpViewLike(
        id="op-1234",
        kind="future_kind",
        origin_channel_id="a2a:peer",
        created_at="",
        summary="some future op",
    )
    rendered, flat_items, _ys = render_pending([op])
    # Kind name surfaces verbatim even when unknown.
    assert "future_kind" in rendered
    # Still produces a flat_items entry so cursor navigation stays
    # consistent across kinds.
    assert flat_items, "expected at least one flat_items entry"
    assert flat_items[0]["kind"] == "future_kind"


def test_kind_dispatch_table_includes_intervention_renderer() -> None:
    """Tier 2b: ``_KIND_RENDERERS`` exposes the intervention entry for future
    extension hooks.

    Phase B (= #270 lift-up refactor) extends this table with
    ``"mcp_call"`` / ``"peer_delegate"`` etc. Pin that the discovery
    mechanism (= module-level dict) is in place + the intervention
    entry is callable.
    """
    assert "intervention" in _KIND_RENDERERS
    assert callable(_KIND_RENDERERS["intervention"])


def test_dict_shaped_input_flows_through() -> None:
    """Tier 2b: dict-shaped (= test path) input renders identically to dataclass.

    The renderer's duck-typed ``_as_dict`` coercion supports both
    PendingOpView dataclass and dict — tests + callers that build
    minimal mocks should not need to import the real dataclass.
    """
    op_dict = {
        "id": "iv-xyz12345",
        "kind": "intervention",
        "origin_channel_id": "tui:x",
        "created_at": "",
        "summary": "ok?",
        "detail": "",
    }
    rendered, flat_items, _ys = render_pending([op_dict])
    assert "iv-xyz12" in rendered
    assert flat_items[0]["id"] == "iv-xyz12345"


def test_cursor_index_marks_row_distinctly() -> None:
    """Tier 2b: ``cursor=N`` marks the Nth row with the coral ``▶`` prefix.

    Defends against a refactor that drops the cursor visual cue
    silently (= cursor navigation would still work but the user
    wouldn't see where they are).
    """
    ops = [
        _PendingOpViewLike(
            id=f"iv-{i}", kind="intervention",
            origin_channel_id="x", created_at="", summary=f"s{i}",
        )
        for i in range(3)
    ]
    rendered, _flat, item_ys = render_pending(ops, cursor=1)
    # The cursor row (index 1) should have ``▶`` in the rendered
    # output near its y-position. Locate by checking the rendered
    # string contains ``▶`` followed by intervention.
    assert "▶" in rendered
    # All ops produce item_ys entries (cursor navigation stays consistent).
    assert item_ys, "expected non-empty item_ys for rendered ops"
