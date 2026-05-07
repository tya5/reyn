"""Keys tab — renders application key bindings grouped by context."""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.binding import Binding

from .base import _CORAL, _esc

if TYPE_CHECKING:
    from textual.app import App


_KEY_PRETTY: dict[str, str] = {
    "ctrl+a": "⌃A", "ctrl+b": "⌃B", "ctrl+c": "⌃C", "ctrl+d": "⌃D",
    "ctrl+e": "⌃E", "ctrl+f": "⌃F", "ctrl+g": "⌃G", "ctrl+h": "⌃H",
    "ctrl+i": "⌃I", "ctrl+j": "⌃J", "ctrl+k": "⌃K", "ctrl+l": "⌃L",
    "ctrl+m": "⌃M", "ctrl+n": "⌃N", "ctrl+o": "⌃O", "ctrl+p": "⌃P",
    "ctrl+q": "⌃Q", "ctrl+r": "⌃R", "ctrl+s": "⌃S", "ctrl+t": "⌃T",
    "ctrl+u": "⌃U", "ctrl+v": "⌃V", "ctrl+w": "⌃W", "ctrl+x": "⌃X",
    "ctrl+y": "⌃Y", "ctrl+z": "⌃Z",
    "ctrl+backslash": "⌃\\",
    "shift+tab": "⇧Tab",
    "ctrl+shift+o": "⌃⇧O",
    "ctrl+shift+w": "⌃⇧W",
    "enter": "Enter", "tab": "Tab", "escape": "Esc", "space": "Space",
}
_CONVERSATION_KEYS = {"ctrl+p", "ctrl+n", "ctrl+shift+n", "ctrl+shift+p"}
_PANEL_KEYS = {
    "ctrl+o", "ctrl+w", "ctrl+shift+o", "ctrl+shift+w", "tab", "shift+tab",
    "h", "l",
}
_EVENTS_KEYS = {"f", "t"}
_DOCS_KEYS = {"j", "k", "space", "/"}
_GROUP_ORDER = [
    "GLOBAL", "CONVERSATION", "PANEL",
    "EVENTS (gated)", "DOCS (gated)", "OTHER",
]


def _key_group_for(key: str) -> str:
    if key in _CONVERSATION_KEYS:
        return "CONVERSATION"
    if key in _PANEL_KEYS:
        return "PANEL"
    if key in _EVENTS_KEYS:
        return "EVENTS (gated)"
    if key in _DOCS_KEYS:
        return "DOCS (gated)"
    if key.startswith("ctrl+"):
        return "GLOBAL"
    return "OTHER"


def _pretty_key(key: str) -> str:
    lower = key.lower()
    if lower in _KEY_PRETTY:
        return _KEY_PRETTY[lower]
    if lower.startswith("ctrl+"):
        suffix = key[5:]
        return f"⌃{suffix.upper()}"
    return key


def render_keys(app: "App") -> str:
    """Return Rich markup listing bindings grouped by context."""
    groups: dict[str, list[tuple[str, str]]] = {g: [] for g in _GROUP_ORDER}
    seen: set[str] = set()
    for raw in app.BINDINGS:
        b = raw if isinstance(raw, Binding) else Binding(*raw)
        if b.key in seen or not b.description:
            continue
        seen.add(b.key)
        group = _key_group_for(b.key)
        if group not in groups:
            group = "OTHER"
        groups[group].append((_pretty_key(b.key), b.description))

    lines: list[str] = []
    for group_name in _GROUP_ORDER:
        entries = groups.get(group_name, [])
        if not entries:
            continue
        lines.append(f"[bold #aaaaaa]  \\[{_esc(group_name)}][/]")
        for key_display, desc in entries:
            key_col = f"{_esc(key_display):<16}"
            lines.append(
                f"[{_CORAL}]    {key_col}[/]  [#dddddd]{_esc(desc)}[/]"
            )
        lines.append("")
    if not lines:
        lines.append("[#555555]  (no bindings)[/]")
    return "\n".join(lines)


__all__ = ["render_keys"]
