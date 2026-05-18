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

from rich.text import Text
from textual.widgets import Static

from reyn.chat.tui._palette import _AMBER, _CORAL

_TICK_INTERVAL_S = 0.1  # elapsed timer refresh rate

_GLYPHS: dict[str, str] = {
    "thinking": "⟳",
    "tool": "⚙",
    "general": "●",
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
        """Activate the status bar with the given body text and glyph kind."""
        self._kind = kind if kind in _GLYPHS else "thinking"
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
        t = Text()
        # On 8-color terminals, hex _CORAL (#C8553D) degrades to ANSI bright
        # red — confusable with error indicators. The thinking sticky shows
        # while the agent is working, so route its glyph through _AMBER
        # (which degrades to ANSI yellow / bright yellow) — neutrally
        # signalling "in progress" rather than "alert". Other kinds (tool,
        # general) keep _CORAL since they're typically transient flashes,
        # not the load-bearing "is the agent working?" indicator.
        glyph_color = _AMBER if self._kind == "thinking" else _CORAL
        t.append(self._glyph + " ", style=glyph_color)
        t.append(self._body, style="dim italic")
        t.append(f" · {elapsed:.1f}s", style="dim italic")
        if self._kind == "thinking":
            t.append("  · Ctrl+C cancel", style="dim italic")
        self.update(t)
