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
    "ctrl+shift+g": "⌃⇧G",
    "enter": "Enter", "tab": "Tab", "escape": "Esc", "space": "Space",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
    # Quick-jump number keys for the right-panel tabs (Ctrl+1 .. Ctrl+7).
    "ctrl+1": "⌃1", "ctrl+2": "⌃2", "ctrl+3": "⌃3", "ctrl+4": "⌃4",
    "ctrl+5": "⌃5", "ctrl+6": "⌃6", "ctrl+7": "⌃7", "ctrl+8": "⌃8",
    "ctrl+9": "⌃9",
}
_CONVERSATION_KEYS = {
    "ctrl+p", "ctrl+n", "ctrl+shift+n", "ctrl+shift+p",
    # ``/find`` cycle navigation — semantically conv-pane scoped
    # (= step through matches in the conv log), so they belong with
    # the other conv-navigation keys (turn jump). Without this they
    # would land in the generic GLOBAL group and be harder to
    # discover next to their conceptual peers.
    "ctrl+g", "ctrl+shift+g",
    # SkillActivityRow drill-down toggle. F3 doesn't start with
    # "ctrl+" so the default routing would land it under OTHER;
    # CONVERSATION is the right home because the action toggles
    # inline expand on widgets that live in the conv pane (=
    # mouse-click + F3 are the two trigger paths to the same UX).
    "f3",
}
# ``j`` / ``k`` / ``space`` / ``c`` are routed via ``RightPanel.on_key``
# (not ``app.BINDINGS``) and dispatch per-tab inside the panel handler,
# so they are panel-universal — not docs-only. Wave-2 K1: previously
# they lived in ``_DOCS_KEYS`` which labelled them "DOCS (gated)" in
# the Keys tab even though they scroll any tab (events / agents /
# memory / docs / pending / cost) and toggle the preview pane on
# whichever tab is active. ``c`` is the generic "copy current view"
# action with pending-tab override (= claim); listing it under PANEL
# matches its dominant meaning.
_PANEL_KEYS = {
    "ctrl+o", "ctrl+w", "ctrl+shift+o", "ctrl+shift+w", "tab", "shift+tab",
    "h", "l", "j", "k", "space", "c",
    # Quick-jump tab keys (= Ctrl+1 .. Ctrl+7). They open the panel
    # if hidden + switch to the Nth tab, so the PANEL group is the
    # natural home.
    "ctrl+1", "ctrl+2", "ctrl+3", "ctrl+4",
    "ctrl+5", "ctrl+6", "ctrl+7",
}
_EVENTS_KEYS = {"f", "t"}
# ``/`` stays DOCS-only — it opens the docs name filter, no other tab
# consumes it.
_DOCS_KEYS = {"/"}
_GROUP_ORDER = [
    "GLOBAL", "INPUT", "CONVERSATION", "PANEL",
    "EVENTS (gated)", "DOCS (gated)", "OTHER",
]

# Keys whose app-level binding is voice-mode-gated (active only during
# recording) and whose dominant chat-time meaning lives on InputBar.
# Surface InputBar's description for these so the Keys tab reflects what
# the user experiences 99 % of the time, not the voice-mode override.
_INPUT_OWNED_KEYS = {"enter", "escape", "tab", "up", "down"}


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
    # Local import keeps right_panel/keys_tab decoupled from widget-init
    # order (InputBar imports from chat.slash, which would otherwise be
    # pulled in at module-load time).
    from ..input_bar import InputBar

    groups: dict[str, list[tuple[str, str]]] = {g: [] for g in _GROUP_ORDER}
    seen: set[str] = set()
    # App-level bindings first — they take precedence on same-key conflicts
    # (the InputBar's ctrl+c / ctrl+d / ctrl+l shadow app's, but app's
    # description is the load-bearing one users see in the footer hint).
    # Exception: _INPUT_OWNED_KEYS — defer to InputBar so the listed
    # description matches the non-voice-mode behavior.
    for raw in app.BINDINGS:
        b = raw if isinstance(raw, Binding) else Binding(*raw)
        if b.key in seen or not b.description:
            continue
        if b.key in _INPUT_OWNED_KEYS:
            continue
        seen.add(b.key)
        group = _key_group_for(b.key)
        if group not in groups:
            group = "OTHER"
        groups[group].append((_pretty_key(b.key), b.description))
    # InputBar-level bindings: chat-input affordances (Enter / Tab / arrows
    # / Esc / Ctrl+J Newline / Ctrl+U Clear input). Without surfacing
    # these the Keys tab silently omitted "how do I insert a newline"
    # and "how do I wipe the buffer" — both load-bearing for the input box.
    for raw in InputBar.BINDINGS:
        b = raw if isinstance(raw, Binding) else Binding(*raw)
        if b.key in seen or not b.description:
            continue
        seen.add(b.key)
        groups["INPUT"].append((_pretty_key(b.key), b.description))

    # Right-panel keys handled via ``RightPanel.on_key`` (not declared
    # ``Binding`` objects) are invisible to the BINDINGS iterations above.
    # Surface them explicitly so the user can discover them without
    # reading the source. K1 (wave-2): expanded the panel-universal set
    # to include j / k / space / c, which previously either lived under
    # DOCS-only or were missing entirely despite working on every tab.
    _PANEL_EXPLICIT: list[tuple[str, str]] = [
        ("h", "Widen panel"),
        ("l", "Narrow panel"),
        ("j", "Scroll down (current tab)"),
        ("k", "Scroll up (current tab)"),
        ("space", "Toggle preview pane"),
        ("c", "Copy current view (pending tab: claim cursor)"),
        # A-F2 (wave-8): ``d`` is the primary pending-tab action (=
        # discard the cursor's intervention) but was missing from
        # _PANEL_EXPLICIT entirely, so a user on the pending tab had
        # no way to discover it from the Keys tab. Surface it here
        # alongside ``c=claim`` for symmetry.
        ("d", "Discard cursor (pending tab)"),
        # H-F11 (wave-10 follow-up): ``a`` on the Agents tab prefills
        # ``/attach <name>`` into the InputBar for the cursor's agent.
        # Same "per-tab action" idiom as the pending-tab ``d`` / ``c``
        # discard / claim shortcuts.
        ("a", "Attach to cursor agent (agents tab)"),
    ]
    for key, desc in _PANEL_EXPLICIT:
        if key not in seen:
            groups["PANEL"].append((_pretty_key(key), desc))
            seen.add(key)

    lines: list[str] = []
    # Key column width: max key length within the group, capped at 6
    # (longest pretty key is ⇧Tab / Enter / Space = 5 chars + 1 pad).
    # Single-line "<key>  <desc>" fits the narrow panel; previously the
    # column was a fixed 16 chars which forced every binding onto two
    # rows after wrapping.
    for group_name in _GROUP_ORDER:
        entries = groups.get(group_name, [])
        if not entries:
            continue
        lines.append(f"[bold #aaaaaa]  \\[{_esc(group_name)}][/]")
        key_width = min(6, max((len(k) for k, _ in entries), default=2) + 1)
        for key_display, desc in entries:
            key_col = f"{_esc(key_display):<{key_width}}"
            lines.append(
                f"  [{_CORAL}]{key_col}[/]  [#dddddd]{_esc(desc)}[/]"
            )
        lines.append("")
    if not lines:
        lines.append("[#555555]  (no bindings)[/]")
    return "\n".join(lines)


__all__ = ["render_keys"]
