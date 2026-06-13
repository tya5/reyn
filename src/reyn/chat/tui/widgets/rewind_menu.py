"""RewindMenuWidget — inline time-travel checkpoint picker (ADR-0038 1f).

Mounts inside ConversationView (not a modal dialog), like InterventionWidget.
Renders the rewind-point timeline from ``AgentRegistry.list_rewind_points()``
— one row per snapshot-generation boundary (``seq · ⏱ rel-time · kind``) — and
lets the user pick a checkpoint to rewind to.

Design (per tui-coder 1f UI-fit cross-review):
- **``can_focus = False``** — the widget never steals focus; the InputBar stays
  focused so the user can always type or Esc out. Navigation is app-driven:
  the App intercepts ↑/↓/Enter (gated to menu-open) and calls
  ``move_selection`` / ``selected_point``; Esc dismiss is handled by the App's
  ``action_voice_cancel`` (Esc is a ``priority`` app binding, so it never
  reaches the widget). See ``app.py``.
- **Scroll window** — the list can hold 20+ checkpoints, so only ``_MAX_VISIBLE``
  rows render at once, windowed around the selection (mirrors SlashPicker).
- **Unmount** is decoupled from the intervention path — the App removes the
  widget via ``widget.remove()`` after a selection or Esc.

This is a passive render + selection-state widget; the rewind itself (calling
``AgentRegistry.rewind_to``) is orchestrated by the App, which owns the registry.
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

# Max checkpoint rows rendered at once (mirrors SlashPicker._MAX_VISIBLE). The
# window slides to keep the selected row visible when the list is longer.
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
        points:          Rows from ``AgentRegistry.list_rewind_points()`` —
                         each ``{"seq": int, "ts": str, "kind": str}``,
                         ascending by seq.
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
        points: list[dict] | None = None,
        *,
        rel_time_fn=None,
        id: str | None = None,
        _tree_rows: list[dict] | None = None,
    ) -> None:
        super().__init__(id=id)
        self._rel_time_fn = rel_time_fn or format_rel_time
        if _tree_rows is not None:
            # Tree mode (Phase-2 fork picker): rows from build_branch_tree_rows
            # — header decorators (non-selectable) interleaved with selectable
            # checkpoint rows. Selection indexes the selectable subset.
            self._mode = "tree"
            self._rows = list(_tree_rows)
            self._selectable = [
                i for i, r in enumerate(self._rows) if r.get("row") == ROW_CHECKPOINT
            ]
            self._points = []
            # Default = the working-tree head: the first selectable row, which
            # (active-first, newest-first ordering) is the active branch's newest
            # checkpoint → Enter immediately = undo (Phase-1 parity).
            self._selected = 0
        else:
            # Flat mode (Phase-1 /rewind timeline).
            self._mode = "flat"
            self._points = list(points or [])
            self._rows = []
            self._selectable = []
            # Default selection = the most-recent checkpoint (bottom).
            self._selected = max(0, len(self._points) - 1)

    @classmethod
    def from_tree_rows(
        cls,
        rows: list[dict],
        *,
        rel_time_fn=None,
        id: str | None = None,
    ) -> "RewindMenuWidget":
        """Construct the Phase-2 fork picker over branch-tree rows (always-tree)."""
        return cls(rel_time_fn=rel_time_fn, id=id, _tree_rows=rows)

    # ── selection (app-driven nav) ────────────────────────────────────────────

    @property
    def selected_index(self) -> int:
        """0-based index of the current selection within the selectable rows."""
        return self._selected

    def move_selection(self, delta: int) -> None:
        """Move the highlight by ``delta`` *selectable* rows, clamped.

        Clamp (not wrap): the timeline is finite and ordered. In tree mode the
        cursor moves among checkpoint rows only — header rows are skipped.
        """
        n = len(self._selectable) if self._mode == "tree" else len(self._points)
        if n == 0:
            return
        self._selected = max(0, min(n - 1, self._selected + delta))
        self.refresh()

    def selected_point(self) -> dict | None:
        """The currently highlighted checkpoint row, or None when empty."""
        if self._mode == "tree":
            if not self._selectable:
                return None
            return self._rows[self._selectable[self._selected]]
        if not self._points:
            return None
        return self._points[self._selected]

    # ── rendering ─────────────────────────────────────────────────────────────

    def _visible_window(self) -> tuple[int, int]:
        """Return ``(start, end)`` row indices to render, keeping the selection
        visible within a ``_MAX_VISIBLE``-row window."""
        n = len(self._points)
        if n <= _MAX_VISIBLE:
            return 0, n
        # Center the selection where possible, then clamp to the ends.
        half = _MAX_VISIBLE // 2
        start = max(0, min(self._selected - half, n - _MAX_VISIBLE))
        return start, start + _MAX_VISIBLE

    def _render_tree(self) -> Text:
        """Render the branch-tree fork picker (Phase-2 2b, always-tree).

        Header rows are dim/bright decorators (active vs inactive branch); the
        ▌ caret sits only on the selected checkpoint row. Indent = depth. The
        `▸` marks the working-tree head's branch.
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
        for i, r in enumerate(self._rows):
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
                continue
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
        body.append(
            "  ↑/↓ select · Enter checkout · Esc cancel\n", style=f"dim {_TEXT_DIM}",
        )
        return body

    def render(self) -> Text:
        if self._mode == "tree":
            return self._render_tree()
        body = Text()
        body.append("  ⏪ rewind — pick a checkpoint\n", style=f"bold {_CORAL}")

        if not self._points:
            body.append("  (no checkpoints yet)\n", style=f"dim {_TEXT_DIM}")
            return body

        n = len(self._points)
        start, end = self._visible_window()

        if start > 0:
            body.append(f"  ↑ {start} earlier…\n", style=f"dim {_TEXT_DIM}")

        # Width for the seq column so kinds/times align.
        seq_w = max((len(f"#{p['seq']}") for p in self._points), default=3)

        for i in range(start, end):
            p = self._points[i]
            is_sel = i == self._selected
            row = Text()
            row.append("▌ " if is_sel else "  ", style=_CORAL if is_sel else _BG_HEADER)
            seq_label = f"#{p['seq']}".ljust(seq_w)
            row.append(seq_label, style=f"bold {_CORAL}" if is_sel else _TEXT_BRIGHT)
            row.append("  ")
            kind = _KIND_LABEL.get(p.get("kind", ""), p.get("kind", ""))
            row.append(kind.ljust(10), style=_TEXT_MUTED)
            rel = self._rel_time_fn(p.get("ts", ""))
            if rel:
                row.append(f"  {rel}", style=f"dim {_TEXT_DIM}")
            row.append("\n")
            body.append_text(row)
            # #1547: per-checkpoint anchor (truncated last user message) as a 2nd
            # dim line, aligned under the kind column. Additive — empty = omitted.
            anchor = p.get("anchor", "")
            if anchor:
                body.append(
                    f"{' ' * (4 + seq_w)}{anchor}\n", style=f"dim {_TEXT_DIM}",
                )

        if end < n:
            body.append(f"  ↓ {n - end} later…\n", style=f"dim {_TEXT_DIM}")

        body.append(
            "  ↑/↓ select · Enter rewind · Esc cancel\n",
            style=f"dim {_TEXT_DIM}",
        )
        return body


__all__ = ["RewindMenuWidget"]
