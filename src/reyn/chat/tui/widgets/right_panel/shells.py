"""Inner Textual widget classes used by RightPanel.

These are intentionally small wrappers around Static / Widget that:

  * delegate render() back to the parent ``RightPanel`` so we never store
    intermediate Rich strings in the visual pipeline (avoids type drift bugs);
  * own preview-pane scroll/clear plumbing for the docs / events / memory tabs.

Kept here to keep ``__init__.py`` focused on the dispatcher itself.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.app import ComposeResult, RenderResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, RichLog, Static

from .base import _esc, logger

if TYPE_CHECKING:
    from . import RightPanel


class _PanelHeader(Static):
    """Fixed header strip — 1 line of text + bottom border."""

    DEFAULT_CSS = """
    _PanelHeader {
        background: #1a1a1a;
        color: #aaaaaa;
        padding: 0 1;
        border-bottom: solid #333333;
    }
    """

    def __init__(self, panel: "RightPanel", **kwargs) -> None:
        super().__init__("", **kwargs)
        self._panel = panel

    def render(self) -> RenderResult:
        try:
            markup = self._panel._panel_header_markup()
            t = Text.from_markup(markup)
            w = self.content_region.width
            if w > 0:
                t.truncate(w)
            return t
        except Exception as exc:
            logger.warning("right_panel header render failed: %s", exc)
            return ""

    def invalidate(self) -> None:
        self.refresh()


class _PanelContent(Static):
    """Static subclass that delegates render() to the parent RightPanel.

    By overriding render() instead of calling update(), we avoid any
    intermediate storage that could end up in Textual's visual pipeline
    with the wrong type.
    """

    def __init__(self, panel: "RightPanel", **kwargs) -> None:
        super().__init__("", **kwargs)
        self._panel = panel

    def render(self) -> RenderResult:
        try:
            markup = self._panel._panel_markup()
            if not isinstance(markup, str):
                return markup
            width = self.content_region.width
            if width <= 0:
                return Text.from_markup(markup)
            # Truncate each line independently so long values
            # (event types, file names, …) don't wrap into multiple
            # rows and split words mid-line. A single line whose
            # markup fails to parse (orphaned ``[/]`` from a leaked
            # newline upstream, for example) falls back to plain text
            # rather than killing the whole tab render via the outer
            # ``except`` — losing one row's color is OK, the entire
            # panel going blank is not.
            parts: list[Text] = []
            for line_markup in markup.split("\n"):
                try:
                    line_t = Text.from_markup(line_markup)
                except Exception:
                    line_t = Text(line_markup)
                line_t.truncate(width, overflow="ellipsis")
                parts.append(line_t)
            return Text("\n").join(parts)
        except Exception as exc:
            logger.warning("right_panel content render failed: %s", exc)
            return ""

    def invalidate(self) -> None:
        """Force a re-render on the next frame."""
        self.refresh(layout=True)


class _PanelTop(Widget):
    """Wraps _PanelHeader + #panel-scroll so the focus indicator can be drawn
    around the upper content region without including the tab strip below.

    Focus model:
      * Tabs has focus  → ``x-focused`` set by the parent → coral ring
      * _PanelTop has focus (via close-preview restore) → same ``x-focused``
        ring; key events bubble to RightPanel.on_key so j/k/space/Tab
        all keep working.
    """

    can_focus = True

    DEFAULT_CSS = """
    _PanelTop {
        height: 1fr;
        layout: vertical;
        border-left: solid #2a2a2a;
    }
    _PanelTop.x-focused {
        border-left: solid $primary;
    }
    """


class _TabContent(Widget):
    """Container holding _PanelTop + _PreviewPane (sits below the tab strip)."""

    DEFAULT_CSS = """
    _TabContent {
        height: 1fr;
        layout: vertical;
    }
    """


class _PreviewPane(Widget):
    """Lower-half preview pane toggled with Space (on docs / events / memory).

    Generic: any tab can populate it via ``show_markdown(path)`` (docs) or
    ``show_text(title, renderable)`` (events JSON, memory body).

    When the preview pane itself has focus, pressing Space posts a
    :class:`CloseRequested` message to the parent RightPanel, which then
    closes the preview. This keeps the toggle key consistent across
    "tabs focused" and "preview focused" states.
    """

    can_focus = True

    class CloseRequested(Message):
        """Posted when the user presses Space inside the focused preview."""

        pass

    DEFAULT_CSS = """
    _PreviewPane {
        display: none;
        height: 1fr;
        border-top: solid #2a2a2a;
        border-left: solid #2a2a2a;
        layout: vertical;
    }
    _PreviewPane.preview-visible {
        display: block;
    }
    _PreviewPane #preview-header {
        height: 1;
        color: #555555;
        background: #1a1a1a;
        padding: 0 1;
    }
    _PreviewPane:focus {
        border-top: solid $primary;
        border-left: solid $primary;
    }
    _PreviewPane:focus #preview-header {
        color: $primary;
    }
    _PreviewPane RichLog {
        background: transparent;
        height: 1fr;
        padding: 0 1;
        overflow-x: auto;
        scrollbar-color: #2a2a2a;
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $primary;
        scrollbar-background: transparent;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_path: Path | None = None
        self._current_title: str = ""

    def compose(self) -> ComposeResult:
        yield Label("", id="preview-header")
        yield RichLog(id="preview-log", markup=False, highlight=False, auto_scroll=False)

    def on_key(self, event) -> None:
        if event.key == "j":
            event.prevent_default()
            event.stop()
            self.scroll_line(+1)
        elif event.key == "k":
            event.prevent_default()
            event.stop()
            self.scroll_line(-1)
        elif event.key in ("J", "N", "n"):
            # Peek-next-item without leaving the preview: walk up to the
            # parent RightPanel and advance the active tab's cursor by +1.
            # The cursor-move helper re-renders the preview itself when
            # the pane is visible (see `_docs_move` et al.), so this stays
            # a single keystroke for "show me the next doc / event /
            # memory entry / agent run". `n` is the bonus alias because
            # some terminals (esp. on Windows / over SSH) don't deliver
            # Shift+letter as a distinct key event.
            event.prevent_default()
            event.stop()
            self._navigate_parent_cursor(+1)
        elif event.key in ("K", "P", "p"):
            # Peek-previous-item, mirror of J / n above.
            event.prevent_default()
            event.stop()
            self._navigate_parent_cursor(-1)
        elif event.key == "d":
            # vim-style half-page down — bigger jump than `j` for the
            # long event-YAML dumps where line-by-line is tedious.
            event.prevent_default()
            event.stop()
            self.scroll_half_page(+1)
        elif event.key == "u":
            # vim-style half-page up.
            event.prevent_default()
            event.stop()
            self.scroll_half_page(-1)
        elif event.key == "l":
            event.prevent_default()
            event.stop()
            self.scroll_col(+1)
        elif event.key == "h":
            event.prevent_default()
            event.stop()
            self.scroll_col(-1)
        elif event.key == "space":
            # Space when the preview itself has focus: ask the parent to
            # close the preview pane (consistent with the panel-tabs Space
            # toggle).
            event.prevent_default()
            event.stop()
            self.post_message(self.CloseRequested())

    def _navigate_parent_cursor(self, delta: int) -> None:
        """Walk up to the parent ``RightPanel`` and move its tab cursor.

        Dispatches per active tab — docs / events / memory / agents each
        have their own cursor + cursor-move helper. The helper takes care
        of refreshing the preview when the pane is visible, so we don't
        call ``_update_preview`` directly here. Tabs without a cursor
        concept (keys / cost) are no-ops.
        """
        # late import to avoid circular: shells.py is imported by __init__.py.
        from . import RightPanel
        parent = None
        for ancestor in self.ancestors_with_self:
            if isinstance(ancestor, RightPanel):
                parent = ancestor
                break
        if parent is None:
            return
        tab = parent.panel_type
        if tab == "docs":
            parent._docs_move(delta)
        elif tab == "events":
            parent._events_move(delta)
        elif tab == "memory":
            parent._memory_move(delta)
        elif tab == "agents":
            parent._agents_move(delta)

    def show_markdown(self, path: Path) -> None:
        from rich.markdown import Markdown as RichMarkdown
        self._current_path = path
        self._current_title = path.name
        try:
            log = self.query_one("#preview-log", RichLog)
            log.clear()
            log.write(RichMarkdown(path.read_text(encoding="utf-8")))
            log.scroll_home(animate=False)
            self._update_header()
        except Exception as exc:
            logger.warning("right_panel preview show_markdown(%s) failed: %s", path, exc)

    def show_text(self, title: str, renderable: Any) -> None:
        """Generic preview: any Rich-renderable content under a custom title.

        Used by the events tab (JSON dumps) and memory tab (entry bodies).
        Differs from show_markdown() by accepting any renderable + a title
        string that doesn't have to be a filesystem path.
        """
        self._current_path = None
        self._current_title = title
        try:
            log = self.query_one("#preview-log", RichLog)
            log.clear()
            log.write(renderable)
            log.scroll_home(animate=False)
            self._update_header()
        except Exception as exc:
            logger.warning("right_panel preview show_text failed: %s", exc)

    def scroll_line(self, delta: int) -> None:
        try:
            log = self.query_one("#preview-log", RichLog)
            log.scroll_to(y=log.scroll_y + delta, animate=False)
        except Exception as exc:
            logger.warning("right_panel preview scroll_line failed: %s", exc)

    def scroll_half_page(self, direction: int) -> None:
        """Scroll by half the visible window height. Used by the d/u keys.

        ``direction`` is +1 (down) or -1 (up). When the size isn't yet
        known (= layout still pending), falls back to a 10-line jump
        which is a reasonable default for the typical preview pane.
        """
        try:
            log = self.query_one("#preview-log", RichLog)
            visible = max(1, log.size.height - 1)
            jump = max(1, visible // 2) * direction
            log.scroll_to(y=log.scroll_y + jump, animate=False)
        except Exception as exc:
            logger.warning(
                "right_panel preview scroll_half_page failed: %s", exc,
            )

    def scroll_col(self, delta: int) -> None:
        try:
            log = self.query_one("#preview-log", RichLog)
            log.scroll_to(x=log.scroll_x + delta, animate=False)
        except Exception as exc:
            logger.warning("right_panel preview scroll_col failed: %s", exc)

    def clear(self) -> None:
        self._current_path = None
        self._current_title = ""
        try:
            self.query_one("#preview-log", RichLog).clear()
            self._update_header()
        except Exception as exc:
            logger.warning("right_panel preview clear failed: %s", exc)

    def _update_header(self) -> None:
        if self._current_title:
            name = _esc(self._current_title)
        elif self._current_path:
            name = _esc(self._current_path.name)
        else:
            name = "—"
        try:
            # Shortened from ``J/n=next K/p=prev`` (15 cells) to
            # ``J/K=next/prev`` (14 cells minus 1 — but more importantly,
            # the trailing prev-shortcut hint is no longer the first
            # thing to clip at default panel widths. The earlier wording
            # truncated to ``K/p=pr…`` at 44-cell minimum panel widths,
            # hiding the prev-item discoverability cue entirely.
            self.query_one("#preview-header", Label).update(
                f"  {name}  │  j↓ k↑ d⇊ u⇈ h← l→  J/K=next/prev"
            )
        except Exception as exc:
            logger.warning("right_panel preview header update failed: %s", exc)


__all__ = ["_PanelHeader", "_PanelContent", "_PanelTop", "_PreviewPane", "_TabContent"]
