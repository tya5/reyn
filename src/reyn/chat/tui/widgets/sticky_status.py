"""StickyStatus — a 1-line status bar pinned to the bottom of the conversation pane.

Shows a currently-happening message. Auto-hides when nothing is being
reported. Used for error notices, turn-position flashes, and "awaiting your
answer" badges. The ``⟳ thinking…`` kind has been replaced by the inline
Braille spinner (``InlineThinkingRow``).

Usage::

    status = StickyStatus(id="sticky-status")
    await conversation.mount(status)
    status.show("⚑ awaiting your answer", kind="general")
    # … later …
    status.hide()
"""
from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import Static

from reyn.chat.tui._palette import _CORAL, _RED_MUTED

_TICK_INTERVAL_S = 0.1  # elapsed timer refresh rate

_GLYPHS: dict[str, str] = {
    # ``"thinking"`` was removed — the inline Braille spinner
    # (``InlineThinkingRow``) now handles the LLM-in-flight indicator
    # directly in the conv pane flow. The sticky is no longer used for
    # thinking state.
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
    # ``"thinking"`` removed — the inline Braille spinner
    # (``InlineThinkingRow``) replaces the sticky thinking indicator.
    # Wave-10 I-F1: critical transient signal — an action failed and
    # the user needs to notice. Higher than ``general`` (= routine
    # breadcrumbs / turn-position flashes shouldn't overwrite an
    # error the user hasn't read yet).
    "error": 80,
    # Transient flashes / breadcrumbs / one-shot notices.
    "general": 50,
}

# W13 A#7: terminal-failure priority (above thinking=100). Applied when
# ``show(..., kind="error", terminal=True)`` is called — a budget-exceeded
# or auth-failed event mid-streaming must not be suppressed by the live
# ⟳ thinking indicator. ``terminal=False`` (the default) keeps the
# existing priority=80 so transient errors stay below thinking.
_TERMINAL_ERROR_PRIORITY: int = 110


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
        self._kind: str = "general"
        self._glyph: str = _GLYPHS["general"]
        self._body: str = ""
        # W13 A#7: track the *effective* priority (= the value used for
        # suppression decisions) separately from the kind-lookup so
        # terminal=True calls (priority=110) are reflected in snapshot().
        self._current_priority: int = 0

    def on_mount(self) -> None:
        """Start the 0.1 s elapsed timer tick."""
        self.set_interval(_TICK_INTERVAL_S, self._tick)

    # ── public API ────────────────────────────────────────────────────────────

    def show(self, text: str, kind: str = "general", *, terminal: bool = False) -> None:
        """Activate the status bar with the given body text and glyph kind.

        Wave-10 G-F8 + I-F8: when the sticky is already active with a
        higher-priority ``kind``, a lower-priority ``show()`` is
        silently suppressed (= the call is a no-op). This protects
        load-bearing error notices from being displaced by transient
        breadcrumbs (turn-position flash, boundary hint). Same-or-higher
        priority overwrites freely.

        W13 A#7: ``terminal=True`` raises the effective priority to
        ``_TERMINAL_ERROR_PRIORITY`` (110 > thinking=100) so a
        terminal failure mid-streaming (budget exceeded, auth error)
        is NOT suppressed by the live ⟳ thinking indicator. Only
        valid when ``kind="error"``; ignored for other kinds.

        ``hide()`` is unconditional — explicit dismissal always wins.

        Note: ``kind="thinking"`` is no longer valid — use
        ``ConversationView.start_thinking()`` for the inline spinner.
        """
        new_kind = kind if kind in _GLYPHS else "general"
        # W13 A#7: terminal error overrides thinking priority.
        if terminal and new_kind == "error":
            new_priority = _TERMINAL_ERROR_PRIORITY
        else:
            new_priority = _KIND_PRIORITY.get(new_kind, 0)
        if self._active:
            if new_priority < self._current_priority:
                return
        self._kind = new_kind
        self._glyph = _GLYPHS[self._kind]
        self._active = True
        self._current_priority = new_priority
        self.add_class("active")
        self.update_text(text)

    def update_text(self, text: str) -> None:
        """Update the body text without resetting the elapsed timer."""
        self._body = text
        self._repaint()

    def hide(self) -> None:
        """Deactivate and hide the status bar."""
        self._active = False
        self._current_priority = 0
        self.remove_class("active")

    @property
    def glyph(self) -> str:
        """Public read of the resolved glyph character for the current kind.

        Returns the string that ``_repaint`` writes at the start of the
        rendered Text (e.g. ``"✗"`` for kind="error", ``"●"`` for
        kind="general"). Exposed so tests can verify the glyph without
        reading the private ``_glyph`` slot — per CLAUDE.md testing
        policy.
        """
        return self._glyph

    def snapshot(self) -> dict:
        """Return the current display state for inspection by callers / tests.

        Exposes ``{"active": bool, "body": str, "kind": str,
        "priority": int}`` so callers (and Tier 2 tests) can verify the
        sticky's state through a public surface rather than reading the
        ``_active`` / ``_body`` / ``_kind`` private attributes directly
        (= ``testing.ja.md`` anti-pattern).

        W13 A#7: ``priority`` is the numeric priority that would be used
        to decide whether the current kind could be displaced. Useful for
        test assertions like ``snapshot()["priority"] > 100``.
        """
        return {
            "active": self._active,
            "body": self._body,
            "kind": self._kind,
            # W13 A#7: return the *effective* priority (= the value set at
            # show()-time, which may be _TERMINAL_ERROR_PRIORITY=110 for
            # terminal errors) rather than the kind-table lookup so tests
            # can assert ``snapshot()["priority"] == 110`` for terminal errors.
            "priority": self._current_priority,
        }

    # ── internal ──────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        if not self._active:
            return
        self._repaint()

    def _repaint(self) -> None:
        # On 8-color terminals, hex _CORAL (#C8553D) degrades to ANSI bright
        # red — confusable with error indicators. The error kind WANTS to
        # read as alert, so bold muted red (_RED_MUTED). General keeps _CORAL
        # since it's a transient flash. (The ``"thinking"`` kind is no longer
        # handled here — the inline Braille spinner replaced it.)
        if self._kind == "error":
            glyph_color = f"bold {_RED_MUTED}"
        else:
            glyph_color = _CORAL
        # Chrome budget: glyph + 1 space + padding 0 1 (= 2 cells) + 1 margin.
        glyph_cells = cell_len(self._glyph) + 1  # ``<glyph> ``
        chrome_cells = glyph_cells + 2 + 1
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
        self.update(t)
