"""MatrixScreen — easter egg invoked via the hidden `/matrix` slash command.

Cascading green ASCII rain à la 1999 Wachowski. Press any key (or click)
to dismiss and return to the chat.

Performance note: re-renders the whole grid each tick (~14 fps). Cheap
enough for typical terminal sizes; not optimised because it's an easter
egg, not a production widget.

Note: katakana characters (East Asian Wide, cell_len == 2) were removed from
_CHARS.  Each render column is allocated exactly one terminal cell; emitting
a double-width glyph overwrites the adjacent column's cell and produces a
visual smear on most terminals.  The ASCII/symbol set preserves the green-
rain aesthetic with no cell-width mismatch.
"""
from __future__ import annotations

import random

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

_SYMBOLS = "01:.\"=*+-<>?|/\\~$#@&"
_CHARS = _SYMBOLS


class _Column:
    """One vertical column of falling characters."""

    __slots__ = ("head", "speed", "trail_len", "chars")

    def __init__(self, height: int) -> None:
        self.head = random.randint(-height, 0)
        self.speed = random.choice((1, 1, 1, 2))
        self.trail_len = random.randint(6, 18)
        self.chars: list[str] = [random.choice(_CHARS) for _ in range(height)]

    def reset(self, height: int) -> None:
        self.head = -random.randint(0, height)
        self.speed = random.choice((1, 1, 1, 2))
        self.trail_len = random.randint(6, 18)


class MatrixScreen(ModalScreen):
    """Full-screen Matrix rain. Any key dismisses."""

    DEFAULT_CSS = """
    MatrixScreen {
        align: center middle;
        background: black;
    }
    MatrixScreen #matrix {
        width: 100%;
        height: 100%;
        background: black;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="matrix")

    def on_mount(self) -> None:
        size = self.app.size
        self._w = max(1, size.width)
        self._h = max(1, size.height)
        self._cols = [_Column(self._h) for _ in range(self._w)]
        self._timer = self.set_interval(0.07, self._tick)

    def on_key(self, event) -> None:
        # Any key dismisses — including Esc, Enter, plain letters.
        event.stop()
        self.dismiss()

    def on_click(self, event) -> None:
        event.stop()
        self.dismiss()

    def _tick(self) -> None:
        h, w = self._h, self._w
        # Advance heads, recycle off-screen columns, scramble random cells.
        for col in self._cols:
            col.head += col.speed
            if col.head - col.trail_len > h + 5:
                col.reset(h)
            if random.random() < 0.3:
                col.chars[random.randint(0, h - 1)] = random.choice(_CHARS)

        out = Text()
        for y in range(h):
            for x in range(w):
                col = self._cols[x]
                ch = col.chars[y]
                dist = col.head - y
                if 0 <= dist < col.trail_len:
                    if dist == 0:
                        # Bright head
                        out.append(ch, style="bold #d8ffd8")
                    elif dist < 3:
                        out.append(ch, style="#88ff88")
                    elif dist < col.trail_len // 2:
                        out.append(ch, style="#33dd33")
                    else:
                        out.append(ch, style="#117711")
                else:
                    out.append(" ")
            if y < h - 1:
                out.append("\n")

        try:
            self.query_one("#matrix", Static).update(out)
        except Exception:
            pass
