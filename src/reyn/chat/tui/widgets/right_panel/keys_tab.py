"""Keys tab — renders application key bindings."""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.binding import Binding

from .base import _esc

if TYPE_CHECKING:
    from textual.app import App


def render_keys(app: "App") -> str:
    """Return Rich markup listing each binding (key + description) once."""
    lines: list[str] = []
    seen: set[str] = set()
    for raw in app.BINDINGS:
        b = raw if isinstance(raw, Binding) else Binding(*raw)
        if b.key in seen or not b.description:
            continue
        seen.add(b.key)
        key_col = f"{_esc(b.key):<18}"
        desc_col = _esc(b.description)
        lines.append(f"[#aaaaaa]  {key_col}[/]  [#dddddd]{desc_col}[/]")
    if not lines:
        lines.append("[#555555]  (no bindings)[/]")
    return "\n".join(lines)


__all__ = ["render_keys"]
