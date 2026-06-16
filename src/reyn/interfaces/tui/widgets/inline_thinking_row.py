"""InlineThinkingRow — inline Braille spinner for LLM-in-flight state.

Replaces the sticky ``kind="thinking"`` indicator (⟳ thinking · Ns) with
an inline widget mounted in the conv pane flow, right where the agent reply
will land. Animates a 10-frame Braille cycle at ~10 fps. No text — pure
visual indicator.

Usage::

    conv.start_thinking()   # mount below the last message
    # … LLM in flight …
    conv.stop_thinking()    # unmount (idempotent on both calls)
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Label

from reyn.interfaces.tui._palette import _AMBER

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # 10-frame Braille cycle
_INTERVAL_S = 0.10  # ~10 fps


class InlineThinkingRow(Widget):
    """Inline spinning indicator showing 'LLM in flight'.

    Mounts in the conv pane flow (= below the last rendered message, before
    the agent reply lands). Animates a Braille spinner via set_interval.
    No "thinking" word — pure visual indicator.

    Lifecycle: mount via ``ConversationView.start_thinking()``;
    unmount via ``ConversationView.stop_thinking()``.
    """

    # Default padding 0 2 matches the ts-off body indent (= _BODY_INDENT_NO_TS
    # = 2).  When timestamps are on (_BODY_INDENT_WITH_TS = 8) the caller must
    # pass indent=8 so the spinner aligns with the body text (Fix 2 / A1).
    DEFAULT_CSS = f"""
    InlineThinkingRow {{
        height: 1;
        padding: 0 2;
        color: {_AMBER};
    }}
    """

    can_focus = False

    def __init__(self, id: str | None = None, *, indent: int | None = None) -> None:
        super().__init__(id=id)
        self._frame_idx = 0
        self._label: Label | None = None
        self._timer = None
        # Dynamic left-padding: caller passes the current body indent so the
        # spinner aligns with body text for the current timestamp-toggle state.
        # None → fall back to DEFAULT_CSS value (0 2 = ts-off indent of 2).
        self._indent: int | None = indent

    def compose(self) -> ComposeResult:
        self._label = Label(_FRAMES[0])
        yield self._label

    def on_mount(self) -> None:
        """Start the Braille animation timer.

        Also applies the dynamic left-padding supplied at construction so
        the spinner aligns with surrounding body text for the current
        timestamp-toggle state (Fix 2 / A1).
        """
        if self._indent is not None:
            self.styles.padding = (0, 0, 0, self._indent)
        self._timer = self.set_interval(_INTERVAL_S, self._tick)

    def _tick(self) -> None:
        self._frame_idx = (self._frame_idx + 1) % len(_FRAMES)
        if self._label is not None:
            self._label.update(_FRAMES[self._frame_idx])

    @property
    def frame_idx(self) -> int:
        """Current Braille spinner frame index (0 … len(_FRAMES)-1).

        Provided as a public accessor so tests can assert on the animation
        state via the public surface (= CLAUDE.md "NEVER assert on private
        state").
        """
        return self._frame_idx

    @property
    def body_indent(self) -> int | None:
        """The left-padding column passed at construction, or None if not supplied.

        Exposed so tests can verify the indent without reading ``_indent``.
        """
        return self._indent

    def on_unmount(self) -> None:
        """Stop the animation timer when the widget is removed."""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
