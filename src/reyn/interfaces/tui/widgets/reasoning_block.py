"""ReasoningBlock — collapsible inline display of a model's reasoning text (#1652).

Models emit reasoning / "thinking" text (``reasoning_content``) separately from
the visible reply. #1652 surfaces it in the conversation as a PERSISTENT,
collapsible block — visually distinct from the agent's answer (it is the model's
thoughts, not its reply).

NOT to be confused with ``InlineThinkingRow`` (a TRANSIENT Braille spinner shown
while the LLM is in flight, no text, unmounted when the reply lands). This widget
is the persistent reasoning *content* that stays in the conversation history.

Shape (mirrors the established collapsible inline rows — ToolCallRow /
SkillActivityRow):
- Header line: ``💭 reasoning · <N> lines  <glyph>`` (glyph ▾ expanded / ▸ collapsed).
- Body: the full reasoning text, shown only when expanded.
- Default EXPANDED (the #1652 contract: reasoning is shown by default).
- Toggle by mouse click (``on_click``); the app's F3 drill-down action can also
  drive it (F3 is the TextArea-swallow-safe key the other inline rows reuse — no
  new binding, no printable/Ctrl-chord swallow).

Render-only widget: it holds the reasoning string + collapse state and renders.
The outbox→mount wiring + the ``display_reasoning`` gate live in the conv pane /
app_outbox handler (a separate seam, finalised against e2e's #1652 outbox struct).
"""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from reyn.interfaces.tui._palette import _TEXT_MUTED

from ._renderable_cache import RenderableCacheMixin

# Header marker — a thought bubble so the block reads as "the model's thoughts",
# distinct from the agent reply (no glyph) and the tool-call rows (●/✓/✗).
_HEADER_PREFIX = "💭 reasoning"
_GLYPH_EXPANDED = "▾"
_GLYPH_COLLAPSED = "▸"


class ReasoningBlock(RenderableCacheMixin, Widget):
    """Collapsible inline block rendering a model's reasoning text (#1652).

    Default expanded. Click (or the app F3 drill-down action) toggles between
    the header-only collapsed view and the header + full-text expanded view.
    Styled dim/italic so it reads as secondary "thoughts" next to the reply.
    """

    DEFAULT_CSS = """
    ReasoningBlock {
        height: auto;
        padding: 0 0;
    }
    """

    can_focus = False

    def __init__(
        self,
        *,
        reasoning: str = "",
        expanded: bool = True,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._reasoning = reasoning or ""
        # Default EXPANDED per the #1652 contract (reasoning shown by default;
        # the display gate is upstream — a hidden block is simply not mounted).
        self._expanded = expanded
        self._header: Static | None = None
        self._body: Static | None = None

    # ── Textual lifecycle ───────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        self._header = Static(id="reasoning_header")
        self._body = Static(id="reasoning_body")
        yield self._header
        yield self._body

    def on_mount(self) -> None:
        self._refresh()

    def on_click(self, event: events.Click) -> None:
        """Mouse click anywhere on the block toggles collapse (mirrors the
        ToolCallRow / SkillActivityRow click contract). ``event.stop()`` so the
        click doesn't bubble to the conv pane's scroll handling."""
        event.stop()
        self.toggle_expand()

    # ── Public API ──────────────────────────────────────────────────────────

    def set_reasoning(self, reasoning: str) -> None:
        """Replace the reasoning text + re-render (e.g. streaming append/final)."""
        self._reasoning = reasoning or ""
        self._refresh()

    def toggle_expand(self) -> None:
        """Flip collapsed / expanded and re-render."""
        self._expanded = not self._expanded
        self._refresh()

    @property
    def is_expanded(self) -> bool:
        """True when the full reasoning text is currently shown."""
        return self._expanded

    def render_header(self) -> Text:
        """Public accessor for the header line (tests assert on ``.plain``)."""
        return self._build_header()

    def render_body(self) -> Text:
        """Public accessor for the body line(s) (tests assert on ``.plain``)."""
        return self._build_body()

    # ── Render ────────────────────────────────────────────────────────────────

    def _line_count(self) -> int:
        if not self._reasoning:
            return 0
        return self._reasoning.count("\n") + 1

    def _build_header(self) -> Text:
        glyph = _GLYPH_EXPANDED if self._expanded else _GLYPH_COLLAPSED
        n = self._line_count()
        t = Text()
        # Dim + italic so the header reads as secondary "thoughts", not a reply.
        t.append(f"{_HEADER_PREFIX} · {n} line{'s' if n != 1 else ''}  {glyph}",
                 style=f"italic {_TEXT_MUTED}")
        return t

    def _build_body(self) -> Text:
        # Body only renders when expanded; collapsed shows the header alone.
        if not self._expanded or not self._reasoning:
            return Text("")
        # Dim (not italic — long text stays readable) so the body is clearly
        # the model's thoughts, visually subordinate to the agent reply.
        return Text(self._reasoning, style=_TEXT_MUTED)

    def _refresh(self) -> None:
        header = self._build_header()
        body = self._build_body()
        if self._header is not None:
            self._header.update(header)
        if self._body is not None:
            self._body.update(body)
        combined = Text()
        combined.append_text(header)
        if body.plain:
            combined.append("\n")
            combined.append_text(body)
        self._set_rendered_cache(combined)


__all__ = ["ReasoningBlock"]
