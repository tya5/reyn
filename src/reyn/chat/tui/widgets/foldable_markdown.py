"""FoldableMarkdown — Markdown widget that auto-folds long agent replies.

Long content (> fold_threshold lines) is truncated to the first N lines with
a fold-hint Label below. Pressing Enter (or calling expand()) replaces the
truncated Markdown with the full content and removes the hint.
"""
from __future__ import annotations

_CORAL = "#C8553D"  # primary theme colour — matches Theme(primary=...)

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Label, Markdown


class FoldableMarkdown(Widget):
    """Wrap agent Markdown so long content auto-folds with an expand affordance.

    If the markdown has more lines than *fold_threshold*, only the first N
    lines are shown along with a dim coral hint label. The user can press
    Enter (or the caller can call :meth:`expand`) to reveal the full text.

    Usage::

        widget = FoldableMarkdown(reply_text, fold_threshold=30)
        await conversation.mount(widget)
        # Later, expand programmatically:
        widget.expand()
    """

    DEFAULT_CSS = """
    FoldableMarkdown {
        height: auto;
    }
    FoldableMarkdown Markdown {
        padding: 0 0;
        height: auto;
        background: transparent;
    }
    FoldableMarkdown .fold-hint {
        height: auto;
        padding: 0 0;
    }
    """

    BINDINGS = [Binding("enter", "expand", "Expand", show=False)]

    can_focus = True

    def __init__(
        self,
        markdown: str,
        *,
        fold_threshold: int = 30,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._source = markdown
        self._fold_threshold = fold_threshold
        self._total_lines = markdown.count("\n") + 1
        self._expanded = False

    # ── compose ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        if self._total_lines <= self._fold_threshold:
            yield Markdown(self._source)
            return

        # Folded view: show only the first fold_threshold lines + hint.
        preview = "\n".join(self._source.splitlines()[: self._fold_threshold])
        yield Markdown(preview, id="folded-markdown")

        remaining = self._total_lines - self._fold_threshold
        hint_text = f"  [ … {remaining} more lines · enter to expand ]"
        yield Label(
            f"[dim {_CORAL}]{hint_text}[/]",
            classes="fold-hint",
            id="fold-hint",
        )

    # ── public API ────────────────────────────────────────────────────────────

    def expand(self) -> None:
        """Force expansion of the folded content."""
        if self._expanded:
            return
        self._expanded = True

        # Query existing children before mutation.
        try:
            old_md = self.query_one("#folded-markdown", Markdown)
            hint = self.query_one("#fold-hint", Label)
        except Exception:
            # Already expanded or widget not yet composed — nothing to do.
            return

        full_md = Markdown(self._source)
        # Mount full Markdown before the fold hint, then remove both stubs.
        self.mount(full_md, before=hint)
        old_md.remove()
        hint.remove()

    # ── action ────────────────────────────────────────────────────────────────

    def action_expand(self) -> None:
        self.expand()
