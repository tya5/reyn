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
  status snapshot the plain path's status bar reads. Cost is the 5-row ×
  3-scope breakdown table (:func:`_cost_breakdown_table`) plus the cumulative
  token/cache lines; Ctx is the current-state block (window / prompt / free /
  last-call cache) plus the compaction subsystem's own estimate.
- **Tool / MCP / Skill / Hook** — the session-scoped capability-visibility and
  hook-applicability toggles (``visibility_items`` / ``hook_items``), each row
  carrying the ``/visibility`` or ``/hook`` slash that flips it
  (:func:`pane_commands`).
- **Pipe / Cron** — the registered pipelines and configured cron jobs
  (read-only: neither has an on/off toggle mechanism).
- **Menu** — the full slash-command registry (:data:`reyn.interfaces.slash.REGISTRY`).
- **Help** — the app's declarative ``BINDINGS`` plus the imperative/declarative
  navigation keys each widget owns (:data:`COMPOSER_KEYS` /
  :data:`MENUBAR_KEYS` / :data:`SENTQUEUE_KEYS` — the sent-queue's own
  select/cancel/back-to-composer keys, #3300 Y-client).

Every ENUMERATING pane (Model / Agent / Menu / the toggle categories) derives its
full set from the canonical registry — never a hand-curated subset — so a
newly-configured model class, a freshly-loaded agent, or a newly-registered slash
command appears in the drawer automatically. The formatting is pure
(:func:`pane_payload` and its per-pane helpers take plain inputs and return
``list[str]``) so completeness is directly testable without mounting a widget.
An ACTIONABLE pane's rows and the slash commands that apply them are projected
from ONE ``(row, command)`` entry list per pane (:data:`_PANE_ENTRY_BUILDERS`),
so :func:`pane_payload` and :func:`pane_commands` can never drift out of
index lock-step. The drawer container itself is assembled by
:class:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp`.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual`` imports never reach an
always-loaded module.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.content import Content
from textual.message import Message
from textual.widget import Widget
from textual.widgets import (
    OptionList,
    Static,
    Tabs,
    TextArea,
)

from .intervention_panel import InterventionPanel
from .sent_queue import SentQueue

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
        if event.key == "up":
            # ↑ on the composer's FIRST line hands focus UP into whichever
            # region above the composer currently has something to act on —
            # the mirror image of the ↓ rule above (the region sits ABOVE the
            # composer: conversation / intervention panel / sent-queue /
            # input). #3327: a pending intervention takes PRIORITY over the
            # sent-queue (answering it is the more urgent action, and it sits
            # higher in the stack) — this is also the keyboard route BACK
            # into the panel after Esc/Tab dismissed it without answering
            # (InterventionPanel.action_dismiss_panel), which before #3327
            # had no way back at all. #3300 Y-client's sent-queue-focus rule
            # is otherwise unchanged: on any later line, or with nothing
            # pending/queued, ↑ moves the cursor normally (falls through to
            # the base TextArea) — never steals focus toward a region with
            # nothing to act on.
            row, _ = self.cursor_location
            if row <= 0:
                iv_panel = self.app.query(InterventionPanel)
                if iv_panel and iv_panel.first().has_pending():
                    event.stop()
                    event.prevent_default()
                    iv_panel.first().focus_pending()
                    return
                sent_queue = self.app.query(SentQueue)
                if sent_queue and sent_queue.first().has_items():
                    event.stop()
                    event.prevent_default()
                    sent_queue.first().focus()
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
    # The six categories the retired chip bar kept behind its level-2 "more…"
    # sub-bar (#3338). There is no second level here — the drawer's tab row is
    # already a flat, arrow-navigable strip, so each category is simply its own
    # tab. Tool/MCP/Skill/Hook are ACTIONABLE (each row dispatches the
    # ``/visibility`` or ``/hook`` that flips it); Pipe/Cron are read-only.
    ("tool", "Tool"),
    ("mcp", "MCP"),
    ("skill", "Skill"),
    ("pipe", "Pipe"),
    ("hook", "Hook"),
    ("cron", "Cron"),
    ("menu", "Menu"),
    ("help", "Help"),
]

#: Menu items whose pane is an interactive :class:`OptionList` picker (keyboard
#: selection); every other tab renders as a read-only Rich :class:`Static`.
#:
#: **Known display limitation (Model/Agent/Menu, NOT a security gap):**
#: ``OptionList`` markup-parses a bare ``str`` option — only the "history"
#: tab's rows get the :func:`_history_option_content` ``Content``-literal
#: wrap, because only History carries LLM-/user-derived conversation text
#: (#3302 fix-class). Model/Agent/Menu rows are operator/config-derived
#: identifiers (configured model classes, loaded agent names, the
#: ``@slash``-registered command table) — not live conversation content, so
#: they are NOT neutralize-relevant and are left unwrapped. If an operator
#: ever names a model class / agent / slash command with a `[...]`-shaped
#: substring, THAT row's display will visually corrupt (the same bracket-
#: eating rendering quirk, not an injection risk) — a known, accepted
#: limitation of leaving those three panes unwrapped, not a claim that they
#: are immune to the rendering quirk.
_LIST_PANES = frozenset({
    "model", "agent", "history", "menu", "tool", "mcp", "skill", "hook",
})

#: The composer's navigation keys, co-located with the widget that OWNS them (they
#: are imperative ``Composer._on_key`` overrides, not declarative ``BINDINGS``, so
#: the Help pane sources them from here rather than re-hardcoding a second copy).
COMPOSER_KEYS: "list[tuple[str, str]]" = [
    ("enter", "send"),
    ("shift+enter", "newline"),
    ("↓", "focus menu"),
    # #3327: ↑ now targets whichever of the two regions above the composer
    # actually has something to act on, pending intervention first — see
    # ``Composer._on_key``'s "up" branch for the exact priority/fallback.
    ("↑", "focus pending intervention (else sent queue)"),
]

#: The menu row's navigation keys (imperative ``MenuBar._on_key`` overrides).
MENUBAR_KEYS: "list[tuple[str, str]]" = [
    ("← →", "move"),
    ("enter", "open"),
    ("↑ / esc", "close"),
]

#: The sent-queue region's navigation keys (#3300 Y-client,
#: ``SentQueue.BINDINGS`` — declarative, but the Help pane still sources them
#: from HERE, the same single-source-of-truth convention ``MENUBAR_KEYS``
#: uses, rather than re-deriving prose from the ``Binding`` objects).
SENTQUEUE_KEYS: "list[tuple[str, str]]" = [
    ("↑ / ↓", "select queued message"),
    ("enter", "cancel selected"),
    ("esc / tab", "back to composer"),
]


def pane_is_list(tab_id: str) -> bool:
    """Whether ``tab_id``'s drawer pane is an interactive :class:`OptionList`
    picker (vs a read-only :class:`Static` readout)."""
    return tab_id in _LIST_PANES


#: The ONE list pane whose rows are LLM-/user-derived content (recent
#: conversation turns) rather than operator/config-derived identifiers —
#: see :func:`_history_option_content`'s docstring for why only this tab
#: needs the fidelity wrap.
_USER_CONTENT_LIST_PANE = "history"


def _history_option_content(rows: "Sequence[str]") -> list[Content]:
    """Wrap each History-pane row in a literal :class:`~textual.content.Content`
    — never a bare ``str`` handed to :class:`~textual.widgets.OptionList`.

    ``OptionList`` markup-parses a bare ``str`` option exactly like
    ``Static``/``RadioButton`` do (``Option.prompt`` → ``textual.visual.
    visualize(..., markup=True)`` by default, unset here) — the SAME
    ``#3302`` bracket-eating class, just reached through a different widget.
    History rows are the one drawer pane whose text is conversation content
    (:func:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp.
    _history_turns`, already neutralized — ESC/control strip — at that
    source), so ONLY this pane needs the wrap; Model/Agent/Menu rows are
    operator/config-derived identifiers (see the module docstring), not
    live conversation text.

    Two call sites need this identically — the initial build
    (:func:`build_drawer_pane`, at ``compose`` time) and the refresh
    (``TextualChatApp._refresh_pane``, on every drawer re-open) — a fresh
    History tab was, before this fix, safe at ONE of those and broken at
    the other depending on which code path last touched it."""
    return [Content(row) for row in rows]


# ── per-pane pure formatters (registry inputs → display strings) ──────────────
# Each takes plain data (never a widget) and returns ``list[str]``, so the
# derive-from-registry completeness of the enumerating panes is directly testable
# without mounting Textual. A pane that enumerates a set (model/agent/menu) MUST
# render its FULL input — never a hand-curated subset — so a new registry entry
# surfaces automatically.


def _model_pane_entries(
    classes: "Sequence[str]", active: "str | None"
) -> "list[tuple[str, str]]":
    """``(row, slash)`` per operator-configured model class, active class marked."""
    return [
        (f"{c}  · active" if c == active else c, f"/model {c}") for c in classes
    ]


def model_pane_options(classes: "Sequence[str]", active: "str | None") -> list[str]:
    """One row per operator-configured model class, active class marked. Derived
    from the snapshot's ``model_classes`` (= ``ModelResolver.known_classes()``) —
    the FULL configured set, so a newly-added class appears without code change."""
    return [row for row, _cmd in _model_pane_entries(classes, active)]


def _agent_pane_entries(
    names: "Sequence[str]",
    active: "str | None",
    tree: "Sequence[dict]" = (),
) -> "list[tuple[str, str]]":
    """``(row, slash)`` for the agent→session tree (``AgentRegistry.session_tree()``
    via the snapshot's ``session_tree``), falling back to the flat agent list when
    the tree is empty.

    The tree shape is the retired chip bar's ``_agent_expansion`` contract, restored
    (#3338): one row per agent plus an indented row per session of that agent, the
    attach focus marked with ``▸`` at BOTH levels. A session row switches when its
    agent is already attached (``/session switch <sid>``); otherwise it attaches the
    agent first (``/attach <agent>``) — switching into a session of a non-attached
    agent is not a single-command operation, so the row does the reachable half
    rather than dispatching a command that would fail."""
    if tree:
        out: "list[tuple[str, str]]" = []
        for agent in tree:
            name = agent.get("agent", "")
            attached = bool(agent.get("attached"))
            out.append((f"{'▸' if attached else ' '} {name}", f"/attach {name}"))
            for sess in agent.get("sessions") or []:
                sid = sess.get("sid", "")
                smark = "▸" if sess.get("attached") else " "
                cmd = f"/session switch {sid}" if attached else f"/attach {name}"
                out.append((f"    {smark} {sid}", cmd))
        return out
    return [
        (f"{n}  · active" if n == active else n, f"/attach {n}") for n in names
    ]


def agent_pane_options(
    names: "Sequence[str]",
    active: "str | None",
    tree: "Sequence[dict]" = (),
) -> list[str]:
    """One row per loaded agent AND per session beneath it, the attach focus
    marked. Derived from the snapshot's ``session_tree`` (=
    ``AgentRegistry.session_tree()``), degrading to the flat ``agent_names`` list
    when no tree is available — the FULL loaded set either way, so a
    freshly-created/attached agent (or a newly-spawned session) appears
    automatically."""
    return [row for row, _cmd in _agent_pane_entries(names, active, tree)]


# ── the six toggle/list categories the retired "more…" sub-bar owned (#3338) ──


def _visibility_pane_entries(
    snap: dict, kind: str, fallback_key: "str | None"
) -> "list[tuple[str, str]]":
    """``(row, slash)`` for one capability-visibility category (tool/mcp/skill).

    Session-backed ``visibility_items`` give togglable rows whose slash FLIPS the
    current state (``/visibility off …`` for an on item and vice versa). Until the
    session wires that state, fall back to the config-declared names as a read-only
    listing (empty command = the row dispatches nothing). ``fallback_key`` is
    ``None`` for tool, which has no config-declared name source."""
    items = [
        it for it in (snap.get("visibility_items") or []) if it.get("kind") == kind
    ]
    if items:
        return [
            (
                f"[{'on' if it['on'] else 'off'}] {it['name']}",
                f"/visibility {'off' if it['on'] else 'on'} {kind} {it['name']}",
            )
            for it in items
        ]
    names = [d["name"] for d in (snap.get(fallback_key) or [])] if fallback_key else []
    return [(n, "") for n in names] or [("(none)", "")]


def _hook_pane_entries(snap: dict) -> "list[tuple[str, str]]":
    """``(row, slash)`` for the hook-applicability toggles — session-backed
    ``hook_items`` (each row's slash flips it via ``/hook on|off <name>``), else the
    config-derived hook labels as a read-only listing."""
    items = snap.get("hook_items") or []
    if items:
        return [
            (
                f"[{'on' if h['on'] else 'off'}] {h['name']}"
                + (f"  · {h['scope']}" if h.get("scope") else ""),
                f"/hook {'off' if h['on'] else 'on'} {h['name']}",
            )
            for h in items
        ]
    labels = [h["label"] for h in (snap.get("hooks") or [])]
    return [(label, "") for label in labels] or [("(none)", "")]


def pipe_pane_lines(snap: "dict | None") -> list[str]:
    """The registered pipelines (``PipelineRegistry.entries()`` via the snapshot's
    ``pipelines``). Read-only: pipelines have no on/off toggle mechanism."""
    snap = snap or {}
    pipelines = snap.get("pipelines") or []
    return [
        f"{p['name']}  {p['description']}" if p.get("description") else f"{p['name']}"
        for p in pipelines
    ] or ["(none)"]


def cron_pane_lines(snap: "dict | None") -> list[str]:
    """The configured cron jobs (``config.cron.jobs`` via the snapshot's
    ``cron_jobs``), each with its enabled state and schedule. Read-only: a cron
    job's enabled flag is config-declared, not session-togglable."""
    snap = snap or {}
    jobs = snap.get("cron_jobs") or []
    return [
        f"[{'on' if j.get('enabled') else 'off'}] {j['name']}  {j['schedule']}"
        for j in jobs
    ] or ["(none)"]


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


def _cache_hit_line(label: str, cached: int, prompt: int, *, note: str = "") -> str:
    """One ``cache X% hit (a / b prompt tokens)`` line, label padded to the same
    9-char column every other cost/ctx line uses (it was misaligned when the label
    itself carried the qualifier, e.g. ``"cache (cumulative)"``)."""
    pct = round(100 * cached / prompt) if prompt > 0 else 0
    tail = f", {note}" if note else ""
    return f"{label:<9}{pct}% hit ({cached:,} / {prompt:,} prompt tokens{tail})"


# Cost-panel breakdown: the >200k tiered-pricing guard tolerance.
# ``estimate_cost_breakdown()`` does not replicate litellm's >200k tiered rates
# (see its docstring), so the 4 components' sum can legitimately diverge from the
# litellm-accurate Total at very high token volumes. A pure floating-point
# rounding residual from summing many small per-call floats is NOT the same thing
# as tiered pricing kicking in — the relative tolerance below absorbs float noise
# while still catching a real tiered-rate mismatch (typically a multi-percent
# divergence, not a rounding-error one).
_COST_BREAKDOWN_EPSILON_ABS = 1e-6
_COST_BREAKDOWN_EPSILON_REL = 1e-4


def _cost_scope_state(
    breakdown, authoritative_total: float
) -> "tuple[float, float, float, float, str]":
    """One scope column's ``(input_cost, output_cost, saved, saved_pct, state)``.

    ``input_cost`` = the cache-aware cost actually paid for input (prompt +
    cache-read + cache-creation components). ``saved_pct`` = ``Saved /
    (Input + Saved)`` — the no-cache-baseline denominator (what input WOULD have
    cost without caching), NOT ``Saved / Total``: pinning the wrong denominator
    silently under/over-states the savings %. Divide-by-zero guarded (0% when
    ``Input + Saved == 0``, i.e. no priced input tokens recorded yet).

    ``state`` is one of THREE cases — the panel renders each distinctly so it never
    MISATTRIBUTES a cause:

    - ``"ok"`` — the 4 components reconcile with the authoritative Total (within
      float-noise tolerance): show exact numbers.
    - ``"approx"`` — components are present (sum > 0) but diverge from Total beyond
      tolerance = genuine >200k TIERED pricing, which ``estimate_cost_breakdown``
      does not replicate: mark the component cells ``~`` + a tiered-pricing
      footnote.
    - ``"unavail"`` — components are ~0 while Total > 0 = the breakdown is
      UNAVAILABLE, not diverging (the durable per-agent Total survives a restart
      via the ledger, but the in-memory ``CostBreakdown`` resets to 0 — it is NOT
      ledger-persisted; it is also 0 before the first accumulation). This is NOT
      tiered pricing, so it must NOT fire the ``~``/tiered footnote (a false-fire
      the architect caught once already); the Total stays authoritative and the
      component cells blank to ``—`` with a distinct "unavailable" note.
    """
    input_cost = (
        breakdown.prompt_cost + breakdown.cache_read_cost + breakdown.cache_creation_cost
    )
    output_cost = breakdown.completion_cost
    saved = breakdown.cache_savings
    no_cache_baseline = input_cost + saved
    saved_pct = (saved / no_cache_baseline) if no_cache_baseline > 0 else 0.0

    component_sum = input_cost + output_cost
    tol = max(
        _COST_BREAKDOWN_EPSILON_ABS,
        abs(authoritative_total) * _COST_BREAKDOWN_EPSILON_REL,
    )
    if component_sum <= _COST_BREAKDOWN_EPSILON_ABS and authoritative_total > tol:
        # Breakdown absent while a real Total exists → unavailable, not tiered.
        state = "unavail"
    elif abs(component_sum - authoritative_total) > tol:
        # Components present but don't reconcile → genuine >200k tiered pricing.
        state = "approx"
    else:
        state = "ok"
    return input_cost, output_cost, saved, saved_pct, state


def _cost_breakdown_table(snap: dict) -> list[str]:
    """The 5-row (Total/Input/Output/Saved/Saved%) × 3-column (Session/Agent/
    Project) cost breakdown table.

    Total is always the litellm-accurate authoritative figure (``cost_usd`` /
    ``cost_agent`` / ``cost_total`` — already computed via ``estimate_cost``,
    unaffected by the >200k breakdown limitation). Input/Output/Saved/Saved% are
    derived from the accumulated ``CostBreakdown`` per scope. Per-scope ``state``
    (see :func:`_cost_scope_state`) decides how the component cells render: exact
    (``ok``), ``~``-marked with a tiered-pricing footnote (``approx``), or ``—``
    with a DIFFERENT "unavailable" footnote (``unavail``) — never misattributed to
    tiered pricing."""
    from reyn.llm.pricing import CostBreakdown

    session_total = snap.get("cost_usd", 0.0)
    scopes = [
        ("Ses", snap.get("cost_breakdown_session") or CostBreakdown(), session_total),
        ("Agt", snap.get("cost_breakdown_agent") or CostBreakdown(),
         snap.get("cost_agent", session_total)),
        ("Prj", snap.get("cost_breakdown_project") or CostBreakdown(),
         snap.get("cost_total", session_total)),
    ]
    col_w = 9
    header = "COST" + "".join(f"{name:>{col_w}}" for name, _, _ in scopes)

    per_scope = [
        (name, total, *_cost_scope_state(breakdown, total))
        for name, breakdown, total in scopes
    ]
    any_approx = any(state == "approx" for *_rest, state in per_scope)
    any_unavail = any(state == "unavail" for *_rest, state in per_scope)

    total_row = "Total" + "".join(
        f"{'$' + format(total, '.4f'):>{col_w}}" for _, total, *_ in per_scope
    )

    def _cell(value: float, state: str) -> str:
        if state == "unavail":
            return "—"
        s = f"${value:.4f}"
        return ("~" + s)[:col_w] if state == "approx" else s

    input_row = "Input" + "".join(
        f"{_cell(inp, state):>{col_w}}" for _, _, inp, _out, _sav, _pct, state in per_scope
    )
    output_row = "Output" + "".join(
        f"{_cell(out, state):>{col_w}}" for _, _, _inp, out, _sav, _pct, state in per_scope
    )
    saved_row = "Saved" + "".join(
        f"{_cell(sav, state):>{col_w}}" for _, _, _inp, _out, sav, _pct, state in per_scope
    )
    pct_row = "Saved%" + "".join(
        ("—".rjust(col_w) if state == "unavail" else f"{round(100 * pct)}%".rjust(col_w))
        for _, _, _inp, _out, _sav, pct, state in per_scope
    )

    rows = [header, total_row, input_row, output_row, saved_row, pct_row]
    if any_approx:
        rows.append("~ approx at high volume (>200k tiered pricing)")
    if any_unavail:
        rows.append("— breakdown unavailable this session (Total is exact)")
    return rows


def cost_pane_lines(snap: "dict | None") -> list[str]:
    """The Cost readout — CUMULATIVE figures from the SAME status snapshot the
    plain path's cost chip reads: the 3-scope × 5-row breakdown table, the token
    counters, and the cumulative cache-hit line.

    The counterpart Ctx pane deliberately shows CURRENT state only; cumulative
    belongs here. Restored from the retired chip bar's ``_cost_expansion``
    (#3338) — the snapshot always carried ``cost_breakdown_*`` /
    ``session_cached_tokens``, this surface simply stopped reading them."""
    snap = snap or {}
    p, c, _t = snap.get("usage", (0, 0, 0))
    agent_tokens = snap.get("agent_tokens", _t)
    cached = snap.get("session_cached_tokens", 0)
    return [
        *_cost_breakdown_table(snap),
        f"tokens   prompt {p:,} · completion {c:,} · total {agent_tokens:,}",
        _cache_hit_line("cache", cached, p, note="cumulative"),
    ]


def ctx_pane_lines(snap: "dict | None") -> list[str]:
    """The Ctx readout — CURRENT state only (cumulative figures live in the Cost
    pane instead, see :func:`cost_pane_lines`).

    Two DISTINCT figures, kept visually separated so they never collapse back into
    one ambiguous number:

    - ``window`` / ``prompt`` / ``free`` / ``cache`` — the REAL last-call size
      against the model's REAL context limit ("how close to the hard limit").
    - ``compaction`` — the compaction subsystem's OWN lightweight estimate (history
      only, excl. system prompt/tools) against ITS internal trigger threshold
      (already SP/head/tail-adjusted). A smaller, already-adjusted number; NOT
      comparable to the block above.

    ``ctx_compaction_status_fn`` is called LAZILY, here — ``_snapshot()`` stores the
    bound method rather than its result precisely because
    ``Session.context_window_status()`` is expensive (json.dumps + a token estimate
    of the full router-view history) and ``_snapshot()`` runs on every render frame.
    This function therefore runs only when the Ctx pane is actually being built:
    on open, and (#3338 liveness) on frame arrival while that ONE tab is open —
    never per render frame, and never for a tab that is not open."""
    snap = snap or {}
    window = snap.get("ctx_window", 0)
    prompt_tokens = snap.get("ctx_used", 0)
    free = max(0, window - prompt_tokens)
    pct = round(100 * prompt_tokens / window) if window > 0 else 0
    recent_prompt, recent_cached = snap.get("ctx_recent_usage", (0, 0))
    status_fn = snap.get("ctx_compaction_status_fn")
    status = status_fn() if status_fn is not None else {}
    comp_trigger = status.get("effective_trigger", 0)
    comp_est = max(0, comp_trigger - status.get("free_window", 0))
    comp_pct = round(100 * comp_est / comp_trigger) if comp_trigger > 0 else 0
    return [
        f"window       {window:,} tokens  ({snap.get('ctx_source', 'unknown')})",
        f"prompt       {prompt_tokens:,} tokens  ({pct}% of window)",
        f"free         {free:,} tokens",
        _cache_hit_line("cache", recent_cached, recent_prompt),
        f"compaction   {comp_est:,} / {comp_trigger:,} tokens est.  ({comp_pct}% to trigger)",
        f"             {_ctx_bar(prompt_tokens, window)}  {_ctx_pct(snap)}",
    ]


def help_pane_lines(
    app_bindings: "Iterable[tuple[str, str]]" = (),
    *,
    composer_keys: "Sequence[tuple[str, str]]" = tuple(COMPOSER_KEYS),
    menubar_keys: "Sequence[tuple[str, str]]" = tuple(MENUBAR_KEYS),
    sentqueue_keys: "Sequence[tuple[str, str]]" = tuple(SENTQUEUE_KEYS),
) -> list[str]:
    """The Help readout — the app's declarative ``BINDINGS`` (passed as
    ``(key, description)`` pairs) plus the imperative composer/menu navigation
    keys and the sent-queue's own navigation keys (#3300 Y-client) each widget
    owns. Not a registry-enumeration pane: key handling is split between
    declarative ``BINDINGS`` and imperative ``_on_key`` overrides, so the keys
    are sourced from where they are DEFINED (the widgets' key constants + the
    app's BINDINGS) rather than a single enumerable table."""
    lines = ["Shortcuts"]
    lines += [f"  {key}  {desc}" for key, desc in composer_keys]
    lines += [f"  {key}  {desc}" for key, desc in menubar_keys]
    lines += [f"  {key}  {desc}" for key, desc in sentqueue_keys]
    lines += [f"  {key}  {desc}" for key, desc in app_bindings]
    return lines


#: Every ACTIONABLE pane, keyed by tab id → a builder producing that pane's
#: ``(row, slash-command)`` entries from the status snapshot. :func:`pane_payload`
#: projects the rows and :func:`pane_commands` the commands from the SAME list, so
#: an ``OptionSelected.option_index`` can never address the wrong command (the two
#: cannot drift into different orderings/lengths by construction). A row with an
#: empty command is inert (a read-only fallback listing).
_PANE_ENTRY_BUILDERS: "dict[str, object]" = {
    "model": lambda s: _model_pane_entries(
        s.get("model_classes") or [],
        s.get("model_active_class") or s.get("model"),
    ),
    "agent": lambda s: _agent_pane_entries(
        s.get("agent_names") or [], s.get("attached_name"), s.get("session_tree") or []
    ),
    "tool": lambda s: _visibility_pane_entries(s, "tool", None),
    "mcp": lambda s: _visibility_pane_entries(s, "mcp", "mcp_servers"),
    "skill": lambda s: _visibility_pane_entries(s, "skill", "skills"),
    "hook": _hook_pane_entries,
}


def pane_commands(tab_id: str, snapshot: "dict | None" = None) -> list[str]:
    """The slash command parallel to each row of ``tab_id``'s pane — index-aligned
    with :func:`pane_payload`'s rows for the same ``snapshot``, ``[]`` for a pane
    with no actionable rows. An empty string marks an inert row (a read-only
    fallback listing that has no toggle to dispatch).

    This is what makes the restored categories OPERABLE rather than merely visible
    (#3338): the app maps a selected row straight onto ``/model`` / ``/attach`` /
    ``/session switch`` / ``/visibility`` / ``/hook`` and submits it through the
    same transport seam a typed slash uses."""
    builder = _PANE_ENTRY_BUILDERS.get(tab_id)
    if builder is None:
        return []
    return [cmd for _row, cmd in builder(snapshot or {})]  # type: ignore[operator]


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
    builder = _PANE_ENTRY_BUILDERS.get(tab_id)
    if builder is not None:
        return [row for row, _cmd in builder(snap)]  # type: ignore[operator]
    if tab_id == "history":
        return history_pane_options(history)
    if tab_id == "menu":
        return menu_pane_options(commands)
    if tab_id == "cost":
        return cost_pane_lines(snap)
    if tab_id == "ctx":
        return ctx_pane_lines(snap)
    if tab_id == "pipe":
        return pipe_pane_lines(snap)
    if tab_id == "cron":
        return cron_pane_lines(snap)
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
    (see :meth:`TextualChatApp._refresh_pane`).

    Two rendering idioms co-exist here, deliberately NOT interchangeable:
    the readout branch wraps in a Rich :class:`~rich.text.Text` LITERAL
    (never markup-parsed regardless of content — Cost/Ctx/Help are always
    internal-only figures, so this is fidelity-safe as-is); the History
    OptionList branch wraps in :class:`~textual.content.Content`
    (:func:`_history_option_content`) because ``OptionList`` DOES
    markup-parse a bare ``str`` option — do not "simplify" one branch to
    match the other, they guard against different widgets' different
    default behaviors."""
    if pane_is_list(tab_id):
        options = (
            _history_option_content(rows)
            if tab_id == _USER_CONTENT_LIST_PANE
            else rows
        )
        return OptionList(*options, id=tab_id)
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
