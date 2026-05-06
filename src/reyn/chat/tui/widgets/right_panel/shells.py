"""Inner Textual widget classes used by RightPanel.

These are intentionally small wrappers around Static / Widget that:

  * delegate render() back to the parent ``RightPanel`` so we never store
    intermediate Rich strings in the visual pipeline (avoids type drift bugs);
  * own preview-pane scroll/clear plumbing for the docs tab.

Kept here to keep ``__init__.py`` focused on the dispatcher itself.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import ComposeResult, RenderResult
from textual.widget import Widget
from textual.widgets import Label, RichLog, Static, Tabs
from textual.widgets._tabs import Underline as _Underline

from .base import _esc, logger

if TYPE_CHECKING:
    from . import RightPanel


class _TopTabs(Tabs):
    """Tabs with the underline indicator docked to the top instead of the bottom."""

    def on_mount(self) -> None:
        # Inline styles have highest priority — overrides DEFAULT_CSS dock:bottom
        self.query_one(_Underline).styles.dock = "top"


class _PanelHeader(Static):
    """Fixed header strip — 1 line of text + symmetric padding = natural 3 rows."""

    DEFAULT_CSS = """
    _PanelHeader {
        background: #1a1a1a;
        color: #aaaaaa;
        padding: 0 2;
        border-bottom: solid #333333;
    }
    """

    def __init__(self, panel: "RightPanel", **kwargs) -> None:
        super().__init__("", **kwargs)
        self._panel = panel

    def render(self) -> RenderResult:
        try:
            return self._panel._panel_header_markup()
        except Exception as exc:
            logger.warning("right_panel header render failed: %s", exc)
            return ""

    def invalidate(self) -> None:
        self._layout_cache.clear()
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
            return self._panel._panel_markup()
        except Exception as exc:
            logger.warning("right_panel content render failed: %s", exc)
            return ""

    def invalidate(self) -> None:
        """Force a re-render on the next frame."""
        self._layout_cache.clear()
        self.refresh(layout=True)


class _PreviewPane(Widget):
    """Lower-half preview pane toggled with 'f'.

    Generic: any tab can populate it. Currently docs tab shows the focused
    file's Markdown content. Other tabs may use it in future.
    """

    can_focus = True

    DEFAULT_CSS = """
    _PreviewPane {
        display: none;
        height: 1fr;
        border-top: tall #2a2a2a;
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
        border-top: tall $primary;
    }
    _PreviewPane:focus #preview-header {
        color: $primary;
    }
    _PreviewPane RichLog {
        background: transparent;
        height: 1fr;
        padding: 0 1;
        overflow-x: auto;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._current_path: Path | None = None

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
        elif event.key == "l":
            event.prevent_default()
            event.stop()
            self.scroll_col(+1)
        elif event.key == "h":
            event.prevent_default()
            event.stop()
            self.scroll_col(-1)

    def show_markdown(self, path: Path) -> None:
        from rich.markdown import Markdown as RichMarkdown
        self._current_path = path
        try:
            log = self.query_one("#preview-log", RichLog)
            log.clear()
            log.write(RichMarkdown(path.read_text(encoding="utf-8")))
            log.scroll_home(animate=False)
            self._update_header()
        except Exception as exc:
            logger.warning("right_panel preview show_markdown(%s) failed: %s", path, exc)

    def scroll_line(self, delta: int) -> None:
        try:
            log = self.query_one("#preview-log", RichLog)
            log.scroll_to(y=log.scroll_y + delta, animate=False)
        except Exception as exc:
            logger.warning("right_panel preview scroll_line failed: %s", exc)

    def scroll_col(self, delta: int) -> None:
        try:
            log = self.query_one("#preview-log", RichLog)
            log.scroll_to(x=log.scroll_x + delta, animate=False)
        except Exception as exc:
            logger.warning("right_panel preview scroll_col failed: %s", exc)

    def clear(self) -> None:
        self._current_path = None
        try:
            self.query_one("#preview-log", RichLog).clear()
            self._update_header()
        except Exception as exc:
            logger.warning("right_panel preview clear failed: %s", exc)

    def _update_header(self) -> None:
        name = _esc(self._current_path.name) if self._current_path else "—"
        try:
            self.query_one("#preview-header", Label).update(
                f"  {name}  │  j↓  k↑  h←  l→"
            )
        except Exception as exc:
            logger.warning("right_panel preview header update failed: %s", exc)


class _TabContent(Widget):
    """Container below the tab bar: header + scroll area + preview pane."""

    DEFAULT_CSS = """
    _TabContent {
        height: 1fr;
        layout: vertical;
    }
    """


__all__ = ["_TopTabs", "_PanelHeader", "_PanelContent", "_PreviewPane", "_TabContent"]
