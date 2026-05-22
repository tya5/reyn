"""StickyStatus — a 1-line status bar pinned to the bottom of the conversation pane.

Shows a live "currently happening" message with an elapsed timer that updates
every 0.1 s. Auto-hides when nothing is being reported. Intended to replace
inline "⟳ thinking…" log lines with a non-intrusive persistent indicator.

Usage::

    status = StickyStatus(id="sticky-status")
    await conversation.mount(status)
    status.show("thinking", kind="thinking")
    # … later …
    status.hide()
"""
from __future__ import annotations

import time

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import Static

from reyn.chat.tui._palette import _AMBER, _CORAL

_TICK_INTERVAL_S = 0.1  # elapsed timer refresh rate

_GLYPHS: dict[str, str] = {
    "thinking": "⟳",
    "general": "●",
    # Wave-10 I-F1: ``"error"`` was passed by 7+ call sites
    # (``app_outbox._show_transient_status`` for ``/copy`` failures,
    # ``ws_client`` reconnection notices, right_panel preview-error
    # surface) but had no entry here, so ``show()`` silently
    # fell back to ``"thinking"`` (= the ⟳ amber glyph). An error
    # message rendered with the same shape and color as the
    # ``⟳ thinking…`` live indicator was easy to read as "the agent
    # is still working" rather than "an action failed". ``✗`` is
    # the same glyph used by ToolCallRow / SkillActivityRow for
    # failure terminals, so the cross-surface vocabulary stays
    # uniform.
    "error": "✗",
}

# Wave-10 G-F8 + I-F8: overwrite-priority hierarchy.
#
# ``show()`` was previously unconditional — a low-priority transient
# (turn-position breadcrumb, boundary hint) would silently overwrite
# a load-bearing live indicator (``⟳ thinking…``). Two visible
# failures resulted:
#
#   - I-F8: Ctrl+P / Ctrl+N during an LLM call replaced the ⟳ thinking
#     indicator with ``↑ turn 3 / 8``, making the agent appear frozen
#     until the next outbox event arrived.
#   - G-F8: ``_maybe_warn_about_trimmed_history`` wrote to the sticky,
#     then ``_flash_turn_position`` fired in the same call frame and
#     overwrote the warning before the user could read it — the
#     trim-warning was effectively invisible.
#
# This map gives each ``kind`` a numeric priority. ``show()`` suppresses
# its own call when the requested ``kind`` is LOWER than the currently
# active ``kind``. Same-or-higher priority overwrites freely (= the
# expected behaviour for genuine status updates, e.g. ``thinking`` →
# ``thinking`` body update).
_KIND_PRIORITY: dict[str, int] = {
    # Live load-bearing state — must not be displaced by transients.
    "thinking": 100,
    # Wave-10 I-F1: critical transient signal — an action failed and
    # the user needs to notice. Higher than ``general`` (= routine
    # breadcrumbs / turn-position flashes shouldn't overwrite an
    # error the user hasn't read yet) but lower than ``thinking`` —
    # if the LLM is mid-call, the live indicator stays visible and
    # the error log line in the conv pane remains the load-bearing
    # record.
    "error": 80,
    # Transient flashes / breadcrumbs / one-shot notices.
    "general": 50,
}


class StickyStatus(Static):
    """A 1-line sticky status bar with a live elapsed timer.

    Pinned to the bottom of whatever container it lives in (above InputBar).
    Hidden by default; call show() to activate and hide() to dismiss.

    Rendering while active::

        ⟳ thinking · 1.4s

    The glyph is rendered in coral; the body text is dim italic.
    Elapsed is formatted with 1 decimal place, minimum 0.1 s.
    """

    DEFAULT_CSS = """
    StickyStatus {
        display: none;
        height: 1;
        padding: 0 1;
        dock: bottom;
        background: transparent;
    }
    StickyStatus.active {
        display: block;
    }
    """

    can_focus = False

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", id=id)
        self._active: bool = False
        self._kind: str = "thinking"
        self._glyph: str = _GLYPHS["thinking"]
        self._body: str = ""
        self._start: float = 0.0

    def on_mount(self) -> None:
        """Start the 0.1 s elapsed timer tick."""
        self.set_interval(_TICK_INTERVAL_S, self._tick)

    # ── public API ────────────────────────────────────────────────────────────

    def show(self, text: str, kind: str = "thinking") -> None:
        """Activate the status bar with the given body text and glyph kind.

        Wave-10 G-F8 + I-F8: when the sticky is already active with a
        higher-priority ``kind``, a lower-priority ``show()`` is
        silently suppressed (= the call is a no-op). This protects
        load-bearing live indicators (``⟳ thinking…``) from being
        displaced by transient breadcrumbs (turn-position flash,
        boundary hint). Same-or-higher priority overwrites freely.

        ``hide()`` is unconditional — explicit dismissal always wins.
        """
        new_kind = kind if kind in _GLYPHS else "thinking"
        if self._active:
            current_priority = _KIND_PRIORITY.get(self._kind, 0)
            new_priority = _KIND_PRIORITY.get(new_kind, 0)
            if new_priority < current_priority:
                return
        self._kind = new_kind
        self._glyph = _GLYPHS[self._kind]
        self._start = time.monotonic()
        self._active = True
        self.add_class("active")
        self.update_text(text)

    def update_text(self, text: str) -> None:
        """Update the body text without resetting the elapsed timer."""
        self._body = text
        self._repaint()

    def hide(self) -> None:
        """Deactivate and hide the status bar."""
        self._active = False
        self.remove_class("active")

    def snapshot(self) -> dict:
        """Return the current display state for inspection by callers / tests.

        Exposes ``{"active": bool, "body": str, "kind": str}`` so callers
        (and Tier 2 tests) can verify the sticky's state through a public
        surface rather than reading the ``_active`` / ``_body`` / ``_kind``
        private attributes directly (= ``testing.ja.md`` anti-pattern).
        """
        return {"active": self._active, "body": self._body, "kind": self._kind}

    # ── internal ──────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        if not self._active:
            return
        self._repaint()

    def _repaint(self) -> None:
        elapsed = max(0.1, time.monotonic() - self._start)
        # On 8-color terminals, hex _CORAL (#C8553D) degrades to ANSI bright
        # red — confusable with error indicators. The thinking sticky shows
        # while the agent is working, so route its glyph through _AMBER
        # (which degrades to ANSI yellow / bright yellow) — neutrally
        # signalling "in progress" rather than "alert". The error kind
        # WANTS to read as alert, so bold red (#aa6666). General keeps
        # _CORAL since it's a transient flash, not the load-bearing "is
        # the agent working?" indicator.
        if self._kind == "thinking":
            glyph_color = _AMBER
        elif self._kind == "error":
            glyph_color = "bold #aa6666"
        else:
            glyph_color = _CORAL
        # Total cells in the fixed-width suffix segments so we can truncate
        # the body when narrow terminals would otherwise clip the
        # ``Ctrl+C cancel`` hint behind the right edge.
        glyph_cells = cell_len(self._glyph) + 1  # ``<glyph> ``
        elapsed_suffix = f" · {elapsed:.1f}s"
        elapsed_cells = cell_len(elapsed_suffix)
        cancel_suffix = "  · Ctrl+C cancel" if self._kind == "thinking" else ""
        cancel_cells = cell_len(cancel_suffix)
        # Padding 0 1 → 2 cells consumed; another 1-cell safety margin
        # keeps the body off the right edge even if Textual reserves
        # something additional (cursor / scrollbar on certain themes).
        chrome_cells = glyph_cells + elapsed_cells + cancel_cells + 2 + 1
        available = max(0, int(getattr(self.size, "width", 0)) - chrome_cells)
        body = self._body
        if available > 0 and cell_len(body) > available:
            # Truncate by cells (CJK / wide-char aware) and append the
            # ellipsis. ``available - 1`` reserves the ellipsis cell.
            out_chars: list[str] = []
            used = 0
            for ch in body:
                w = cell_len(ch)
                if used + w > max(0, available - 1):
                    break
                out_chars.append(ch)
                used += w
            body = "".join(out_chars) + "…"
        t = Text()
        t.append(self._glyph + " ", style=glyph_color)
        t.append(body, style="dim italic")
        t.append(elapsed_suffix, style="dim italic")
        if cancel_suffix:
            t.append(cancel_suffix, style="dim italic")
        self.update(t)
