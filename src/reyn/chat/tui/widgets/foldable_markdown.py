"""FoldableMarkdown — toggleable preview/full long-reply widget.

Collapsed (default): renders the preview (first N rendered lines) and a
dim "▶ N more lines · click or F8 / /expand to show" hint footer.
Expanded: renders full Markdown and a dim "▼ collapse" footer.

Toggle via on_click, the public toggle() method, or external lookup
(= ConversationView.toggle_last_foldable()).
"""
from __future__ import annotations

from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Label, Static

from reyn.chat.tui._palette import _CORAL

_BODY_INDENT_COLS = 7  # must match conversation.py _BODY_INDENT_COLS


class FoldableMarkdown(Widget):
    """Toggleable preview / full long-reply display widget.

    Collapsed (default): renders preview (first N rendered lines) + a
    dim "▶ N more lines · click or F8 / /expand to show" hint footer.
    Expanded: renders full Markdown + a dim "▼ collapse" footer.

    Toggle via on_click, public toggle() method, or external lookup
    (= ConversationView.toggle_last_foldable()).
    """

    DEFAULT_CSS = f"""
    FoldableMarkdown {{
        height: auto;
        padding: 0;
    }}
    FoldableMarkdown:hover Label.fm-hint {{
        color: #cc9955;
    }}
    FoldableMarkdown Label.fm-hint {{
        color: #886633;
        height: 1;
        padding: 0 {_BODY_INDENT_COLS};
    }}
    FoldableMarkdown Static.fm-body {{
        height: auto;
        padding: 0;
    }}
    """

    def __init__(
        self,
        *,
        full_text: str,
        preview_text: str,
        remaining_lines: int,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._full_text = full_text
        self._preview_text = preview_text
        self._remaining_lines = remaining_lines
        self._expanded = False

    def compose(self) -> ComposeResult:
        yield Static(
            Padding(RichMarkdown(self._preview_text), (0, 0, 0, _BODY_INDENT_COLS)),
            classes="fm-body",
        )
        yield Label(
            self._collapsed_hint(),
            classes="fm-hint",
            markup=False,
        )

    def _collapsed_hint(self) -> str:
        return f"▶  {self._remaining_lines} more lines · click or F8 / /expand to show"

    def _expanded_hint(self) -> str:
        return "▼  collapse · click or F8 / /expand"

    def toggle(self) -> None:
        """Toggle between collapsed and expanded state."""
        self._expanded = not self._expanded
        self._refresh_display()

    def on_click(self) -> None:
        """Mouse click toggles expanded state."""
        self.toggle()

    def is_expanded(self) -> bool:
        """Return True when the widget is currently in expanded state."""
        return self._expanded

    def _refresh_display(self) -> None:
        """Update the body Static and hint Label to match _expanded."""
        try:
            body = self.query_one(".fm-body", Static)
            hint = self.query_one(".fm-hint", Label)
        except Exception:
            return
        if self._expanded:
            body.update(
                Padding(RichMarkdown(self._full_text), (0, 0, 0, _BODY_INDENT_COLS))
            )
            hint.update(self._expanded_hint())
        else:
            body.update(
                Padding(RichMarkdown(self._preview_text), (0, 0, 0, _BODY_INDENT_COLS))
            )
            hint.update(self._collapsed_hint())
