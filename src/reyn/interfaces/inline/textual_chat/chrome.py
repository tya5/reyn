"""Composer + bottom-chrome tab-drawer widgets for the Textual chat surface.

The :class:`Composer` is the multi-line Claude-Code-style input (Enter submits,
Shift+Enter newlines). The bottom chrome (Phase 3) is a slim :class:`StatusLine`
of ``model │ agent │ cost │ ctx`` values plus a focusable :class:`MenuBar` (a
``Tabs`` row); opening a menu item expands a ``ContentSwitcher`` drawer whose
per-tab panes are built by :func:`_drawer_child` (an :class:`OptionList` for the
interactive pickers, a plain Rich :class:`Static` for the read-only readouts).
Drawer content is PLACEHOLDER here — real registry wiring is Phase 4. The drawer
container itself is assembled by
:class:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp`.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual`` imports never reach an
always-loaded module.
"""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget
from textual.widgets import (
    OptionList,
    Static,
    Tabs,
    TextArea,
)


class Composer(TextArea):
    """Multi-line Claude-Code-style input: **Enter submits**, **Shift+Enter**
    inserts a newline (the inverse of ``TextArea``'s default), auto-growing up to
    ``MAX_ROWS`` then internally scrolling. Every other key falls through to the
    base ``TextArea`` bindings unchanged."""

    MAX_ROWS = 6

    class Submitted(Message):
        """Posted when the user presses Enter with non-blank content."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def on_mount(self) -> None:
        self.show_line_numbers = False
        self._sync_height()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if self.text.strip():
                self.post_message(self.Submitted(self.text))
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return
        if event.key == "down":
            # ↓ on the composer's LAST line hands focus down to the menu row
            # (CC/reyn chrome flow: the composer is the default focus; ↓ steps
            # into the bottom chrome). On any earlier line ↓ moves the cursor
            # normally (falls through to the base TextArea).
            row, _ = self.cursor_location
            if row >= self.document.line_count - 1:
                menubar = self.app.query(MenuBar)
                if menubar:
                    event.stop()
                    event.prevent_default()
                    menubar.first().focus()
                    return
        await super()._on_key(event)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._sync_height()

    def _sync_height(self) -> None:
        wrapped_rows = max(self.wrapped_document.height, 1)
        self.styles.height = min(wrapped_rows, self.MAX_ROWS)

    def clear_and_reset(self) -> None:
        self.text = ""
        self._sync_height()


# ── Phase 3: bottom-chrome tab-drawer ────────────────────────────────────────
# Default collapsed = a slim status-values line + a focusable menu row. Pressing
# ↓ from the composer focuses the menu; opening an item expands a drawer
# DOWNWARD. Interactive panes are Textual OptionLists (Model/Agent/History/Menu —
# keyboard selection); static readouts are plain Rich in a Static (Cost/Ctx/Help)
# — the "Textual only where there is a selection" split. Content is PLACEHOLDER
# here; wiring to reyn's real registries is Phase 4.

_MENU_TABS: "list[tuple[str, str]]" = [
    ("model", "Model"),
    ("agent", "Agent"),
    ("history", "History"),
    ("cost", "Cost"),
    ("ctx", "Ctx"),
    ("menu", "Menu"),
    ("help", "Help"),
]


def _drawer_child(tab_id: str) -> Widget:
    """Build the drawer pane for a menu item: an :class:`OptionList` for the
    interactive pickers (Model/Agent/History/Menu — keyboard selection), a plain
    Rich :class:`Static` for the read-only readouts (Cost/Ctx/Help).

    The content is PLACEHOLDER (Phase 3 ports the STRUCTURE only). Phase 4 swaps
    each pane's body for its canonical reyn registry (model/agent registries,
    session history, cost/token trackers, the slash-command registry, keybindings).
    """
    if tab_id == "model":
        return OptionList(
            "sonnet   · active", "opus", "haiku", "gemini-2.5-flash-lite",
            id="model",
        )
    if tab_id == "agent":
        return OptionList(
            "default   · active", "planner", "reviewer", "researcher",
            id="agent",
        )
    if tab_id == "history":
        return OptionList(
            "1 · (placeholder) previous conversation turn…",
            "2 · (placeholder) another previous turn…",
            id="history",
        )
    if tab_id == "menu":
        return OptionList(
            "/model — switch model",
            "/agent — switch agent",
            "/clear — clear conversation",
            "/quit — exit",
            id="menu",
        )
    if tab_id == "cost":
        return Static(
            Text.from_markup(
                "[b]Usage · this session[/b] (placeholder)\n"
                "  turn    $0.0000\n"
                "  total   $0.0000\n"
                "  tokens  0 in · 0 out"
            ),
            id="cost",
        )
    if tab_id == "ctx":
        return Static(
            Text.from_markup(
                "[b]Context window[/b] (placeholder)\n"
                "  0 / 200 000 tokens  (0%)\n"
                "  ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁"
            ),
            id="ctx",
        )
    return Static(
        Text.from_markup(
            "[b]Shortcuts[/b]\n"
            "  enter send · shift+enter newline\n"
            "  ↓ focus menu · ← → move · enter open · esc close"
        ),
        id="help",
    )


class StatusLine(Static):
    """Slim bottom status-values line — plain, not rich. Mirrors reyn's inline
    REPL bottom toolbar (``model │ agent │ cost │ ctx``), which sits BELOW the
    input — matching Claude Code and reyn (not a top-docked line). The menu
    *items* live in the focusable :class:`MenuBar` row just below it."""


class MenuBar(Tabs):
    """Focusable horizontal menu row (the collapsed bottom chrome). ``← →`` move
    the highlight, ``Enter`` opens the highlighted item's drawer, and ``↑``/``Esc``
    close it and hand focus back to the composer. Unlike a plain :class:`Tabs`,
    moving the highlight does NOT open anything — opening is an explicit ``Enter``
    (the base ``Tabs.TabActivated`` fired on arrow-move is intentionally ignored)."""

    class Selected(Message):
        """Posted on an explicit ``Enter`` (opening ``tab_id``) or on ``↑``/``Esc``
        (the sentinel ``"__close__"``)."""

        def __init__(self, tab_id: str) -> None:
            self.tab_id = tab_id
            super().__init__()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if self.active:
                self.post_message(self.Selected(self.active))
            return
        if event.key in ("up", "escape"):
            event.stop()
            event.prevent_default()
            self.post_message(self.Selected("__close__"))
            return
        await super()._on_key(event)
