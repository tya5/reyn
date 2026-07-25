"""Composer + bottom-chrome tab-drawer widgets for the Textual chat surface.

The :class:`Composer` is the multi-line Claude-Code-style input (Enter submits,
Shift+Enter newlines). The bottom chrome (Phase 3) is a slim :class:`StatusLine`
of ``model │ agent │ cost │ ctx`` values plus a focusable :class:`MenuBar` (a
``Tabs`` row); opening a menu item expands a ``ContentSwitcher`` drawer whose
per-tab panes are built by :func:`pane_payload` (an :class:`OptionList` for the
interactive pickers, a plain Rich :class:`Static` for the read-only readouts).

Phase 4 wires every pane to its CANONICAL reyn source (no placeholders):

- **Model** — the operator-configured model classes + active class from the
  status snapshot's ``model_classes`` / ``model_active_class`` (ultimately
  ``Session.known_model_classes()`` → ``ModelResolver.known_classes()``).
- **Agent** — the loaded agents + attach focus from the snapshot's
  ``agent_names`` / ``attached_name`` (``AgentRegistry.loaded_names()``).
- **History** — recent turns of the conversation model the app retains. Phase 5
  hydrates that model at startup from the persisted ``history.jsonl`` log (via the
  ``ChatReadModel.conversation_history`` seam), so this pane is cross-session by
  construction — it shows restored PRIOR turns alongside the live ones (see the
  app docstring).
- **Cost / Ctx** — the live token/cost + context-window figures from the same
  status snapshot the plain path's status bar reads (``usage`` / ``cost_agent`` /
  ``cost_total`` / ``ctx_used`` / ``ctx_window``).
- **Menu** — the full slash-command registry (:data:`reyn.interfaces.slash.REGISTRY`).
- **Help** — the app's declarative ``BINDINGS`` plus the imperative navigation
  keys each widget owns (:data:`COMPOSER_KEYS` / :data:`MENUBAR_KEYS`).

Every ENUMERATING pane (Model / Agent / Menu) derives its full set from the
canonical registry — never a hand-curated subset — so a newly-configured model
class, a freshly-loaded agent, or a newly-registered slash command appears in the
drawer automatically. The formatting is pure (:func:`pane_payload` and its
per-pane helpers take plain inputs and return ``list[str]``) so completeness is
directly testable without mounting a widget. The drawer container itself is
assembled by :class:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp`.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual`` imports never reach an
always-loaded module.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget
from textual.widgets import (
    OptionList,
    Static,
    Tabs,
    TextArea,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from reyn.interfaces.slash import SlashCommand


class Composer(TextArea):
    """Multi-line Claude-Code-style input: **Enter submits**, **Shift+Enter**
    inserts a newline (the inverse of ``TextArea``'s default), auto-growing up to
    ``MAX_ROWS`` then internally scrolling. Every other key falls through to the
    base ``TextArea`` bindings unchanged."""

    MAX_ROWS = 6

    class Submitted(Message):
        """Posted when the user presses Enter with non-blank content."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def on_mount(self) -> None:
        self.show_line_numbers = False
        self._sync_height()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if self.text.strip():
                self.post_message(self.Submitted(self.text))
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            start, end = self.selection
            self._replace_via_keyboard("\n", start, end)
            return
        if event.key == "down":
            # ↓ on the composer's LAST line hands focus down to the menu row
            # (CC/reyn chrome flow: the composer is the default focus; ↓ steps
            # into the bottom chrome). On any earlier line ↓ moves the cursor
            # normally (falls through to the base TextArea).
            row, _ = self.cursor_location
            if row >= self.document.line_count - 1:
                menubar = self.app.query(MenuBar)
                if menubar:
                    event.stop()
                    event.prevent_default()
                    menubar.first().focus()
                    return
        await super()._on_key(event)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._sync_height()

    def _sync_height(self) -> None:
        wrapped_rows = max(self.wrapped_document.height, 1)
        self.styles.height = min(wrapped_rows, self.MAX_ROWS)

    def clear_and_reset(self) -> None:
        self.text = ""
        self._sync_height()


# ── bottom-chrome tab-drawer ─────────────────────────────────────────────────
# Default collapsed = a slim status-values line + a focusable menu row. Pressing
# ↓ from the composer focuses the menu; opening an item expands a drawer
# DOWNWARD. Interactive panes are Textual OptionLists (Model/Agent/History/Menu —
# keyboard selection); static readouts are plain Rich in a Static (Cost/Ctx/Help)
# — the "Textual only where there is a selection" split. Phase 4 fills each pane
# from its canonical reyn source (see :func:`pane_payload`).

_MENU_TABS: "list[tuple[str, str]]" = [
    ("model", "Model"),
    ("agent", "Agent"),
    ("history", "History"),
    ("cost", "Cost"),
    ("ctx", "Ctx"),
    ("menu", "Menu"),
    ("help", "Help"),
]

#: Menu items whose pane is an interactive :class:`OptionList` picker (keyboard
#: selection); every other tab renders as a read-only Rich :class:`Static`.
_LIST_PANES = frozenset({"model", "agent", "history", "menu"})

#: The composer's navigation keys, co-located with the widget that OWNS them (they
#: are imperative ``Composer._on_key`` overrides, not declarative ``BINDINGS``, so
#: the Help pane sources them from here rather than re-hardcoding a second copy).
COMPOSER_KEYS: "list[tuple[str, str]]" = [
    ("enter", "send"),
    ("shift+enter", "newline"),
    ("↓", "focus menu"),
]

#: The menu row's navigation keys (imperative ``MenuBar._on_key`` overrides).
MENUBAR_KEYS: "list[tuple[str, str]]" = [
    ("← →", "move"),
    ("enter", "open"),
    ("↑ / esc", "close"),
]


def pane_is_list(tab_id: str) -> bool:
    """Whether ``tab_id``'s drawer pane is an interactive :class:`OptionList`
    picker (vs a read-only :class:`Static` readout)."""
    return tab_id in _LIST_PANES


# ── per-pane pure formatters (registry inputs → display strings) ──────────────
# Each takes plain data (never a widget) and returns ``list[str]``, so the
# derive-from-registry completeness of the enumerating panes is directly testable
# without mounting Textual. A pane that enumerates a set (model/agent/menu) MUST
# render its FULL input — never a hand-curated subset — so a new registry entry
# surfaces automatically.


def model_pane_options(classes: "Sequence[str]", active: "str | None") -> list[str]:
    """One row per operator-configured model class, active class marked. Derived
    from the snapshot's ``model_classes`` (= ``ModelResolver.known_classes()``) —
    the FULL configured set, so a newly-added class appears without code change."""
    return [f"{c}  · active" if c == active else c for c in classes]


def agent_pane_options(names: "Sequence[str]", active: "str | None") -> list[str]:
    """One row per loaded agent, the attached agent marked. Derived from the
    snapshot's ``agent_names`` (= ``AgentRegistry.loaded_names()``) — the FULL
    loaded set, so a freshly-created/attached agent appears automatically."""
    return [f"{n}  · active" if n == active else n for n in names]


def history_pane_options(turns: "Sequence[str]") -> list[str]:
    """Recent turns of the live conversation (already-formatted ``role · text``
    rows). A readout of the retained conversation model, not a registry set."""
    return list(turns) if turns else ["(no conversation yet)"]


def menu_pane_options(commands: "Iterable[SlashCommand]") -> list[str]:
    """One ``/<name> — <summary>`` row per NON-hidden slash command, sorted by
    name. Derived from the whole :data:`reyn.interfaces.slash.REGISTRY` — a newly
    ``@slash``-registered command appears automatically (no curated subset)."""
    visible = sorted((c for c in commands if not c.hidden), key=lambda c: c.name)
    return [f"/{c.name} — {c.summary}" for c in visible]


def _ctx_pct(snap: "dict | None") -> str:
    """Context-occupancy percent, or ``—`` before any LLM call has completed
    (``used``/``window`` still 0) — mirrors the plain path's ``_ctx_pct``."""
    snap = snap or {}
    window = snap.get("ctx_window", 0)
    used = snap.get("ctx_used", 0)
    if window <= 0 or used <= 0:
        return "—"
    return f"{round(100 * used / window)}%"


def _ctx_bar(used: int, window: int, *, cells: int = 24) -> str:
    """A ``cells``-wide filled/empty occupancy bar (▓ filled, ░ empty)."""
    if window <= 0 or used <= 0:
        return "░" * cells
    filled = min(cells, round(cells * used / window))
    return "▓" * filled + "░" * (cells - filled)


def cost_pane_lines(snap: "dict | None") -> list[str]:
    """The Cost readout — live token/cost figures from the SAME status snapshot
    the plain path's cost chip reads (``usage`` / ``cost_agent`` / ``cost_total``).
    This is the F5b surface: cost becomes visible in the Textual TTY."""
    snap = snap or {}
    p, c, t = snap.get("usage", (0, 0, 0))
    cost_agent = snap.get("cost_agent", 0.0)
    cost_total = snap.get("cost_total", cost_agent)
    return [
        "Usage · this session",
        f"  agent   ${cost_agent:.4f}",
        f"  total   ${cost_total:.4f}",
        f"  tokens  {p:,} in · {c:,} out · {t:,} total",
    ]


def ctx_pane_lines(snap: "dict | None") -> list[str]:
    """The Ctx readout — last-call prompt tokens against the model's real context
    window (``ctx_used`` / ``ctx_window``), with an occupancy bar and percent."""
    snap = snap or {}
    used = snap.get("ctx_used", 0)
    window = snap.get("ctx_window", 0)
    return [
        "Context window",
        f"  {used:,} / {window:,} tokens  ({_ctx_pct(snap)})",
        f"  {_ctx_bar(used, window)}",
    ]


def help_pane_lines(
    app_bindings: "Iterable[tuple[str, str]]" = (),
    *,
    composer_keys: "Sequence[tuple[str, str]]" = tuple(COMPOSER_KEYS),
    menubar_keys: "Sequence[tuple[str, str]]" = tuple(MENUBAR_KEYS),
) -> list[str]:
    """The Help readout — the app's declarative ``BINDINGS`` (passed as
    ``(key, description)`` pairs) plus the imperative composer/menu navigation keys
    each widget owns. Not a registry-enumeration pane: key handling is split
    between declarative ``BINDINGS`` and imperative ``_on_key`` overrides, so the
    keys are sourced from where they are DEFINED (the widgets' key constants + the
    app's BINDINGS) rather than a single enumerable table."""
    lines = ["Shortcuts"]
    lines += [f"  {key}  {desc}" for key, desc in composer_keys]
    lines += [f"  {key}  {desc}" for key, desc in menubar_keys]
    lines += [f"  {key}  {desc}" for key, desc in app_bindings]
    return lines


def pane_payload(
    tab_id: str,
    *,
    snapshot: "dict | None" = None,
    commands: "Iterable[SlashCommand]" = (),
    history: "Sequence[str]" = (),
    app_bindings: "Iterable[tuple[str, str]]" = (),
) -> list[str]:
    """The display rows for ``tab_id``'s drawer pane, derived from canonical reyn
    sources. For a list pane (:func:`pane_is_list`) the rows are OptionList
    options; for a readout the rows are Static lines. All inputs are plain data
    (the app assembles them from its live snapshot / the slash REGISTRY / the
    conversation model) so this stays pure + testable."""
    snap = snapshot or {}
    if tab_id == "model":
        return model_pane_options(
            snap.get("model_classes") or [],
            snap.get("model_active_class") or snap.get("model"),
        )
    if tab_id == "agent":
        return agent_pane_options(snap.get("agent_names") or [], snap.get("attached_name"))
    if tab_id == "history":
        return history_pane_options(history)
    if tab_id == "menu":
        return menu_pane_options(commands)
    if tab_id == "cost":
        return cost_pane_lines(snap)
    if tab_id == "ctx":
        return ctx_pane_lines(snap)
    return help_pane_lines(app_bindings)


def status_line_text(snap: "dict | None", agent_name: str) -> str:
    """The slim ``model │ agent │ cost │ ctx`` status-values line, from the live
    status snapshot (F5b: the running cost + context percent are visible here even
    when the drawer is closed). Falls back to the threaded ``agent_name`` and
    ``—``/``$0.0000``/``—`` when no snapshot is available yet (pre-session)."""
    snap = snap or {}
    model = snap.get("model_active_class") or snap.get("model") or "—"
    agent = snap.get("attached_name") or agent_name
    cost = snap.get("cost_agent", 0.0)
    return f"model {model} │ agent {agent} │ cost ${cost:.4f} │ ctx {_ctx_pct(snap)}"


def build_drawer_pane(tab_id: str, rows: "Sequence[str]") -> Widget:
    """Build the mounted drawer pane widget for ``tab_id`` from its display
    ``rows``: an :class:`OptionList` for a picker pane, a Rich :class:`Static`
    for a readout. The app rebuilds ``rows`` from a fresh snapshot on each open
    (see :meth:`TextualChatApp._refresh_pane`)."""
    if pane_is_list(tab_id):
        return OptionList(*rows, id=tab_id)
    return Static(Text("\n".join(rows)), id=tab_id)


class StatusLine(Static):
    """Slim bottom status-values line — plain, not rich. Mirrors reyn's inline
    REPL bottom toolbar (``model │ agent │ cost │ ctx``), which sits BELOW the
    input — matching Claude Code and reyn (not a top-docked line). The menu
    *items* live in the focusable :class:`MenuBar` row just below it."""


class MenuBar(Tabs):
    """Focusable horizontal menu row (the collapsed bottom chrome). ``← →`` move
    the highlight, ``Enter`` opens the highlighted item's drawer, and ``↑``/``Esc``
    close it and hand focus back to the composer. Unlike a plain :class:`Tabs`,
    moving the highlight does NOT open anything — opening is an explicit ``Enter``
    (the base ``Tabs.TabActivated`` fired on arrow-move is intentionally ignored)."""

    class Selected(Message):
        """Posted on an explicit ``Enter`` (opening ``tab_id``) or on ``↑``/``Esc``
        (the sentinel ``"__close__"``)."""

        def __init__(self, tab_id: str) -> None:
            self.tab_id = tab_id
            super().__init__()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if self.active:
                self.post_message(self.Selected(self.active))
            return
        if event.key in ("up", "escape"):
            event.stop()
            event.prevent_default()
            self.post_message(self.Selected("__close__"))
            return
        await super()._on_key(event)
