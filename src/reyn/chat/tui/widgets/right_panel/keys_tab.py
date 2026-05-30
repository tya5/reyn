"""Keys tab — renders application key bindings grouped by context."""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.binding import Binding

from .base import _CORAL, _esc

if TYPE_CHECKING:
    from textual.app import App


# ---------------------------------------------------------------------------
# Inline detail table (T1-4 / T2-3, Wave-12).
# Maps the lowercase key string → multi-line "what does this REALLY do" text.
# ---------------------------------------------------------------------------
_KEY_DETAILS: dict[str, str] = {
    "f7": (
        "Toggle expand on the most-recent failed tool-call row so the\n"
        "full failure reason surfaces inline (mouse-click does the same).\n"
        "If the row was already flushed to scroll history, surfaces a\n"
        "Ctrl+B -> Events hint. No-op hint when no failure exists yet."
    ),
    "f8": (
        "Toggle the latest long agent reply (expand/collapse). Same as\n"
        "/expand or clicking the foldable's hint footer. Collapsed shows\n"
        "preview + ▶ hint; expanded shows full text + ▼ hint."
    ),
    "f9": (
        "Toggle the HH:MM timestamp prefix in conv-pane message headers.\n"
        "With timestamps hidden, body indent shrinks col 8 → col 2, giving\n"
        "more horizontal space for content. Persists via tui_prefs.json."
    ),
    "f3": (
        "Bulk-toggle expand on all in-flight skill + tool-call rows at once\n"
        "(mouse-click does the same per-row). One keypress aligns every row\n"
        "to a single convergence state — mixed sets (one expanded, one\n"
        "collapsed) flip to match the first row. Shows a 'no active rows'\n"
        "hint when nothing is in flight."
    ),
    "escape": (
        "Context-aware back/cancel:\n"
        "  • voice mode → cancel recording\n"
        "  • error box → dismiss\n"
        "  • right panel → close (= drop focus back to input)\n"
        "  • docs filter active → clear filter"
    ),
    "ctrl+b": (
        "Open/close right panel. Opens to the most recent context tab\n"
        "(= last skill activity → Agents; last error jump → Events)."
    ),
    "space": (
        "Toggle preview pane for cursor row. Works on Events / Agents /\n"
        "Memory / Pending tabs. No-op on Cost / Docs / Keys (= those tabs\n"
        "have no per-row preview, except Keys uses Space for THIS expand)."
    ),
    "f4": (
        "Focus the async-stack panel at the bottom of the screen so its\n"
        "rows can be cursored with j/k and inspected."
    ),
    "ctrl+r": (
        "Toggle voice recording. While recording: Enter stops, transcribes,\n"
        "and submits immediately (= dictate-and-send). Esc cancels.\n"
        "F2 is an alias (macOS may intercept F2 — prefer Ctrl+R there)."
    ),
    # T2-3 content sweep additions (Wave-12 Topic B #5 / #6 / #7 / #8).
    "tab": (
        "Confirm slash-picker selection (when picker is open).\n"
        "No-op when picker is closed."
    ),
    "up": (
        "Picker selection up (when picker is open).\n"
        "Else: history previous, but only when cursor is on the\n"
        "first row of the input textarea."
    ),
    "down": (
        "Picker selection down (when picker is open).\n"
        "Else: history next, but only when cursor is on the last\n"
        "row of the input textarea."
    ),
    "f2": (
        "Alias of Ctrl+R (voice toggle). macOS may intercept F2\n"
        "for built-in shortcuts — prefer Ctrl+R there."
    ),
    "f": (
        "Cycle the Events tab filter (groups: all / errors / actions /\n"
        "phase / plan). Only active on the Events tab."
    ),
    "t": (
        "Tail events (Events tab) OR cycle memory type filter\n"
        "(Memory tab). Tab-gated — silent no-op on other tabs."
    ),
    "i": (
        "Isolate the cursor's chain in the Events tab — hides events\n"
        "not in the same chain_id. Only active on the Events tab."
    ),
    "v": (
        "Toggle verbose mode on the Events tab. When off (default),\n"
        "hides compaction_check events (= 'not compacted' noise — fires\n"
        "on every chat turn with outcomes too_few_turns /\n"
        "below_min_batch / below_threshold / already_running).\n"
        "When on, shows everything. Only active on the Events tab."
    ),
    "g": (
        "Docs tab only. Switches language preference: ja → en → ja.\n"
        "Each concept appears once; (ja)/(en) suffix marks fallback\n"
        "rows when the preferred variant is absent."
    ),
}


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
    "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8",
    "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
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
    # F4 focuses the bottom AsyncStackPanel for keyboard nav.
    # Panel renders ambient conv-pane work (= attached agent
    # tasks), so CONVERSATION is the right home for discovery
    # alongside Ctrl+P / Ctrl+N turn jump.
    "f4",
    # F5 / F6 — error block jump (= prev / next mounted ErrorBox).
    # Conv-pane scoped: targets widgets inside the conv pane.
    "f5", "f6",
    # W13 T2-1: ToolCallRow failure drill-down. Same rationale as F3 --
    # toggles inline expand on a widget that lives in the conv pane.
    "f7",
    # F8: FoldableMarkdown toggle. Same rationale as F3/F7 — toggles
    # expand state of a widget mounted in the conv pane.
    "f8",
    # F9: timestamp toggle — affects conv-pane header rendering.
    "f9",
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
_EVENTS_KEYS = {"f", "t", "i", "v"}
# ``/`` and ``g`` stay DOCS-only — ``/`` opens the docs name filter;
# ``g`` toggles the language preference (ja preferred / en preferred).
_DOCS_KEYS = {"/", "g"}
_GROUP_ORDER = [
    "GLOBAL", "INPUT", "CONVERSATION", "PANEL",
    "EVENTS (gated)", "DOCS (gated)", "OTHER", "MOUSE",
]

# Explicit mouse-interaction rows (no key-press semantics — cross-cutting
# affordances that have no Binding entry but are discoverable here).
_MOUSE_EXPLICIT: list[tuple[str, str]] = [
    ("click skill row", "Drill-down (= F3 mouse equivalent)"),
    ("click failed tool row", "Expand failure detail (= F7 mouse equivalent)"),
    ("click foldable reply", "Toggle long reply expand/collapse (= F8 / /expand)"),
    ("click header [N pending]", "Jump to Pending tab"),
    ("click header [find: ...]", "Clear find filter"),
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


def render_keys(
    app: "App",
    *,
    cursor: int = 0,
    expanded: set[int] | None = None,
) -> tuple[str, list[str], list[int]]:
    """Return (Rich markup, flat_key_list, key_ys) listing bindings grouped by context.

    ``cursor`` selects the cursor row (0-indexed over the flat key list).
    ``expanded`` is the set of cursor indices whose detail block is visible.
    ``flat_key_list`` is a parallel list of lowercase key strings (or ``""``
    for MOUSE rows) so callers can look up ``_KEY_DETAILS`` by row index.
    ``key_ys`` is a parallel list of 0-indexed line numbers in the rendered
    output for each key row (= for scroll-into-view, same shape as other
    tabs' ``*_item_ys``).
    """
    if expanded is None:
        expanded = set()

    # Local import keeps right_panel/keys_tab decoupled from widget-init
    # order (InputBar imports from chat.slash, which would otherwise be
    # pulled in at module-load time).
    from ..input_bar import InputBar

    groups: dict[str, list[tuple[str, str]]] = {g: [] for g in _GROUP_ORDER}
    # Parallel dict: group_name → list of raw key strings (lowercase) for
    # detail lookup. MOUSE rows use "" since they have no key semantics.
    group_keys: dict[str, list[str]] = {g: [] for g in _GROUP_ORDER}
    seen: set[str] = set()
    # ``binding_seen`` tracks keys that were emitted from app.BINDINGS /
    # InputBar.BINDINGS. Used by the _PANEL_EXPLICIT loop below to skip
    # entries whose key was already surfaced from a real Binding — but
    # distinct from ``seen`` so that multiple _PANEL_EXPLICIT entries for
    # the same key (= same key, different tab contexts) are not dropped
    # against each other.
    binding_seen: set[str] = set()
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
        binding_seen.add(b.key)
        group = _key_group_for(b.key)
        if group not in groups:
            group = "OTHER"
        groups[group].append((_pretty_key(b.key), b.description))
        group_keys[group].append(b.key.lower())
    # InputBar-level bindings: chat-input affordances (Enter / Tab / arrows
    # / Esc / Ctrl+J Newline / Ctrl+U Clear input). Without surfacing
    # these the Keys tab silently omitted "how do I insert a newline"
    # and "how do I wipe the buffer" — both load-bearing for the input box.
    for raw in InputBar.BINDINGS:
        b = raw if isinstance(raw, Binding) else Binding(*raw)
        if b.key in seen or not b.description:
            continue
        seen.add(b.key)
        binding_seen.add(b.key)
        groups["INPUT"].append((_pretty_key(b.key), b.description))
        group_keys["INPUT"].append(b.key.lower())

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
        # ``d`` does double duty across two tab contexts:
        #   - A-F2 (wave-8): pending tab → discard cursor's intervention
        #   - T2-5a (wave-12): events tab → open runtime/events.md
        # Wave Round 2 finding N5 (2026-05-29): the prior layout
        # emitted two consecutive ``d`` rows under [PANEL]. Users
        # encountered them as duplicate entries (= "why is `d` listed
        # twice?") rather than as one key with two tab-gated meanings.
        # Merge into a single row with the parenthesised dual-meaning
        # idiom that ``c`` already uses ("Copy current view (pending
        # tab: claim cursor)").
        ("d", "Discard cursor (pending tab) / open events.md (events tab)"),
        # H-F11 (wave-10 follow-up): ``a`` on the Agents tab prefills
        # ``/attach <name>`` into the InputBar for the cursor's agent.
        # Same "per-tab action" idiom as the pending-tab ``d`` / ``c``
        # discard / claim shortcuts.
        ("a", "Attach to cursor agent (agents tab)"),
        # Lang toggle: each doc concept appears once; ``g`` switches the
        # preferred language (ja ↔ en). Only active on the Docs tab.
        ("g", "Toggle docs language preference (ja ↔ en, docs tab only)"),
    ]
    # Note: memory tab's ``t`` (= cycle_memory_type_filter, wave-11
    # A#1) is intentionally NOT in this list because ``t`` is already
    # in ``seen`` via the events-tab tail-cycle binding, and the
    # de-dupe guard would silently swallow this entry. The memory
    # binding self-documents via ``_flash_status("memory filter: <X>")``
    # on every press.
    #
    # The guard here is ``binding_seen`` (not ``seen``) so that multiple
    # _PANEL_EXPLICIT rows with the same key but different tab-context
    # descriptions (e.g. two ``d`` rows — pending-tab discard and
    # events-tab docs jump) are both emitted. ``binding_seen`` only tracks
    # keys that were already surfaced from app/InputBar Binding objects;
    # two explicit synthetic rows for the same key are intentional and must
    # not de-dupe against each other.
    for key, desc in _PANEL_EXPLICIT:
        if key not in binding_seen:
            groups["PANEL"].append((_pretty_key(key), desc))
            group_keys["PANEL"].append(key.lower())
            seen.add(key)

    # Wave Round 2 finding N2 (2026-05-29): Tab / Shift+Tab are
    # _INPUT_OWNED_KEYS so the loops above surfaced them under INPUT
    # (= "Confirm" / "Previous"). Their dual role as the panel-focused
    # tab switcher was invisible — the only documented switchers in the
    # PANEL group were ⌃W / ⌃⇧W / ⌃⇧O, even though Tab actually drives
    # the panel-side switch when focus is on the tabs widget. Surface
    # them as PANEL-context rows so the discoverability gap closes
    # without disturbing the InputBar-context description.
    groups["PANEL"].append((_pretty_key("tab"), "Next tab (panel focused)"))
    group_keys["PANEL"].append("tab")
    groups["PANEL"].append((_pretty_key("shift+tab"), "Previous tab (panel focused)"))
    group_keys["PANEL"].append("shift+tab")

    # Wave-11 A#2 / fix/tui-keys-tab-correctness: ``i`` and ``v`` are events-tab
    # keys handled via RightPanel.on_key and routed through ``_EVENTS_KEYS``.
    # They were previously surfaced via ``_PANEL_EXPLICIT`` (= rendered under
    # [PANEL]) which misrepresented their gating. Surface them here as synthetic
    # rows under [EVENTS (gated)] — the correct group per ``_key_group_for``.
    # Only emit if not already surfaced from app/InputBar BINDINGS.
    if "i" not in binding_seen:
        groups["EVENTS (gated)"].append(
            (_pretty_key("i"), "Isolate cursor's chain (events tab only)")
        )
        group_keys["EVENTS (gated)"].append("i")
    if "v" not in binding_seen:
        groups["EVENTS (gated)"].append(
            (_pretty_key("v"), "Toggle verbose (events tab)")
        )
        group_keys["EVENTS (gated)"].append("v")

    # MOUSE group (T1-4, Wave-12): explicit click-interaction rows.
    # These have no key semantics so raw_key is "" for all of them.
    for display, desc in _MOUSE_EXPLICIT:
        groups["MOUSE"].append((display, desc))
        group_keys["MOUSE"].append("")

    # Build flat list of (key_display, desc, raw_key) across all groups in
    # _GROUP_ORDER order for cursor + expand tracking.
    flat_rows: list[tuple[str, str, str]] = []  # (display, desc, raw_key)
    for group_name in _GROUP_ORDER:
        entries = groups.get(group_name, [])
        raw_keys = group_keys.get(group_name, [])
        for (kd, desc), rk in zip(entries, raw_keys):
            flat_rows.append((kd, desc, rk))

    # Render.
    lines: list[str] = []
    flat_key_list: list[str] = []  # parallel to the rendered "key rows" only
    key_ys: list[int] = []  # 0-indexed line number of each key row in output
    # Key column width: max key length within the group, capped at 6
    # (longest pretty key is ⇧Tab / Enter / Space = 5 chars + 1 pad).
    # Single-line "<key>  <desc>" fits the narrow panel; previously the
    # column was a fixed 16 chars which forced every binding onto two
    # rows after wrapping.

    # Row index tracking — increments only for key/mouse entries, not headers.
    row_idx = 0

    for group_name in _GROUP_ORDER:
        entries = groups.get(group_name, [])
        raw_keys = group_keys.get(group_name, [])
        if not entries:
            continue
        lines.append(f"[bold #aaaaaa]  \\[{_esc(group_name)}][/]")
        # MOUSE rows can be much longer than 6 chars — use a wider cap for
        # the MOUSE group so the descriptions don't collide with the key col.
        if group_name == "MOUSE":
            key_width = max((len(k) for k, _ in entries), default=2) + 2
        else:
            key_width = min(6, max((len(k) for k, _ in entries), default=2) + 1)
        for (key_display, desc), raw_key in zip(entries, raw_keys):
            is_cursor = row_idx == cursor
            flat_key_list.append(raw_key)
            # Record the y-position (0-indexed line in the rendered output)
            # of this key row for scroll-into-view.
            key_ys.append(len(lines))

            # Cursor indicator — subtle highlight so the row is identifiable.
            cursor_prefix = f"[bold {_CORAL}]▶[/] " if is_cursor else "  "
            key_col = f"{_esc(key_display):<{key_width}}"
            lines.append(
                f"{cursor_prefix}[{_CORAL}]{key_col}[/]  [#dddddd]{_esc(desc)}[/]"
            )

            # Inline detail block when this row is expanded.
            if row_idx in expanded:
                detail = _KEY_DETAILS.get(raw_key, "")
                if detail:
                    for detail_line in detail.splitlines():
                        lines.append(
                            f"  [dim #aaaaaa]    {_esc(detail_line)}[/]"
                        )

            row_idx += 1
        lines.append("")
    if not lines:
        lines.append("[#555555]  (no bindings)[/]")
    return "\n".join(lines), flat_key_list, key_ys


# ---------------------------------------------------------------------------
# Keys-tab cursor + expand state (owned at module level so RightPanel can
# delegate to these helpers without carrying keys-specific state itself).
# ---------------------------------------------------------------------------
_keys_cursor: int = 0
_keys_expanded: set[int] = set()


def keys_move(delta: int, n_rows: int) -> None:
    """Move the keys-tab cursor by ``delta``; wraps around ``n_rows``."""
    global _keys_cursor
    if n_rows == 0:
        _keys_cursor = 0
        return
    _keys_cursor = (_keys_cursor + delta) % n_rows


def toggle_expand_cursor(flat_key_list: list[str]) -> bool:
    """Toggle the inline detail block for the cursor row.

    Returns True if a detail block is now visible (= toggle-on), False if
    hidden or no-op. No-op when the cursor row has no ``_KEY_DETAILS`` entry.
    """
    global _keys_cursor, _keys_expanded
    if not flat_key_list:
        return False
    idx = max(0, min(len(flat_key_list) - 1, _keys_cursor))
    raw_key = flat_key_list[idx]
    if not raw_key or raw_key not in _KEY_DETAILS:
        # No detail entry → no-op (no crash, no state change).
        return False
    if idx in _keys_expanded:
        _keys_expanded.discard(idx)
        return False
    else:
        _keys_expanded.add(idx)
        return True


def get_keys_cursor() -> int:
    """Return the current keys-tab cursor index."""
    return _keys_cursor


def get_keys_expanded() -> set[int]:
    """Return the current set of expanded row indices (read-only copy)."""
    return set(_keys_expanded)


__all__ = [
    "render_keys",
    "_KEY_DETAILS",
    "keys_move",
    "toggle_expand_cursor",
    "get_keys_cursor",
    "get_keys_expanded",
]
