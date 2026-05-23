"""AsyncStackPanel — sticky list of non-attach agents currently running.

PoC stage for issue #427 L4 step 5: widget shape + entry state mgmt +
ordering + cap behaviour. Production wiring (= subscribe to agent
registry / non-attach agent lifecycle events, drive entries on the
real signal) is deferred to a follow-up PR.

Spec (= issue #427 L2):

  ⟳ async: code_review · 12.3s      ← running entry
  ⟳ async: monitor_loop · 5m21s      ← running, longer-running
  ⚑ async: alice (1 pending)         ← intervention-pending entry
  … +N more (panel for all)          ← overflow indicator when > _CAP

- 1-5 entries dynamic, ``_CAP`` cap with overflow indicator
- Sort: intervention-pending (= ⚑) on top, then running by elapsed (= shortest first)
- Lifetime: entries appear on ``add()`` / ``set_pending()``, disappear on
  ``remove()``; history persists via inline conv-pane markers (= separate
  surface, not this widget's concern)
- Dock: bottom (= above input bar, complements StickyStatus which keeps
  its 1-line attached-agent thinking role)

Per ``feedback-tui-visibility-axis``: bottom strip is for ephemeral
runtime state (= "what's running right now"), not chat history. This
widget never emits to history; consumers route history-side events
(= start / complete inline markers) through a separate path.

Public API:
    panel.add(agent_id, summary)       # mount / update a running entry
    panel.set_pending(agent_id, count) # switch entry to ⚑ + pending count
    panel.set_running(agent_id, summary) # switch back to ⟳ (intervention resolved)
    panel.remove(agent_id)             # entry disappeared (complete / fail)
    panel.clear()                      # reset all entries
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from reyn.chat.tui._palette import _AMBER, _CORAL

# Visible cap. Past this, overflow collapses to a single "… +N more" row
# so the panel never crowds the input bar even under unusual fan-out.
_CAP = 5

# Tick rate for elapsed-time updates. 1 Hz is plenty for ambient status;
# slower than SkillActivityRow's 0.5s by design (= ambient, not focus).
_TICK_INTERVAL_S = 1.0

_GLYPH_RUNNING = "⟳"
_GLYPH_PENDING = "⚑"

_RIGHT_MARGIN_CELLS = 4


@dataclass
class _Entry:
    """One row in the stack. Keyed externally by ``agent_id`` in the
    panel's dict; this struct just carries the renderable state."""

    summary: str
    started_at: float
    pending_count: int = 0  # 0 = running, >0 = intervention pending


def _fmt_elapsed(seconds: float) -> str:
    """Compact elapsed time for ambient status (= "12.3s" / "5m21s" / "1h12m")."""
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    if seconds < 3600.0:
        m = int(seconds // 60)
        s = int(seconds - m * 60)
        return f"{m}m{s:02d}s"
    h = int(seconds // 3600)
    m = int((seconds - h * 3600) // 60)
    return f"{h}h{m:02d}m"


class AsyncStackPanel(Widget):
    """Bottom-docked sticky list of non-attach agent activity.

    State machine per entry:
        add(summary)            → running (⟳)
        set_pending(count)      → pending (⚑ count)
        set_running(summary)    → back to running (⟳, e.g. intervention resolved)
        remove()                → entry gone

    The widget itself stays mounted (= dock:bottom, height:auto) but
    renders empty when no entries are active. When there's nothing to
    show, ``_build_lines()`` returns an empty Text and Textual collapses
    the height — input bar regains full vertical space.
    """

    DEFAULT_CSS = """
    AsyncStackPanel {
        dock: bottom;
        height: auto;
        padding: 0 1;
        background: transparent;
    }
    """

    can_focus = False

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        # Insertion-order dict so two entries with identical elapsed
        # break ties by add-order (= older entries float to the top
        # of their sort bucket — matches the "shortest first within
        # bucket" intent + makes test ordering deterministic).
        self._entries: dict[str, _Entry] = {}
        self._static: Static | None = None

    # ── Textual lifecycle ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        self._static = Static(id="async_stack_text")
        yield self._static

    def on_mount(self) -> None:
        self.set_interval(_TICK_INTERVAL_S, self._tick)
        self._refresh()

    # ── Public API ───────────────────────────────────────────────────────────

    def add(self, agent_id: str, summary: str) -> None:
        """Mount or update a running entry for ``agent_id``."""
        if not agent_id:
            return
        existing = self._entries.get(agent_id)
        if existing is not None:
            existing.summary = summary
            existing.pending_count = 0  # back to running on summary update
        else:
            self._entries[agent_id] = _Entry(
                summary=summary,
                started_at=time.monotonic(),
            )
        self._refresh()

    def set_pending(self, agent_id: str, count: int) -> None:
        """Switch ``agent_id``'s entry to ⚑ glyph with the given pending count.

        No-op when the entry doesn't exist (= ordering between agent
        registry signals isn't strictly guaranteed; treat unknown
        agent_id as "not visible yet" rather than auto-mounting).
        """
        entry = self._entries.get(agent_id)
        if entry is None:
            return
        entry.pending_count = max(0, int(count))
        self._refresh()

    def set_running(self, agent_id: str, summary: str | None = None) -> None:
        """Switch ``agent_id`` back to running (⟳) state.

        Used when an intervention is resolved and the agent resumes
        execution. ``summary`` (optional) lets the caller refresh the
        displayed text at the same time.
        """
        entry = self._entries.get(agent_id)
        if entry is None:
            return
        entry.pending_count = 0
        if summary is not None:
            entry.summary = summary
        self._refresh()

    def remove(self, agent_id: str) -> None:
        """Remove ``agent_id``'s entry (= complete / fail / disappear)."""
        if agent_id in self._entries:
            del self._entries[agent_id]
            self._refresh()

    def clear(self) -> None:
        """Reset to empty (= no entries)."""
        if self._entries:
            self._entries.clear()
            self._refresh()

    def snapshot(self) -> list[dict]:
        """Public read of the current entry list for tests / introspection.

        Returns the visible (= sorted, capped, overflow-substituted) view
        as a list of ``{"agent_id", "glyph", "summary", "pending_count",
        "elapsed_s", "is_overflow"}`` dicts. Lets callers verify ordering
        + overflow without reaching into private state (= per testing
        policy public-surface convention).
        """
        out: list[dict] = []
        for agent_id, entry, glyph in self._sorted_visible():
            out.append({
                "agent_id": agent_id,
                "glyph": glyph,
                "summary": entry.summary,
                "pending_count": entry.pending_count,
                "elapsed_s": time.monotonic() - entry.started_at,
                "is_overflow": False,
            })
        overflow = len(self._entries) - len(out)
        if overflow > 0:
            out.append({
                "agent_id": "",
                "glyph": "",
                "summary": f"… +{overflow} more (panel for all)",
                "pending_count": 0,
                "elapsed_s": 0.0,
                "is_overflow": True,
            })
        return out

    # ── Internal rendering ───────────────────────────────────────────────────

    def _tick(self) -> None:
        if self._entries:
            self._refresh()

    def _sorted_visible(self) -> list[tuple[str, _Entry, str]]:
        """Return up to ``_CAP`` (agent_id, entry, glyph) tuples in display order.

        Sort: pending entries (= ⚑) first, then running entries (= ⟳)
        ordered by elapsed shortest-first. Within each bucket, ties
        broken by insertion order (= dict iteration stability).
        """
        pending: list[tuple[str, _Entry, str]] = []
        running: list[tuple[str, _Entry, str]] = []
        for agent_id, entry in self._entries.items():
            if entry.pending_count > 0:
                pending.append((agent_id, entry, _GLYPH_PENDING))
            else:
                running.append((agent_id, entry, _GLYPH_RUNNING))
        running.sort(key=lambda triple: time.monotonic() - triple[1].started_at)
        ordered = pending + running
        return ordered[:_CAP]

    def _build_lines(self) -> Text:
        """Multi-line Text — one row per visible entry + overflow indicator."""
        t = Text()
        if not self._entries:
            return t
        try:
            total_width = int(getattr(self.size, "width", 0))
        except Exception:
            total_width = 0
        if total_width <= 0:
            total_width = 80
        body_budget = max(20, total_width - _RIGHT_MARGIN_CELLS)
        visible = self._sorted_visible()
        for i, (agent_id, entry, glyph) in enumerate(visible):
            if i > 0:
                t.append("\n")
            self._append_row(t, agent_id, entry, glyph, body_budget)
        overflow = len(self._entries) - len(visible)
        if overflow > 0:
            t.append("\n")
            tail = f"… +{overflow} more (panel for all)"
            t.append(self._truncate_to_cells(tail, body_budget), style="dim #888888")
        return t

    def _append_row(
        self,
        t: Text,
        agent_id: str,
        entry: _Entry,
        glyph: str,
        body_budget: int,
    ) -> None:
        """Append one ``<glyph> async: <agent_id> · <body> · <elapsed>`` row.

        Truncation order on overflow: shrink ``summary`` first (= most
        disposable), keep agent_id + glyph + elapsed always visible.
        """
        elapsed_str = _fmt_elapsed(time.monotonic() - entry.started_at)
        if entry.pending_count > 0:
            elapsed_segment = f"  ({entry.pending_count} pending)"
            elapsed_style = "bold #ffaa44"
            glyph_style = _AMBER
        else:
            elapsed_segment = f"  · {elapsed_str}"
            elapsed_style = "dim"
            glyph_style = _CORAL

        prefix = f"{glyph} async: {agent_id}"
        prefix_cells = cell_len(prefix)
        elapsed_cells = cell_len(elapsed_segment)
        sep = "  · " if entry.summary else ""
        sep_cells = cell_len(sep)

        summary_budget = max(
            0, body_budget - prefix_cells - sep_cells - elapsed_cells,
        )
        summary_display = self._truncate_to_cells(entry.summary, summary_budget)

        t.append(f"{glyph} ", style=glyph_style)
        t.append("async: ", style="dim #888888")
        t.append(agent_id, style="bold")
        if summary_display:
            t.append(sep, style="dim")
            t.append(summary_display, style="dim")
        t.append(elapsed_segment, style=elapsed_style)

    def _truncate_to_cells(self, text: str, max_cells: int) -> str:
        if max_cells <= 0:
            return ""
        if cell_len(text) <= max_cells:
            return text
        ellipsis = "…"
        budget = max_cells - cell_len(ellipsis)
        if budget <= 0:
            return ellipsis
        out: list[str] = []
        used = 0
        for ch in text:
            w = cell_len(ch)
            if used + w > budget:
                break
            out.append(ch)
            used += w
        return "".join(out) + ellipsis

    def _refresh(self) -> None:
        """Re-render the rows after a state change.

        Wave-10 follow-up I-F11: ``self._static`` is assigned inside
        ``compose()`` BEFORE the ``yield``. Textual considers the
        widget mounted only after ``yield`` returns + the DOM append
        completes on the next event-loop tick. An external caller
        invoking ``add()`` / ``set_pending()`` / ``remove()`` before
        the widget is attached (= pre-mount race in test harness, or
        future wiring that hooks into a pre-mount registry event)
        passes the bare ``self._static is None`` guard because the
        attribute IS set — but ``self._static.update(...)`` lands on
        a Static that has no parent in the DOM. The update is silently
        dropped by Textual and the visible state diverges from the
        widget's internal model.

        Adding ``is_mounted`` (= Textual's public "widget is in the
        DOM" check) makes the pre-mount path a no-op. Mounted-but-
        pending updates still flush correctly because on_mount()
        calls ``_refresh`` directly after marking the widget
        attached.
        """
        if self._static is None:
            return
        # ``is_mounted`` is the public Textual attribute for "widget
        # has reached the post-on_mount DOM-attached state". Falls
        # back to True when missing (= ancient Textual versions) so
        # the new gate doesn't break existing pre-mount behaviour on
        # incompatible runtimes.
        if not getattr(self, "is_mounted", True):
            return
        self._static.update(self._build_lines())


__all__ = ["AsyncStackPanel"]
