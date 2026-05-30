"""AsyncStackPanel — sticky list of the attached agent's running tasks.

Wave-13 T2-2: ``remove(plan_id, terminal=...)`` flash extension.

When ``terminal`` is ``"aborted"`` or ``"interrupted"``, the row briefly
transitions to a red ``"✗ async: <id> (interrupted)"`` state for ~1.5 s
before unmounting, so the user can distinguish abnormal terminations from
clean completions. The same ``terminal="ok"`` default preserves the
existing immediate-unmount behaviour.

Flash lifecycle per entry:
  add(summary)                     → running (⟳)
  set_pending(count)               → pending (⚑)
  set_running(summary)             → back to running (⟳)
  remove(ok)                       → immediate unmount
  remove(aborted|interrupted)      → transition to _FLASH state → 1.5s timer
                                     → unmount (earlier remove cancels timer)


Scope (= user direction 2026-05-23): only the **attached** agent's
tasks are surfaced. A "task" is the SP-level abstraction covering
both **skill spawns** and **plan spawns** — the same lifecycle the
LLM observes via ``[task_spawned]`` / ``[task_completed]``
notifications. Non-attached agent activity flows through its own
event log and is not the TUI's concern.

The original PoC framing (= "non-attach agents currently running",
issue #427 L4 step 5) was narrowed to attached-only because:
  - the attached agent's tasks are already tracked by the trace
    lifecycle the App drives (``_handle_trace_for_skill_row``), so
    the event source needs no new plumbing
  - non-attached agents have their own ``.reyn/events/agents/<name>/``
    surface (= visible via the right-panel Agents tab) that suits
    a "what other agents are doing" overview better than a sticky
    1-line strip

Spec (= issue #427 L2 visual contract, retained):

  ⟳ async: code_review · 12.3s      ← running task
  ⟳ async: monitor_loop · 5m21s      ← running, longer-running
  ⚑ async: alice (1 pending)         ← intervention-pending task
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

    panel.add(agent_id, summary)         # mount / update a running entry
    panel.set_pending(agent_id, count)   # switch entry to ⚑ + pending count
    panel.set_running(agent_id, summary) # switch back to ⟳ (intervention resolved)
    panel.remove(agent_id)               # entry disappeared (complete / fail)
    panel.clear()                        # reset all entries

The ``agent_id`` parameter is a legacy name from the PoC era — in
the attached-agent-only production wiring, this string is the
**task identity** (= ``run_id`` for skill spawns, ``plan_id`` for
plan spawns). The widget treats it as an opaque key.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from reyn.chat.tui._palette import (
    _AMBER,
    _BG_HEADER,
    _CORAL,
    _STATUS_ERROR,
    _STATUS_WARN,
    _TEXT_MUTED,
)

from ._renderable_cache import RenderableCacheMixin

# Visible cap. Past this, overflow collapses to a single "… +N more" row
# so the panel never crowds the input bar even under unusual fan-out.
_CAP = 5

# Tick rate for elapsed-time updates. 1 Hz is plenty for ambient status;
# slower than SkillActivityRow's 0.5s by design (= ambient, not focus).
_TICK_INTERVAL_S = 1.0

_GLYPH_RUNNING = "⟳"
_GLYPH_PENDING = "⚑"
_GLYPH_INTERRUPTED = "✗"

# How long (in seconds) an interrupted/aborted row stays visible before
# unmounting. Long enough to be noticed; short enough to clear itself.
_FLASH_DURATION_S = 1.5

_RIGHT_MARGIN_CELLS = 4

# Wave-11 B#5: when the row is narrow enough that summary would be
# wiped to 0 cells, the agent_id (often a 36-char UUID) gets
# middle-elided to free cells for the more-informative summary.
# Below this many cells of summary budget, prefer to elide id over
# blanking summary entirely. 8 cells fits a typical short word.
_MIN_SUMMARY_BUDGET_CELLS = 8

# Minimum cells preserved for the elided agent_id: 4 head + 1 "…"
# + 4 tail = 9. Typical UUID head ``abcd`` + tail ``7890`` is
# enough to disambiguate among ≤ _CAP simultaneous tasks.
_MIN_ELIDED_ID_CELLS = 9


def _middle_elide_id(agent_id: str, max_cells: int) -> str:
    """Middle-elide an identifier to fit ``max_cells`` cells.

    Returns ``head + '…' + tail``. Below 3 cells degrades to plain
    head truncation (= can't fit head+ellipsis+tail meaningfully).
    Input shorter than the budget round-trips unchanged so calling
    this on already-fitting IDs is a safe no-op.
    """
    if cell_len(agent_id) <= max_cells:
        return agent_id
    if max_cells < 3:
        return agent_id[:max_cells]
    keep = max_cells - 1  # 1 cell for "…"
    head_n = keep // 2
    tail_n = keep - head_n
    return agent_id[:head_n] + "…" + agent_id[-tail_n:]


@dataclass
class _Entry:
    """One row in the stack. Keyed externally by ``agent_id`` in the
    panel's dict; this struct just carries the renderable state."""

    summary: str
    started_at: float
    pending_count: int = 0  # 0 = running, >0 = intervention pending
    # Wave-13 T2-2: flash state for terminal non-ok removal.
    # ``True`` while the 1.5 s interrupted-flash window is active;
    # the render path reads this to show the red ✗ shape instead of
    # the normal ⟳ / ⚑ row.
    flashing: bool = False
    # Captured at the moment flashing starts; None = compute live elapsed.
    # Freeze elapsed so the row reads "stopped, fading out" rather than
    # "still running" during the 1.5 s flash window.
    frozen_elapsed_s: float | None = None


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


class AsyncStackPanel(RenderableCacheMixin, Widget):
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
        max-height: 6;
        padding: 0 1;
        background: transparent;
        overflow: hidden;
    }
    AsyncStackPanel #async_stack_text {
        /* Force the inner Static to clip rather than wrap when a
           pre-truncated row's residual width briefly exceeds the
           constrained panel width (= e.g. between Ctrl+B side-panel
           mount and the next ``_refresh`` tick, ``self.size.width``
           reports the wider pre-mount value and ``_build_lines``
           pre-truncates against that stale budget). Combined with
           the ``on_resize`` hook below which forces a synchronous
           rebuild on the new width, wrap is eliminated. */
        height: auto;
        overflow: hidden;
    }
    AsyncStackPanel:focus {
        background: #1a1a1a;
    }
    """

    # Focusable so the F4 binding can route focus here and j/k
    # navigation works. Focus visibility is the ``:focus`` CSS
    # background change above; the per-row selection caret is in
    # ``_build_lines`` (= ``▌`` glyph on the cursor row when focused).
    can_focus = True

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        # Insertion-order dict so two entries with identical elapsed
        # break ties by add-order (= older entries float to the top
        # of their sort bucket — matches the "shortest first within
        # bucket" intent + makes test ordering deterministic).
        self._entries: dict[str, _Entry] = {}
        self._static: Static | None = None
        # Wave-13 T2-2: per-entry deferred-unmount timer handles.
        # Keyed by agent_id; value is a Textual timer object (or any
        # object with a ``stop()`` method — tests may inject a stub).
        # When ``remove(terminal!="ok")`` fires, a timer is placed here.
        # If ``remove()`` is called again for the same id before the
        # timer fires, the pending timer is cancelled and the entry is
        # unmounted immediately.
        self._flash_timers: dict[str, object] = {}
        # Cursor index for keyboard navigation when the panel is
        # focused (= F4 + j/k path). Indexes into the
        # ``_sorted_visible`` result, NOT the underlying
        # ``_entries`` dict, so visual ordering and cursor ordering
        # stay aligned (= pending entries float to top in both).
        # Clamped on every navigation step + on render so deletes
        # / completions can't leave a dangling out-of-range cursor.
        self._cursor: int = 0
        # Set synchronously in ``_return_focus_to_input`` so the
        # immediately-following ``_refresh()`` renders without the ▌
        # caret. Cleared on the next ``on_focus`` (= when the panel
        # actually regains focus). Exploration finding #6.
        self._focus_releasing: bool = False

    # ── Textual lifecycle ────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        self._static = Static(id="async_stack_text")
        yield self._static

    def on_mount(self) -> None:
        self.set_interval(_TICK_INTERVAL_S, self._tick)
        self._refresh()

    def on_focus(self, event: events.Focus) -> None:
        """Repaint to show the cursor caret on the selected entry."""
        # Reset cursor to top on every fresh focus — predictable
        # entry point regardless of where the user navigated last
        # time. Without this, returning to the panel after entries
        # changed could land on a stale offset.
        self._cursor = 0
        # Clear the focus-releasing flag (= we're back, so the caret
        # should render again on the next paint).
        self._focus_releasing = False
        self._refresh()

    def on_blur(self, event: events.Blur) -> None:
        """Repaint to drop the cursor caret when focus leaves."""
        self._refresh()

    def on_resize(self, event: events.Resize) -> None:
        """Rebuild rows when the panel's own width changes.

        ``_build_lines`` uses ``self.size.width`` to compute
        ``body_budget`` for per-row truncation. Without this hook the
        panel only refreshes on the 1 Hz tick, so opening the side
        panel (Ctrl+B) leaves a window where the rows render
        pre-truncated against the old wider budget — Rich then wraps
        the residue across multiple visual lines instead of eliding
        cleanly. Exploration finding #2, 2026-05-28.
        """
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

    def remove(
        self,
        agent_id: str,
        *,
        terminal: Literal["ok", "aborted", "interrupted"] = "ok",
    ) -> None:
        """Remove ``agent_id``'s entry (= complete / fail / disappear).

        ``terminal="ok"`` (default): immediate unmount — same as the
        historical signature, fully backwards-compatible.

        ``terminal="aborted"`` / ``"interrupted"``: the row briefly
        transitions to a red ``✗ async: <id> (interrupted)`` state for
        ~1.5 s before unmounting. If ``remove()`` is called again for
        the same ``agent_id`` during the flash window (e.g. a forced
        cancel-all sweep), any pending timer is cancelled and the entry
        is unmounted immediately instead.

        Any pending flash timer for ``agent_id`` is always cancelled
        on entry, regardless of ``terminal``, so double-remove calls
        never leak orphan timers.
        """
        # Cancel any pending flash timer before doing anything else.
        self._cancel_flash_timer(agent_id)

        if agent_id not in self._entries:
            return

        if terminal == "ok":
            del self._entries[agent_id]
            self._refresh()
            return

        # Non-ok terminal: transition to flash state and schedule unmount.
        entry = self._entries[agent_id]
        entry.frozen_elapsed_s = time.monotonic() - entry.started_at
        entry.flashing = True
        self._refresh()
        try:
            timer = self.app.set_timer(
                _FLASH_DURATION_S,
                lambda: self._flush_now(agent_id),
            )
            self._flash_timers[agent_id] = timer
        except Exception:
            # If timer scheduling fails (widget torn down, no app),
            # fall through to immediate unmount so we don't leak the row.
            self._flush_now(agent_id)

    def _cancel_flash_timer(self, agent_id: str) -> None:
        """Cancel any pending flash-timer for ``agent_id`` (no-op if none)."""
        timer = self._flash_timers.pop(agent_id, None)
        if timer is None:
            return
        try:
            timer.stop()
        except Exception:
            pass

    def _flush_now(self, agent_id: str) -> None:
        """Immediately unmount ``agent_id``'s entry.

        Called by the deferred flash timer and directly by tests via
        the public surface. Cancels any pending timer first so this
        is idempotent (double-flush is a safe no-op).
        """
        self._cancel_flash_timer(agent_id)
        if agent_id in self._entries:
            del self._entries[agent_id]
            self._refresh()

    def clear(self) -> None:
        """Reset to empty (= no entries).

        Cancels any pending flash timers so deferred unmounts from a
        prior interrupted state don't fire after the panel is cleared
        (e.g. Ctrl+L conversation clear).
        """
        # Cancel all pending flash timers first.
        for aid in list(self._flash_timers.keys()):
            self._cancel_flash_timer(aid)
        if self._entries:
            self._entries.clear()
            self._refresh()

    # ── keyboard navigation (F4-driven focus path) ──────────────────────────

    def move_cursor(self, delta: int) -> None:
        """Move the keyboard cursor by ``delta`` rows (clamped to visible range).

        Visible range = ``_sorted_visible`` count (= up to ``_CAP``,
        sorted with pending entries first). Wraps modulo the count
        so j/k can cycle indefinitely without hitting an edge — same
        idiom as SlashPicker's row navigation. Empty panel: no-op.
        """
        visible = self._sorted_visible()
        if not visible:
            self._cursor = 0
            return
        self._cursor = (self._cursor + delta) % len(visible)
        self._refresh()

    def selected_agent_id(self) -> str:
        """Return the agent_id under the keyboard cursor, or "" when empty.

        Used by the F4-mode ``c`` key handler to populate the
        InputBar with ``/cancel <id>``. Returns the visible-order
        entry at ``_cursor``; clamps to the first entry if the
        cursor drifted out of range (= entries removed since
        last navigation step).
        """
        visible = self._sorted_visible()
        if not visible:
            return ""
        idx = max(0, min(self._cursor, len(visible) - 1))
        return visible[idx][0]

    def on_key(self, event: events.Key) -> None:
        """Keyboard handler — j/k navigate, c prefills /cancel, Esc unfocus.

        Active only when the panel itself has focus (= F4-driven).
        The keys bubble up from the focused panel; consumed ones
        call ``event.stop()`` so they don't fall through to App-level
        bindings (e.g. ``j`` would scroll the right panel otherwise).
        """
        key = event.key
        if key in ("down", "j"):
            self.move_cursor(+1)
            event.stop()
            return
        if key in ("up", "k"):
            self.move_cursor(-1)
            event.stop()
            return
        if key == "c":
            self._prefill_cancel_and_unfocus()
            event.stop()
            return
        if key == "escape":
            self._return_focus_to_input()
            event.stop()
            return

    def _prefill_cancel_and_unfocus(self) -> None:
        """``c`` — pre-populate ``/cancel <id>`` in the InputBar and refocus it.

        The user is on the panel via F4 with a task highlighted;
        ``c`` is the discoverable "cancel this" key. We don't
        actually cancel here — slash-side ``/cancel <id>`` already
        owns the cancel contract — we just stage the command so the
        user reviews + Enters. Empty selection (= no entries) is a
        silent no-op; focus stays on the panel.
        """
        agent_id = self.selected_agent_id()
        if not agent_id:
            return
        try:
            from .input_bar import InputBar
            ib = self.app.query_one("#inputbar", InputBar)
            ta = ib.query_one("#input")
            ta.load_text(f"/cancel {agent_id}")
            # Move cursor to end so Enter sends immediately.
            ta.move_cursor((0, len(f"/cancel {agent_id}")))
            ib.focus_input()
        except Exception:
            pass

    def _return_focus_to_input(self) -> None:
        """Esc — return focus to the InputBar without staging anything.

        ``_focus_releasing`` is flipped BEFORE the actual focus transfer
        so the synchronous ``_refresh()`` repaints the rows without the
        ▌ caret. Without this, Textual's ``on_blur`` only fires on the
        next event-loop tick (= ``has_focus`` stays True during the
        gap), so a casual Esc leaves a ~1-frame ghost caret on screen.
        Exploration finding #6, 2026-05-28.
        """
        self._focus_releasing = True
        self._refresh()
        try:
            from .input_bar import InputBar
            self.app.query_one("#inputbar", InputBar).focus_input()
        except Exception:
            pass

    def snapshot(self) -> list[dict]:
        """Public read of the current entry list for tests / introspection.

        Returns the visible (= sorted, capped, overflow-substituted) view
        as a list of ``{"agent_id", "glyph", "summary", "pending_count",
        "elapsed_s", "is_overflow", "flashing"}`` dicts. Lets callers
        verify ordering + overflow + flash state without reaching into
        private state (= per testing policy public-surface convention).

        Wave-13 T2-2: ``flashing`` is ``True`` while a non-ok terminal
        flash window is active (= row is red, pending unmount).
        """
        out: list[dict] = []
        for agent_id, entry, glyph in self._sorted_visible():
            if entry.frozen_elapsed_s is not None:
                elapsed_s = entry.frozen_elapsed_s
            else:
                elapsed_s = time.monotonic() - entry.started_at
            out.append({
                "agent_id": agent_id,
                "glyph": glyph,
                "summary": entry.summary,
                "pending_count": entry.pending_count,
                "elapsed_s": elapsed_s,
                "is_overflow": False,
                "flashing": entry.flashing,
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
                "flashing": False,
            })
        return out

    def build_lines(self) -> "Text":
        """Public alias for ``_build_lines()``.

        Returns the multi-line ``rich.text.Text`` that would be rendered
        to the Static — one row per visible entry plus an overflow
        indicator when entries exceed ``_CAP``. Exposed so tests can
        inspect the rendered output without calling the private method
        directly — per CLAUDE.md testing policy.
        """
        return self._build_lines()

    @property
    def static_widget(self):
        """The inner Static (or stub) that ``_refresh`` pushes rendered text into.

        ``None`` before ``compose()`` runs. Exposed so tests can inspect
        accumulated ``update()`` calls without reaching into the private
        ``_static`` attribute (per feedback_test_public_surface_not_private_state
        policy). Tests that inject a stub (e.g. ``_StubStatic``) assign it
        directly to ``_static``; this accessor makes that visible via the
        public surface.
        """
        return self._static

    # ── Internal rendering ───────────────────────────────────────────────────

    def _tick(self) -> None:
        if self._entries:
            self._refresh()

    def _sorted_visible(self) -> list[tuple[str, _Entry, str]]:
        """Return up to ``_CAP`` (agent_id, entry, glyph) tuples in display order.

        Sort: pending entries (= ⚑) first, then running entries (= ⟳)
        ordered by elapsed shortest-first, then flashing entries (= ✗)
        last. Flashing rows are transitioning out; pushing them to the
        tail keeps the most actionable rows (pending / running) at the
        top of the narrow strip where the user's eye lands first.

        Within each bucket, ties broken by insertion order (= dict
        iteration stability).
        """
        pending: list[tuple[str, _Entry, str]] = []
        running: list[tuple[str, _Entry, str]] = []
        flashing: list[tuple[str, _Entry, str]] = []
        for agent_id, entry in self._entries.items():
            if entry.flashing:
                flashing.append((agent_id, entry, _GLYPH_INTERRUPTED))
            elif entry.pending_count > 0:
                pending.append((agent_id, entry, _GLYPH_PENDING))
            else:
                running.append((agent_id, entry, _GLYPH_RUNNING))
        running.sort(key=lambda triple: time.monotonic() - triple[1].started_at)
        ordered = pending + running + flashing
        return ordered[:_CAP]

    def _build_lines(self) -> Text:
        """Multi-line Text — one row per visible entry + overflow indicator.

        When the panel itself has focus (= F4 path), the row under
        the keyboard cursor gets a leading ``▌`` caret (= same idiom
        as SlashPicker's selection marker) so the user sees which
        entry the next ``c`` / Enter will target. Unfocused: no
        caret — the panel stays in its ambient single-line mode.
        """
        t = Text()
        if not self._entries:
            self._cursor = 0
            return t
        try:
            total_width = int(getattr(self.size, "width", 0))
        except Exception:
            total_width = 0
        if total_width <= 0:
            total_width = 80
        body_budget = max(20, total_width - _RIGHT_MARGIN_CELLS)
        visible = self._sorted_visible()
        # Clamp cursor to current visible range — entries can
        # disappear between navigation steps (task completed /
        # cancelled), so the cursor might drift out of bounds.
        if visible:
            self._cursor = max(0, min(self._cursor, len(visible) - 1))
        # ``_focus_releasing`` is set synchronously in ``_return_focus_to_input``
        # before focus actually transfers, so a refresh fired in that same
        # call paints the rows WITHOUT the ▌ caret. Without this, the user
        # sees a ~1-frame ghost caret after pressing Esc because Textual
        # dispatches ``on_blur`` only on the next event-loop tick (= the
        # window when ``has_focus`` is still True but the panel is no
        # longer the active focus target). Exploration finding #6, 2026-05-28.
        focused = self.has_focus and not self._focus_releasing
        for i, (agent_id, entry, glyph) in enumerate(visible):
            if i > 0:
                t.append("\n")
            if focused:
                if i == self._cursor:
                    t.append("▌ ", style=_CORAL)
                else:
                    t.append("  ", style=_BG_HEADER)
            self._append_row(t, agent_id, entry, glyph, body_budget)
        overflow = len(self._entries) - len(visible)
        if overflow > 0:
            t.append("\n")
            tail = f"… +{overflow} more (panel for all)"
            t.append(self._truncate_to_cells(tail, body_budget), style="dim " + _TEXT_MUTED)
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

        Wave-13 T2-2: when ``entry.flashing`` is True, bypasses normal
        rendering and emits a fixed red ``✗ async: <id_short> (interrupted)``
        row so the user can see the termination reason before unmount.
        """
        # Flash mode: render a fixed red "interrupted" line and return.
        # The agent_id is middle-elided to a short form (first 8 chars)
        # so the row fits comfortably even on narrow terminals.
        if entry.flashing:
            id_short = _middle_elide_id(agent_id, min(16, body_budget - 24))
            flash_text = f"✗ async: {id_short} (interrupted)"
            t.append(self._truncate_to_cells(flash_text, body_budget), style="bold " + _STATUS_ERROR)
            return

        if entry.frozen_elapsed_s is not None:
            elapsed_s = entry.frozen_elapsed_s
        else:
            elapsed_s = time.monotonic() - entry.started_at
        elapsed_str = _fmt_elapsed(elapsed_s)
        if entry.pending_count > 0:
            elapsed_segment = f"  ({entry.pending_count} pending)"
            elapsed_style = "bold " + _STATUS_WARN
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
        # Wave-11 B#5 + exploration finding #3 (2026-05-28): the
        # summary (= skill name) is the most user-readable signal —
        # ``word_stats_demo`` tells the user WHAT is running, while
        # the agent_id (= timestamp + skill + 4-hex suffix) is mostly
        # a unique-ish handle. So whenever rendering the full summary
        # would require truncating it, middle-elide the agent_id
        # first to free cells (= the head+tail preview still
        # disambiguates among ≤ _CAP simultaneous tasks, while the
        # full skill name lands without the ``word_stats…`` truncation
        # the prior threshold-based logic left behind for narrow-ish
        # widths). Identity floor is _MIN_ELIDED_ID_CELLS; below that
        # the panel is so narrow we accept summary truncation too.
        desired_summary_cells = cell_len(entry.summary or "")
        if entry.summary and summary_budget < desired_summary_cells:
            fixed_cells = cell_len(f"{glyph} async: ") + sep_cells + elapsed_cells
            target_id_cells = max(
                _MIN_ELIDED_ID_CELLS,
                body_budget - fixed_cells - desired_summary_cells,
            )
            if target_id_cells < cell_len(agent_id):
                agent_id = _middle_elide_id(agent_id, target_id_cells)
                prefix = f"{glyph} async: {agent_id}"
                prefix_cells = cell_len(prefix)
                summary_budget = max(
                    0, body_budget - prefix_cells - sep_cells - elapsed_cells,
                )
        summary_display = self._truncate_to_cells(entry.summary, summary_budget)

        t.append(f"{glyph} ", style=glyph_style)
        t.append("async: ", style="dim " + _TEXT_MUTED)
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
        text = self._build_lines()
        self._static.update(text)
        # Wave-11 (mixin migration): cache the rendered Text so the
        # RenderableCacheMixin's ``rendered_text()`` accessor stays
        # in sync with what's on screen. Matches the funnel pattern
        # used by SkillActivityRow / SlashPicker / ReynHeader /
        # ToolCallRow — 5th widget on the shared mixin.
        self._set_rendered_cache(text)


__all__ = ["AsyncStackPanel"]
