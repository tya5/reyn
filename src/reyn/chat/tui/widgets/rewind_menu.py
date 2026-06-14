"""RewindMenuWidget — inline time-travel checkpoint picker (ADR-0038 1f).

Mounts inside ConversationView (not a modal dialog), like InterventionWidget.
Renders the branch tree from ``build_branch_tree_rows`` (``list_branches()`` +
``list_rewind_points(include_abandoned=True)``) — branch headers + per-branch
checkpoint rows (``seq · kind · rel-time`` + the #1547 anchor line) — and lets
the user pick a checkpoint to checkout (active node = undo, dead = fork-switch).

Design (per tui-coder 1f UI-fit cross-review; always-tree since #1561):
- **``can_focus = False``** — the widget never steals focus; the InputBar stays
  focused so the user can always type or Esc out. Navigation is app-driven:
  the App intercepts ↑/↓/Enter (gated to menu-open) and calls
  ``move_selection`` / ``selected_point``; Esc dismiss is handled by the App's
  ``action_voice_cancel`` (Esc is a ``priority`` app binding, so it never
  reaches the widget). See ``app.py``.
- **No scroll window (yet)** — every row renders; a windowing pass for long
  trees is tracked in #1577. (The Phase-1 flat-mode window was removed in #1563.)
- **Unmount** is decoupled from the intervention path — the App removes the
  widget via ``widget.remove()`` after a selection or Esc.

This is a passive render + selection-state widget; the checkout itself (calling
``AgentRegistry.checkout``) is orchestrated by the App, which owns the registry.
"""
from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.widget import Widget

from reyn.chat.tui._palette import (
    _BG_HEADER,
    _CORAL,
    _TEXT_BRIGHT,
    _TEXT_DIM,
    _TEXT_MUTED,
)
from reyn.chat.tui.widgets._branch_tree import ROW_CHECKPOINT, ROW_HEADER

# Max tree rows (headers + checkpoints) rendered at once (#1577 — mirrors
# SlashPicker / the old flat window). The window slides to keep the selected
# checkpoint visible; the selected branch's header is pinned when it scrolls off.
_MAX_VISIBLE = 8


def format_rel_time(ts: str) -> str:
    """Best-effort relative-time string for a WAL ISO timestamp.

    WAL entries carry ``ts`` as ``datetime.now().isoformat()``. Returns an
    empty string when the value is missing / unparseable / in the future so
    the row layout stays intact.
    """
    if not ts:
        return ""
    try:
        when = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ""
    now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
    delta = (now - when).total_seconds()
    if delta < 0:
        return ""
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"

# kind → short glyph/label for the row's boundary type column.
_KIND_LABEL = {
    "turn": "turn",
    "plan-step": "plan-step",
    "phase": "phase",
}


class RewindMenuWidget(Widget):
    """Inline checkpoint picker for ``/rewind``.

    Args:
        rows:            Branch-tree rows from ``build_branch_tree_rows`` —
                         header decorators + checkpoint rows; selection
                         indexes the selectable (checkpoint) subset.
        rel_time_fn:     Optional ``(ts: str) -> str`` formatter for the
                         relative-time column (e.g. "2m ago"). Defaults to
                         showing the raw ts (tests inject a deterministic fn).
    """

    can_focus = False  # trap 2: never steal focus from the InputBar.

    DEFAULT_CSS = f"""
    RewindMenuWidget {{
        background: {_BG_HEADER};
        border-left: thick {_CORAL};
        padding: 0 1;
        height: auto;
        margin: 1 0 0 0;
    }}
    """

    def __init__(
        self,
        rows: list[dict],
        *,
        rel_time_fn=None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._rel_time_fn = rel_time_fn or format_rel_time
        # Branch-tree rows from ``build_branch_tree_rows`` — header decorators
        # (non-selectable) interleaved with selectable checkpoint rows. Selection
        # indexes the selectable subset. (#1563: the Phase-1 flat timeline path
        # was removed — the always-tree picker is the only mode since #1561.)
        self._rows = list(rows)
        self._selectable = [
            i for i, r in enumerate(self._rows) if r.get("row") == ROW_CHECKPOINT
        ]
        # Default = the working-tree head: the first selectable row, which
        # (active-first, newest-first ordering) is the active branch's newest
        # checkpoint → Enter immediately = undo (Phase-1 parity).
        self._selected = 0

    @classmethod
    def from_tree_rows(
        cls,
        rows: list[dict],
        *,
        rel_time_fn=None,
        id: str | None = None,
    ) -> "RewindMenuWidget":
        """Construct the fork picker over branch-tree rows (kept for call-site
        clarity; equivalent to the constructor)."""
        return cls(rows, rel_time_fn=rel_time_fn, id=id)

    # ── selection (app-driven nav) ────────────────────────────────────────────

    @property
    def selected_index(self) -> int:
        """0-based index of the current selection within the selectable rows."""
        return self._selected

    def move_selection(self, delta: int) -> None:
        """Move the highlight by ``delta`` *selectable* rows, clamped.

        Clamp (not wrap): the timeline is finite and ordered. The cursor moves
        among checkpoint rows only — header rows are skipped.
        """
        n = len(self._selectable)
        if n == 0:
            return
        self._selected = max(0, min(n - 1, self._selected + delta))
        self.refresh()

    def selected_point(self) -> dict | None:
        """The currently highlighted checkpoint row, or None when empty."""
        if not self._selectable:
            return None
        return self._rows[self._selectable[self._selected]]

    # ── rendering ─────────────────────────────────────────────────────────────

    def _visible_window(self) -> tuple[int, int, "int | None"]:
        """``(start, end, pinned_header_idx)`` over ``self._rows`` (#1577).

        Keeps the selected checkpoint within a ``_MAX_VISIBLE``-row window
        (headers + checkpoints counted). When the selected checkpoint's branch
        header is scrolled above the window, its index is returned as
        ``pinned_header_idx`` so the branch context stays visible at the top.
        """
        n = len(self._rows)
        if n <= _MAX_VISIBLE:
            return 0, n, None
        sel_abs = self._selectable[self._selected] if self._selectable else 0
        half = _MAX_VISIBLE // 2
        start = max(0, min(sel_abs - half, n - _MAX_VISIBLE))
        end = start + _MAX_VISIBLE
        # The selected checkpoint's branch header = nearest header at/above it.
        pinned: int | None = None
        for j in range(sel_abs, -1, -1):
            if self._rows[j].get("row") == ROW_HEADER:
                pinned = j if j < start else None
                break
        return start, end, pinned

    def _append_row(self, body: Text, i: int, r: dict, sel_abs: int) -> None:
        """Render one tree row (branch header decorator, or a selectable
        checkpoint row + its #1547 anchor line) into ``body``."""
        indent = "  " * (r.get("depth", 0) + 1)
        if r.get("row") == ROW_HEADER:
            active = r.get("is_active")
            glyph = "● " if active else "◌ "
            mark = "▸ " if active else "  "
            style = _TEXT_BRIGHT if active else f"dim {_TEXT_DIM}"
            tag = "active" if active else "inactive"
            line = Text()
            line.append(f"{mark}{indent}{glyph}{r.get('label', '')}", style=style)
            line.append(f"   {tag}\n", style=f"dim {_TEXT_DIM}")
            body.append_text(line)
            return
        # checkpoint (selectable) row
        is_sel = i == sel_abs
        line = Text()
        line.append("▌ " if is_sel else "  ", style=_CORAL if is_sel else _BG_HEADER)
        line.append(indent)
        line.append(
            f"#{r.get('seq')}", style=f"bold {_CORAL}" if is_sel else _TEXT_BRIGHT,
        )
        kind = _KIND_LABEL.get(r.get("kind", ""), r.get("kind", ""))
        line.append(f"  {kind.ljust(10)}", style=_TEXT_MUTED)
        rel = self._rel_time_fn(r.get("ts", ""))
        if rel:
            line.append(f"  {rel}", style=f"dim {_TEXT_DIM}")
        line.append("\n")
        body.append_text(line)
        # #1547 (restored for tree, #1576): per-checkpoint anchor (truncated last
        # user message) as a dim line under the row, so the user can tell "which
        # turn was this?" per checkpoint. Additive: empty anchor → omitted.
        anchor = r.get("anchor", "")
        if anchor:
            body.append(f"  {indent}  {anchor}\n", style=f"dim {_TEXT_DIM}")

    def render(self) -> Text:
        """Render the branch-tree fork picker (Phase-2 2b, always-tree).

        Header rows are dim/bright decorators (active vs inactive branch); the
        ▌ caret sits only on the selected checkpoint row. Indent = depth. The
        `▸` marks the working-tree head's branch. Long trees are windowed
        (#1577) around the selection with ``↑/↓ N more`` markers, keeping the
        selected branch's header pinned for context.
        """
        body = Text()
        body.append(
            "  ⏪ checkout a checkpoint   (Enter = go here · Esc cancel)\n",
            style=f"bold {_CORAL}",
        )
        if not self._rows:
            body.append("  (no checkpoints yet)\n", style=f"dim {_TEXT_DIM}")
            return body

        sel_abs = self._selectable[self._selected] if self._selectable else -1
        n = len(self._rows)
        start, end, pinned = self._visible_window()

        # Pinned branch header (scrolled above the window) keeps branch context.
        if pinned is not None:
            self._append_row(body, pinned, self._rows[pinned], sel_abs)
        if start > 0:
            hidden = start - (1 if pinned is not None else 0)
            if hidden > 0:
                body.append(f"  ↑ {hidden} earlier…\n", style=f"dim {_TEXT_DIM}")

        for i in range(start, end):
            self._append_row(body, i, self._rows[i], sel_abs)

        if end < n:
            body.append(f"  ↓ {n - end} later…\n", style=f"dim {_TEXT_DIM}")
        body.append(
            "  ↑/↓ select · Enter checkout · ctrl+t edit · Esc cancel\n",
            style=f"dim {_TEXT_DIM}",
        )
        return body


__all__ = ["RewindMenuWidget"]
