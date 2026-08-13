"""Composer + bottom-chrome tab-drawer widgets for the Textual chat surface.

The :class:`Composer` is the multi-line Claude-Code-style input (Enter submits,
Shift+Enter newlines) and the driver of the ``/``-command / ``:``-skill
completion popup (:mod:`~reyn.interfaces.inline.textual_chat.completion`, #3354
— see :class:`Composer` for the key contract and why ``↑``/``↓`` change meaning
while it is open). The bottom chrome (Phase 3) is a slim :class:`StatusLine`
of ``model │ agent │ cost │ ctx`` values plus a focusable :class:`MenuBar` (a
WRAPPING row of :class:`~textual.widgets.Tab` items — see :func:`pack_menu_rows`
for why it wraps rather than scrolls); opening a menu item expands a
``ContentSwitcher`` drawer whose
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
- **Artifacts** (#4482 PR-3) — generated files the terminal can't render
  natively (html/office/pdf/images), newest-first, derived from the SAME
  ``self.conversation`` model History reads (never ``Session.history``'s own
  resident buffer — see :meth:`TextualChatApp._artifact_rows`'s own docstring
  for why the two are not interchangeable). Each openable row carries an
  ``/open <ref>`` command (:func:`pane_commands`) that launches the OS's own
  default app on the resolved path.
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
  select/cancel/back-to-composer keys, #3300 Y-client). :data:`RESERVED_KEYS`
  is the fifth key surface and the one the Help pane does NOT show: keys
  claimed by an approved-but-unimplemented feature, recorded so a new binding
  cannot silently take one (#3352).

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

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.containers import Horizontal
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import (
    OptionList,
    Static,
    Tab,
    TextArea,
)

from reyn.config.chat import TuiConfig

from .completion import CompletionPopup
from .intervention_panel import InterventionPanel
from .presenter import option_content_rows
from .sent_queue import SentQueue

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from reyn.core.present.artifact_list import ArtifactRow
    from reyn.interfaces.slash import SlashCommand


class Composer(TextArea):
    """Multi-line Claude-Code-style input: **Enter submits**, **Shift+Enter**
    inserts a newline (the inverse of ``TextArea``'s default), auto-growing up to
    ``MAX_ROWS`` then internally scrolling. Every other key falls through to the
    base ``TextArea`` bindings unchanged.

    **Completion (#3354).** Typing ``/`` or ``:`` opens the
    :class:`~reyn.interfaces.inline.textual_chat.completion.CompletionPopup`
    above the input row. The popup is non-focusable, so focus never leaves this
    widget and this ``_on_key`` stays the single owner of every keystroke —
    which is exactly why the key contract has to be decided here rather than
    split across two focus targets:

    - ``↑``/``↓`` move the popup's highlight **while the popup is open**, and
      keep their existing #3314/#3277/#3327 routing (pending intervention →
      sent-queue upward, MenuBar downward, cursor otherwise) whenever it is
      NOT. This is a strict PRIORITY override, not a replacement: an open popup
      is a modal-ish, transient list the user just summoned by typing, and the
      regions those arrows otherwise reach are all still one ``Esc`` away.
    - ``Tab`` accepts the highlighted candidate — and ONLY while the popup is
      open. ``Tab`` is NOT a free key: ``TextArea`` defaults to
      ``tab_behavior="focus"`` (measured, ``textual/widgets/_text_area.py``) so
      the key bubbles to ``Screen``'s ``Binding("tab", "app.focus_next")``
      (``textual/screen.py``), which is how the composer currently reaches the
      MenuBar. Intercepting it conditionally BORROWS it for the duration of a
      menu and leaves focus-cycling intact the rest of the time.
      ``Enter`` deliberately does NOT accept (the retired prompt_toolkit
      completer bound both, the retired Textual ``SlashPicker`` bound only
      Tab): here Enter SENDS, so binding it to accept would silently swap a
      fully-typed command for whichever row happened to be highlighted instead
      of sending what the user typed.
    - ``Esc`` dismisses the popup without touching the drawer (the app-level
      ``escape`` → ``close_drawer`` binding only sees the key once the popup is
      closed), and the dismissal is STICKY for that token — see
      :meth:`~reyn.interfaces.inline.textual_chat.completion.CompletionPopup.sync`.

    All three interceptions are gated on
    :attr:`~reyn.interfaces.inline.textual_chat.completion.CompletionPopup.owns_keys`,
    not on mere visibility: a ``/cmd `` usage-hint popup with no candidates
    behind it (#3364) draws its row without taking a single key, so ``Tab`` and
    ``↑`` keep their normal meanings for the whole time the user is typing that
    command's arguments.

    Every one of these keys is registered in :data:`COMPOSER_KEYS`, the Help
    pane's single source of truth (#3314) — an unlisted key is undiscoverable.
    ``↑``/``↓`` appear TWICE there on purpose, once per state.

    **The menu is opened by TYPING, never by buffer contents.** Completion is
    recomputed only for a text change a KEY caused (:attr:`_EDIT_KEYS` /
    ``event.is_printable``, tracked through :meth:`_note_edit_key` and consumed
    once in :meth:`on_text_area_changed`). Programmatic writes —
    ``_restore_cancelled_text`` restoring a cancelled ``/command`` into the box
    (#3300 Y-client), a session-switch reset — must NOT pop a menu the user did
    not ask for, and additionally :meth:`clear_and_reset` closes it outright.
    """

    MAX_ROWS = 6

    #: Keys that EDIT the document without being printable, so a completion
    #: recompute must follow them. Textual routes these through ``BINDINGS`` →
    #: actions that run AFTER ``_on_key`` returns (measured: a sync at the end of
    #: ``_on_key`` still sees the pre-backspace text), which is why the recompute
    #: is driven off the resulting ``Changed`` message rather than from here.
    #: ``shift+enter`` is included so inserting a newline — which disables
    #: completion entirely — closes an open menu.
    _EDIT_KEYS = frozenset({
        "backspace", "delete", "shift+enter",
        "ctrl+w", "ctrl+u", "ctrl+k", "ctrl+x", "ctrl+f",
    })

    class Submitted(Message):
        """Posted when the user presses Enter with non-blank content."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def on_mount(self) -> None:
        self.show_line_numbers = False
        self._sync_height()

    def text_before_cursor(self) -> str:
        """The text from the start of the document up to the cursor — the input
        every completion decision is made from (a completion completes what is
        BEHIND the caret, never what a later line happens to contain)."""
        return self.get_text_range((0, 0), self.cursor_location)

    def _popup(self) -> "CompletionPopup | None":
        """The app's completion popup, or ``None`` when this composer is mounted
        without one (the widget is optional chrome; every completion path
        no-ops rather than requiring it)."""
        found = self.app.query(CompletionPopup)
        return found.first() if found else None

    def _note_edit_key(self, event: events.Key) -> None:
        """Arm ONE completion recompute if this key can edit the document.

        A counter rather than a boolean: :meth:`on_text_area_changed` consumes
        the arm by matching it, so each qualifying keypress permits at most one
        recompute and a ``Changed`` with no keypress behind it (a programmatic
        write) permits none. A non-editing key (an arrow) never arms, so it
        cannot leave the gate open for a later programmatic write."""
        if event.is_printable or event.key in self._EDIT_KEYS:
            self._edit_key_seq = getattr(self, "_edit_key_seq", 0) + 1

    def _sync_completion(self) -> None:
        """Recompute + push the completion state for the current caret position.

        The app owns resolving the live sources (slash registry, session,
        skills) — this widget only asks for the state and hands it to the
        popup, so a composer mounted in an app without that hook simply never
        completes."""
        popup = self._popup()
        state_fn = getattr(self.app, "completion_state", None)
        if popup is None or state_fn is None:
            return
        popup.sync(state_fn(self.text_before_cursor()))

    def _accept_completion(self, popup: CompletionPopup) -> None:
        """Replace the typed prefix with the highlighted candidate.

        Replaces exactly ``prefix_len`` characters immediately before the caret
        (the sigil and everything left of the token survive) via the SAME
        ``_replace_via_keyboard`` seam Shift+Enter uses, so undo history and the
        ``Changed`` message behave like ordinary typing — which is also what
        re-arms the menu for the NEXT stage (``/model `` → its argument list).

        A no-match menu has nothing highlighted: the key is still consumed (a
        visible menu owning Tab is more predictable than Tab silently moving
        focus out from under one) but nothing is inserted."""
        candidate = popup.selected()
        state = popup.state()
        if candidate is None:
            popup.close()
            return
        row, col = self.cursor_location
        start = (row, max(0, col - state.prefix_len))
        popup.close()
        self._replace_via_keyboard(
            candidate.value + state.accept_suffix, start, (row, col)
        )

    async def _on_key(self, event: events.Key) -> None:
        popup = self._popup()
        if popup is not None and popup.owns_keys:
            if event.key in ("up", "down"):
                event.stop()
                event.prevent_default()
                popup.move_selection(-1 if event.key == "up" else 1)
                return
            if event.key == "tab":
                event.stop()
                event.prevent_default()
                self._accept_completion(popup)
                return
            if event.key == "escape":
                event.stop()
                event.prevent_default()
                popup.dismiss_current()
                return
        self._note_edit_key(event)
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
        if event.key in ("pageup", "pagedown"):
            # #3470: PageUp/PageDown scroll the CONVERSATION, unconditionally —
            # never the composer's own text. TextArea's default binds these to
            # page-sized cursor jumps, which in a <= MAX_ROWS-tall chat box has
            # no practical value; meanwhile "scroll back through the chat" had
            # NO discoverable key at all (the only route was an undocumented
            # Shift+Tab focus hop into the conversation pane, with no visual
            # cue that focus had left the composer). Unconditional delegation
            # keeps one meaning per key (#3365 principle: a key's meaning does
            # not change with state), and focus never leaves the composer.
            # The pane is queried by type NAME so this module keeps its
            # import-isolation (textual_flowview stays out of chrome.py —
            # pinned by test_phase3_chrome_imports_stay_tty_only).
            flow = self.app.query("FlowView")
            if flow:
                event.stop()
                event.prevent_default()
                if event.key == "pageup":
                    flow.first().scroll_page_up(animate=False)
                else:
                    flow.first().scroll_page_down(animate=False)
                return
        await super()._on_key(event)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._sync_height()
        # Consume the arm a qualifying keypress left (see :meth:`_note_edit_key`).
        # An unmatched arm means this change came from a programmatic write, and
        # completion stays exactly as it was — the menu is a response to TYPING.
        armed = getattr(self, "_edit_key_seq", 0)
        if armed != getattr(self, "_synced_key_seq", 0):
            self._synced_key_seq = armed
            self._sync_completion()

    def _sync_height(self) -> None:
        wrapped_rows = max(self.wrapped_document.height, 1)
        self.styles.height = min(wrapped_rows, self.MAX_ROWS)

    def clear_and_reset(self) -> None:
        self.text = ""
        self._sync_height()
        # A submitted turn empties the box; an empty box completes nothing, and
        # leaving a stale popup up would keep swallowing ↑/↓ after the text that
        # justified it is gone.
        popup = self._popup()
        if popup is not None:
            popup.close()


# ── bottom-chrome tab-drawer ─────────────────────────────────────────────────
# Default collapsed = a slim status-values line + a focusable menu row. Pressing
# ↓ from the composer focuses the menu; opening an item expands a drawer
# DOWNWARD. Interactive panes are Textual OptionLists (Model/Agent/History/Menu —
# keyboard selection); static readouts are plain Rich in a Static (Cost/Ctx/Help)
# — the "Textual only where there is a selection" split. Phase 4 fills each pane
# from its canonical reyn source (see :func:`pane_payload`).

#: Tab labels, abbreviated where needed to leave room for the status-values
#: line to co-reside on the menu row (#3326). Each abbreviation stays uniquely
#: identifiable on its own (an accepted condition, not just a nice-to-have —
#: e.g. ``History`` -> ``Hist`` reads unambiguously; ``Ctx``/``Cost`` are left
#: alone precisely because shortening either risks the two becoming
#: indistinguishable at a glance). Most labels were already <= 4 chars and are
#: untouched. #3469: ``Skill`` is NOT abbreviated — the #3326 4-char cut made
#: it read as a typo ("Skil"), and the full label still packs all 13 tabs onto
#: one row at 80 columns (measured: 79 cells used — ``History``'s 3-char
#: saving was the one that mattered).
_MENU_TABS: "list[tuple[str, str]]" = [
    ("model", "Model"),
    ("agent", "Agent"),
    ("history", "Hist"),
    # #4482 PR-3: generated artifacts (html/office/pdf/images — anything the
    # terminal can't render natively) the operator can open with the OS's own
    # default app. "Art" reads unambiguous next to "Agent" here (distinct
    # first two letters, Ar- vs Ag-), matching this row's own "each
    # abbreviation stays uniquely identifiable on its own" bar.
    ("artifacts", "Art"),
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
    # #4542 (owner ruling): compact glyphs, not "Menu"/"Help" full words —
    # same flat arrow-navigable tab, same Enter-opens-drawer mechanism as
    # every other entry above; only the visible label shrinks. Distinct
    # single characters (not the SAME "…" twice) so #3338's on-screen
    # geometry test (every tab id maps 1:1 to a real, distinguishable Tab
    # widget) has something to distinguish rather than two identical labels.
    ("menu", "…"),
    ("help", "?"),
]

#: Menu items whose pane is an interactive :class:`OptionList` picker (keyboard
#: selection); every other tab renders as a read-only Rich :class:`Static`.
#:
#: **Known display limitation (Model/Agent/Menu, NOT a security gap):**
#: ``OptionList`` markup-parses a bare ``str`` option — the panes in
#: :data:`_LITERAL_ROW_PANES` get the :func:`_literal_option_content`
#: ``Content``-literal wrap (History because it carries LLM-/user-derived
#: conversation text — #3302 fix-class; Tool/MCP/Skill/Hook because their rows
#: carry reyn's own bracket markers — #3380). Model/Agent/Menu rows are operator/config-derived
#: identifiers (configured model classes, loaded agent names, the
#: ``@slash``-registered command table) — not live conversation content, so
#: they are NOT neutralize-relevant and are left unwrapped. If an operator
#: ever names a model class / agent / slash command with a `[...]`-shaped
#: substring, THAT row's display will visually corrupt (the same bracket-
#: eating rendering quirk, not an injection risk) — a known, accepted
#: limitation of leaving those three panes unwrapped, not a claim that they
#: are immune to the rendering quirk.
_LIST_PANES = frozenset({
    "model", "agent", "history", "artifacts", "menu", "tool", "mcp", "skill", "hook",
})

# ── the Help pane's key tables ───────────────────────────────────────────────
#
# These are DELIBERATELY not written as ``<key> to <verb>``, the shape #3801
# gave every inline key hint in this interface (``enter to send · shift+enter
# to break the line…``, ``enter to check out · esc to cancel``). Owner ruling:
# a table already puts the key and its meaning in separate columns, so
# inserting "to" adds a word to every row that the layout is already saying,
# and the column of verbs stops lining up as a column.
#
# Recorded here rather than only in the ticket because the difference is
# visible from the tables and the reason is not: someone sweeping this
# interface for the #3801 shape will find these, see hints elsewhere written
# the other way, and file them as the leftovers. They are not.
#
# What the ruling does NOT license is spelling one key two ways — that was
# raised separately (#3805) and settled: key names in these tables are
# LOWERCASE, matching how the inline hints spell them. The tables differ from
# the hints in SHAPE only, never in how a key is written.

#: The composer's navigation keys, co-located with the widget that OWNS them (they
#: are imperative ``Composer._on_key`` overrides, not declarative ``BINDINGS``, so
#: the Help pane sources them from here rather than re-hardcoding a second copy).
COMPOSER_KEYS: "list[tuple[str, str]]" = [
    ("enter", "send"),
    ("shift+enter", "newline"),
    # #3470: the conversation's scroll keys, usable WITHOUT leaving the
    # composer (delegated in ``Composer._on_key`` — focus never moves).
    ("pgup / pgdn", "scroll conversation"),
    ("↓", "focus menu"),
    # #3327: ↑ now targets whichever of the two regions above the composer
    # actually has something to act on, pending intervention first — see
    # ``Composer._on_key``'s "up" branch for the exact priority/fallback.
    ("↑", "focus pending intervention (else sent queue)"),
    # #3354: the / and : completion popup. ↑/↓ are LISTED TWICE on purpose —
    # they genuinely do two different things depending on whether the popup is
    # open, and the Help pane is where a user learns that; collapsing the two
    # into one row would hide the state-dependence that makes the routing
    # predictable (and hide it in the one place #3314 designated as the source
    # of truth for what a key does).
    ("/ or :", "open completion"),
    ("↑ ↓", "move completion selection (while completing)"),
    ("tab", "accept completion (while completing)"),
    ("esc", "dismiss completion"),
]

#: The menu row's navigation keys (imperative ``MenuBar._on_key`` overrides).
#:
#: #3365: ``↑`` and ``esc`` used to share one row worded "close" — read as
#: "closes the drawer, landing on the tab-bar" (one level up), but the ACTUAL
#: destination (measured, both keys) is the Composer directly, regardless of
#: navigation depth. Split into two rows with accurate wording instead of one
#: combined row that invited a "one step back" misreading.
MENUBAR_KEYS: "list[tuple[str, str]]" = [
    ("← →", "move"),
    ("enter", "open"),
    # #3699: a readout pane taller than the drawer's cap scrolls, and until
    # this row existed the Help pane did not say how — the pane whose content
    # was cut off was also the pane that would have told you how to see the
    # rest. PgUp/PgDn rather than ↑/↓ because ↑ already means "back to
    # composer" here (the row below), and this app already uses PgUp/PgDn for
    # "page through content" on the conversation.
    ("pgup / pgdn", "scroll this pane"),
    ("↑", "back to composer"),
    ("esc", "back to composer"),
    # Owned HERE rather than sourced from the app's ``BINDINGS`` (#3818). The
    # binding still exists and still does the work — but Textual identifies the
    # key as ``escape``, and rendering the identifier put ``escape  Close
    # drawer`` directly under ``esc  back to composer``: one key, two
    # spellings, one screen. reyn already owns how a key is written (every
    # other row in every one of these tables), so the fix is to let it own this
    # one too rather than to translate at the last moment.
    ("esc", "close drawer"),
]

#: Keys RESERVED by an approved-but-unimplemented feature — claimed, but bound
#: nowhere in the current tree (#3352).
#:
#: This table exists because a key-collision sweep over live bindings CANNOT
#: see this class of claim: the feature's implementation was deleted and its
#: key survives only in an issue. A new binding that takes one of these looks
#: clean in every grep and then collides the day the feature lands. Entries
#: carry the issue that owns them, and are REMOVED when the feature either
#: lands (the key becomes a live binding) or is dropped (the claim dies).
#:
#: The table is only half of the mechanism — what ENFORCES it is
#: ``test_neither_gutter_key_collides_with_any_other_key_the_app_can_see``,
#: which intersects this dict with the live binding set and fails on any
#: overlap. That gate is a REQUIRED part of this declaration: recording the
#: claim makes it findable, not true (the gate's own first draft folded this
#: dict into the "taken" set and membership-tested only the two new keys,
#: which left the reservation itself completely undefended while reading, in
#: prose, as though it were protected).
#:
#: Only claims backed by an OPEN issue belong here. The retired Textual TUI's
#: other keys (``ctrl+g`` find-next, ``ctrl+t`` rewind-menu edit, ``ctrl+b``/
#: ``ctrl+o``/``ctrl+w`` panel, ``ctrl+1``..``ctrl+7`` tab jump, ``f3``/``f4``/
#: ``f7``/``f9``) are NOT listed: #2193 was re-scoped to voice alone, so those
#: features are explicitly dropped and their keys are free.
#:
#: #4187: voice input LANDED (F2, declared in ``app.py``'s own ``BINDINGS`` —
#: findable there, no reservation needed) — so its entry is removed from here
#: rather than left stale. ``ctrl+r``, the retired TUI's primary binding for
#: the same feature, is NOT re-claimed: #4187 measured it as colliding with
#: reverse-history-search, a terminal-wide convention the Composer's own
#: users bring with them (see ``app.py``'s ``BINDINGS`` comment on the F2
#: entry for the full reasoning). It is free, not reserved — nothing has an
#: open claim on it.
RESERVED_KEYS: "dict[str, str]" = {}

#: The sent-queue region's navigation keys (#3300 Y-client,
#: ``SentQueue.BINDINGS`` — declarative, but the Help pane still sources them
#: from HERE, the same single-source-of-truth convention ``MENUBAR_KEYS``
#: uses, rather than re-deriving prose from the ``Binding`` objects).
#:
#: #3365: ``tab`` dropped — its "back to composer" binding was removed
#: (``Tab`` is forward-only everywhere in the app; ``Esc`` alone owns "back").
SENTQUEUE_KEYS: "list[tuple[str, str]]" = [
    ("↑ / ↓", "select queued message"),
    ("enter", "cancel selected"),
    ("esc", "back to composer"),
]

#: The search bar's keys while it is open (#3476 ⑤,
#: ``search_bar.SearchBar`` — imperative ``on_key`` + the Input's own Enter,
#: sourced from HERE for the Help pane per the same single-source-of-truth
#: convention as the sibling ledgers above). The bar itself opens from the
#: app's declarative ``ctrl+n`` binding (#3692 PR-B ③, moved off ``ctrl+f``
#: once flowview 0.13 gave that key its own meaning), which the Help pane
#: already lists via ``app_bindings``.
SEARCHBAR_KEYS: "list[tuple[str, str]]" = [
    ("enter / ↑", "search: older match"),
    ("shift+enter / ↓", "search: newer match"),
    ("esc", "close search, back to composer"),
]

#: The conversation pane's keyboard cursor (#3476 ⑥, reached the SAME way as
#: ``SENTQUEUE_KEYS`` — Shift+Tab focus-cycling, ``app.py``'s ``selectable=True``).
#: ↑/↓/PageUp/PageDown/Home/End are flowview's OWN built-in cursor bindings
#: (not re-declared here — this ledger only lists what reyn adds on top).
CONVERSATION_CURSOR_KEYS: "list[tuple[str, str]]" = [
    ("↑ / ↓ / pgup / pgdn / home / end", "move cursor"),
    ("enter / space", "copy entry to clipboard"),
    ("r", "open /rewind"),
    ("esc", "back to composer"),
]


def pane_is_list(tab_id: str) -> bool:
    """Whether ``tab_id``'s drawer pane is an interactive :class:`OptionList`
    picker (vs a read-only :class:`Static` readout)."""
    return tab_id in _LIST_PANES


#: The ONE list pane whose rows are LLM-/user-derived content (recent
#: conversation turns) rather than operator/config-derived identifiers —
#: see :func:`_literal_option_content`'s docstring for why this tab needs the
#: fidelity wrap for a REASON the marker panes below do not share.
_USER_CONTENT_LIST_PANE = "history"

#: List panes whose rows must reach :class:`~textual.widgets.OptionList` as
#: ``Content`` literals. Two independent reasons, one mechanism:
#:
#: - ``history`` — the row text is conversation content (fidelity/#3302).
#: - ``tool`` / ``mcp`` / ``skill`` / ``hook`` — the row text is OURS and
#:   contains bracket markers (``[on]`` / ``[off]`` / ``[--]``) that the markup
#:   parser eats. Witnessed in a real TTY on #3380: every ``[on]``/``[off]``
#:   marker was invisible, so a ``/visibility``-hidden capability rendered
#:   identically to an available one and #3379's "two axes, two markers" reduced
#:   to one visible axis. ``[--]`` survived only because it is not valid markup —
#:   which is luck, not a design.
#:
#: Model/Agent/Menu stay unwrapped: their rows are config-derived identifiers
#: that carry no marker of ours, so the quirk only reaches them if an operator
#: names something with a ``[...]``-shaped substring (the accepted limitation
#: documented at ``_LIST_PANES``).
_LITERAL_ROW_PANES = frozenset({
    _USER_CONTENT_LIST_PANE, "artifacts", "tool", "mcp", "skill", "hook",
})


def pane_needs_literal_rows(tab_id: str) -> bool:
    """Whether ``tab_id``'s rows must be wrapped by :func:`_literal_option_content`
    before reaching :class:`~textual.widgets.OptionList` (see
    :data:`_LITERAL_ROW_PANES`). Both call sites — the initial build and the
    refresh — ask this ONE predicate, so a pane cannot be wrapped on one path and
    bare on the other."""
    return tab_id in _LITERAL_ROW_PANES


def _literal_option_content(rows: "Sequence[str]") -> list[Content]:
    """Wrap each row in a literal :class:`~textual.content.Content`
    — never a bare ``str`` handed to :class:`~textual.widgets.OptionList`.

    ``OptionList`` markup-parses a bare ``str`` option exactly like
    ``Static``/``RadioButton`` do (``Option.prompt`` → ``textual.visual.
    visualize(..., markup=True)`` by default, unset here) — the SAME
    ``#3302`` bracket-eating class, just reached through a different widget.
    Which panes need it, and why, is :data:`_LITERAL_ROW_PANES` — History
    because its text is conversation content
    (:func:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp.
    _history_turns`, already neutralized — ESC/control strip — at that source),
    the visibility panes because reyn's OWN ``[on]``/``[off]``/``[--]`` markers
    are bracket-shaped and were being eaten (#3380, witnessed in a real TTY).

    Two call sites need this identically — the initial build
    (:func:`build_drawer_pane`, at ``compose`` time) and the refresh
    (``TextualChatApp._refresh_pane``, on every drawer re-open) — a fresh
    History tab was, before this fix, safe at ONE of those and broken at
    the other depending on which code path last touched it.

    The wrap itself now lives in
    :func:`~reyn.interfaces.inline.textual_chat.presenter.option_content_rows`
    (#3354 gave it a second consumer — the completion popup, whose ``/image``
    candidates are filesystem names). This function stays as the drawer-pane
    NAME for it, carrying the "which panes, and why" reasoning above; the
    mechanism is shared so the two consumers cannot drift into one being safe
    and the other not."""
    return option_content_rows(rows)


# ── per-pane pure formatters (registry inputs → display strings) ──────────────
# Each takes plain data (never a widget) and returns ``list[str]``, so the
# derive-from-registry completeness of the enumerating panes is directly testable
# without mounting Textual. A pane that enumerates a set (model/agent/menu) MUST
# render its FULL input — never a hand-curated subset — so a new registry entry
# surfaces automatically.


def _model_pane_entries(
    classes: "Sequence[str]", active: "str | None"
) -> "list[tuple[str, str]]":
    """``(row, slash)`` per operator-configured model class, active class marked.

    #3324: ``active`` can be a raw LiteLLM model string rather than a
    configured class name — when ``--model <raw-id>`` bypasses the class
    system entirely (``Session.active_model_class()`` returns ``None`` for a
    passthrough model, so the caller falls back to the raw model string).
    That string never equals any class name, so the ``· active`` marker
    silently appeared nowhere. Prepended here as its own informational row
    (empty command = inert, the same convention read-only panes use) rather
    than left unmarked."""
    entries = [
        (f"{c}  · active" if c == active else c, f"/model {c}") for c in classes
    ]
    if active is not None and active not in classes:
        entries.insert(0, (f"(current, not a configured class)  {active}", ""))
    return entries


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


@dataclass(frozen=True)
class DrawerRow:
    """One actionable drawer row, as slots rather than as an assembled string
    (#3691 Phase 2).

    The four capability panes (tool / mcp / skill / hook) each built their rows
    by hand and each spelled the same grammar out again: a state mark, the name,
    an optional ``·``-separated note, and a slash command. Four spellings of one
    grammar is four places for a change to reach three of. #3380 added the
    denial-reason distinction and #3615 a third reason — both had to be applied
    where the mark was decided, and the mark was decided in more than one place.

    Slots, not a redesign. The rendered text is byte-identical to what the four
    builders produced; what changes is that a reader (and the next change) sees
    ``state`` / ``note`` / ``command`` as separate things instead of positions
    inside a string.

    ``command=None`` means **not operable** — a denied capability, or a
    read-only fallback listing. The builders previously used ``""`` for this,
    which is a value the caller has to know means "inert" rather than a type
    that says so. The registry boundary still hands out ``""`` (see
    :meth:`as_entry`), because ``pane_commands``' index-aligned contract is a
    list of strings and changing that is a different change from this one.

    NOT used by the model / agent panes. Their rows are a tree-and-selection
    grammar (indentation, a ``▸`` focus mark at two levels, "· active"), not a
    state grammar — giving them a ``state`` slot would be fitting the type to
    the refactor rather than to the code.
    """

    label: str
    state: "str | None" = None      # "on" | "off" | "--" — None = no state mark
    note: "str | None" = None       # the "· ..." tail: a denial reason, a scope
    command: "str | None" = None    # None = inert (denied, or a read-only row)

    @property
    def text(self) -> str:
        """The row as the pane renders it."""
        head = f"[{self.state}] {self.label}" if self.state else self.label
        return f"{head}  · {self.note}" if self.note else head

    def as_entry(self) -> "tuple[str, str]":
        """``(row, slash)`` for the registry, whose contract predates this type."""
        return self.text, self.command or ""


def _denied_note(reason: "str | None") -> str:
    """The annotation for a non-flippable ``[--]`` row, by ``denied_reason`` (#3380,
    ``"unknown"`` added by #3615).

    ``"turn_context"`` states the CONDITION rather than the fact, because the
    condition is also the remedy — the narrowing lifts when the untrusted entry
    leaves the active context, and an operator told only "denied" would go looking
    for a profile to edit that does not deny it. ``"unknown"`` (#3615) is neither a
    profile denial nor a lifting condition — the session's envelope source could not
    be read, so authorization could not be determined at all; saying "denied" here
    would claim a firmer answer than the read model actually has. An
    unrecognised/absent reason falls back to the envelope wording, which is what
    every pre-#3380 row meant."""
    if reason == "turn_context":
        return "denied while untrusted content is in context"
    if reason == "unknown":
        return "authorization could not be determined for this session"
    return "denied by capability profile"


def _visibility_pane_entries(
    snap: dict, kind: str, fallback_key: "str | None"
) -> "list[tuple[str, str]]":
    """``(row, slash)`` for one capability-visibility category (tool/mcp/skill).

    Session-backed ``visibility_items`` give togglable rows whose slash FLIPS the
    current state (``/visibility off …`` for an on item and vice versa). Until the
    session wires that state, fall back to the config-declared names as a read-only
    listing (empty command = the row dispatches nothing). ``fallback_key`` is
    ``None`` for tool, which has no config-declared name source.

    #3378 — **two axes, two markers.** ``[on]``/``[off]`` is the ``/visibility`` axis
    (user-flippable, so the row carries a slash). ``[--]`` is the ENVELOPE/CONTEXTUAL
    axis, which ``/visibility on`` cannot re-grant — so the row is marked
    distinguishably, annotated with the reason, and carries NO slash. Sharing the
    ``off`` marker between them would tell the operator to try a toggle that cannot
    work, which is the state the owner was in.

    #3380 — **the annotation names WHICH narrowing**, since the operator's next move
    differs. ``denied_reason="envelope"`` is durable for the session (edit the
    profile / topology binding). ``"turn_context"`` is the ephemeral ``_untrusted``
    narrowing, live only while untrusted external content sits in the active context
    — so the row says that condition, which is also how it clears (compaction /
    ``/clear``), rather than a bare "denied". Both marks are ``[--]`` because neither
    is flippable; the reason text is what distinguishes them.

    The whole pane is rebuilt from a fresh snapshot on every frame while it is open
    (#3338), and the turn-context row is derived from the LIVE conversation at read
    time, so no row here is an "as of an earlier turn" value that could outlive its
    cause without saying so.

    #3378 — **empty is two different states.** ``visibility_items is None`` means the
    frame carries no visibility seam (a remote read-model frame, or a session without
    the accessor) → "not wired". A present-but-empty list means the seam answered and
    nothing is narrowed → "(none)". These rendered identically before."""
    raw = snap.get("visibility_items")
    rows: "list[DrawerRow]" = []
    for it in (raw or []):
        if it.get("kind") != kind:
            continue
        if it.get("denied"):
            rows.append(DrawerRow(
                label=it["name"], state="--",
                note=_denied_note(it.get("denied_reason")),
            ))
        else:
            rows.append(DrawerRow(
                label=it["name"], state="on" if it["on"] else "off",
                command=f"/visibility {'off' if it['on'] else 'on'} {kind} {it['name']}",
            ))
    if rows:
        return [r.as_entry() for r in rows]
    names = [d["name"] for d in (snap.get(fallback_key) or [])] if fallback_key else []
    if names:
        return [DrawerRow(label=n).as_entry() for n in names]
    return [DrawerRow(label="(none)" if raw is not None else "(not wired)").as_entry()]


def _hook_pane_entries(snap: dict) -> "list[tuple[str, str]]":
    """``(row, slash)`` for the hook-applicability toggles — session-backed
    ``hook_items`` (each row's slash flips it via ``/hook on|off <name>``), else the
    config-derived hook labels as a read-only listing."""
    items = snap.get("hook_items") or []
    if items:
        return [
            DrawerRow(
                label=h["name"],
                state="on" if h["on"] else "off",
                note=h.get("scope") or None,
                command=f"/hook {'off' if h['on'] else 'on'} {h['name']}",
            ).as_entry()
            for h in items
        ]
    labels = [h["label"] for h in (snap.get("hooks") or [])]
    return [DrawerRow(label=label).as_entry() for label in labels] or [
        DrawerRow(label="(none)").as_entry()
    ]


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


def artifact_row_label(row: "ArtifactRow") -> str:
    """#4482 PR-3: one artifact's display row. Names the SAME thing
    :meth:`_handle_open_artifact_request` opens — architect's ruling
    ("表示から実行まで同じ path を使う") applies to what's shown here too.

    **Prefers `resolved_path` over bare `name`** (review fix, lead-coder/
    architect): `name` alone is a BASENAME, which cannot distinguish two
    same-named artifacts in different directories — the arc's one
    non-negotiable requirement is that the user sees the REAL thing about
    to open, and a basename does not satisfy that on its own. A ref is an
    opaque token with no meaning to the operator either, so neither
    ``name`` nor ``ref`` alone is enough; ``resolved_path`` (a
    project-root-relative path — see :func:`~reyn.core.present.
    artifact_list.resolve_display_paths`) is what's shown whenever it is
    available."""
    if row.error is not None:
        return f"✗ {row.name or '(unresolved)'} — {row.error}"
    if row.is_inline:
        return f"{row.name} (inline — already shown above)"
    return row.resolved_path or row.name


def artifact_pane_options(rows: "Sequence[ArtifactRow]") -> list[str]:
    """Rows for the Artifacts drawer pane — newest-first, already the order
    :func:`~reyn.core.present.artifact_list.collect_artifact_rows` returns."""
    return [artifact_row_label(r) for r in rows] if rows else ["(no artifacts yet)"]


def artifact_pane_commands(rows: "Sequence[ArtifactRow]") -> list[str]:
    """The ``/open <ref>`` command parallel to each row in
    :func:`artifact_pane_options` — empty string (non-actionable, same as
    History/Menu) for a row with nothing to open: an inline artifact (no
    real file — the content already reached the conversation pane
    directly) or an error marker (nothing resolved)."""
    return [
        f"/open {r.ref}" if r.ref is not None else ""
        for r in rows
    ]


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


#: The cost table's row labels, in render order. The label COLUMN is padded to
#: the longest of these (:data:`_COST_LABEL_W`) rather than each row relying on
#: its own literal's width — ``Output``/``Saved%`` are 6 chars while
#: ``Total``/``Input``/``Saved`` are 5, so the implicit-width form (which the
#: retired renderer used, and which this port faithfully inherited) shifted the
#: Output row's cells one column left of every other row. Deriving the width here
#: means adding or renaming a row later cannot re-break the alignment.
#: The table's LABEL column — the scope names, spelled out (#3691). The table
#: was transposed to make that possible: with scopes as COLUMNS they had to fit
#: a 9-character value column and were abbreviated to ``Ses``/``Agt``/``Prj``,
#: three strings a reader has to be taught. As rows they carry their own names,
#: and the metric names move to the header where they already fit.
#:
#: It also costs two fewer lines — a header plus three scopes instead of a
#: header plus five metrics — in a pane that competes for rows against every
#: other drawer pane through ``compact_caps`` (#3680).
_COST_ROW_LABELS = ("COST", "Session", "Agent", "Project")
_COST_LABEL_W = max(len(label) for label in _COST_ROW_LABELS)
_COST_COL_W = 9


def _cost_row(label: str, cells: "Sequence[str]") -> str:
    """One cost-table line: the label left-aligned in a FIXED-width label column,
    then each cell right-aligned in a fixed-width value column — so every row's
    value columns start at the same offset regardless of how long its label is."""
    return f"{label:<{_COST_LABEL_W}}" + "".join(
        f"{cell:>{_COST_COL_W}}" for cell in cells
    )


def _format_cost_cell(value: float, *, approx: bool) -> str:
    """Format one currency cell to fit within :data:`_COST_COL_W`, WITHOUT ever
    silently dropping a digit from the displayed number (#4544 bug A — the prior
    ``("~" + s)[:_COST_COL_W]`` byte-sliced the formatted string, so
    ``~$999.9999`` silently became ``~$999.999``: a DIFFERENT, wrong number that
    reads as a plausible rounding, not a truncation).

    Sheds decimal PRECISION in stages (4dp -> 3dp -> ... -> 0dp) until the
    correctly-ROUNDED string fits — each stage is a real number, never a
    byte-sliced fragment, and Python's own ``:.Nf`` rounding correctly carries
    a value like 999.9999 up to 1000 when it no longer fits at higher
    precision (verified: ``f"{999.9999:.2f}"`` == ``"1000.00"``, not a
    stale ``"999.99"``). ``approx`` prepends ``~`` — the SAME width budget
    applies to it, so an approx cell may show one fewer decimal than the
    corresponding exact cell right at the boundary; that is the ``~``
    character's own real width cost, not a defect.

    Used for every currency cell (Total / Input / Output / Saved), not just
    ``approx`` state, closing #4544 bug B in the same change (the Total/
    ok-state path never even attempted to fit the column before this fix) —
    architect's own review note: all states must share one formatter, since
    the approx-only special case was the asymmetry that caused bug A.

    Falls back to k/M/B unit abbreviation only for values so large that even
    0 decimal places doesn't fit (unreachable at today's real cost volumes,
    kept as a documented floor rather than an unbounded/undefined string)."""
    prefix = "~" if approx else ""
    for decimals in (4, 3, 2, 1, 0):
        s = f"{prefix}${value:.{decimals}f}"
        if len(s) <= _COST_COL_W:
            return s
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(value) >= divisor:
            scaled = value / divisor
            for decimals in (2, 1, 0):
                s = f"{prefix}${scaled:.{decimals}f}{suffix}"
                if len(s) <= _COST_COL_W:
                    return s
    # Unreachable at any realistic cost magnitude; an explicit "…" marker
    # (never a silent digit-chop) rather than an unbounded-width string.
    s = f"{prefix}${value:.0f}"
    return s if len(s) <= _COST_COL_W else s[: _COST_COL_W - 1] + "…"


def _cost_breakdown_table(snap: dict) -> list[str]:
    """The 3-row (Session/Agent/Project) × 5-column (Total/Input/Output/Saved/
    Saved%) cost breakdown table.

    Total is always the litellm-accurate authoritative figure (``cost_usd`` /
    ``cost_agent`` / ``cost_total`` — already computed via ``estimate_cost``,
    unaffected by the >200k breakdown limitation). Input/Output/Saved/Saved% are
    derived from the accumulated ``CostBreakdown`` per scope. Per-scope ``state``
    (see :func:`_cost_scope_state`) decides how the component cells render: exact
    (``ok``), ``~``-marked with a tiered-pricing footnote (``approx``), or ``—``
    with a DIFFERENT "unavailable" footnote (``unavail``) — never misattributed to
    tiered pricing.

    Every line is assembled through :func:`_cost_row`, so the value columns line
    up across rows by construction (see :data:`_COST_ROW_LABELS`)."""
    from reyn.llm.pricing import CostBreakdown

    session_total = snap.get("cost_usd", 0.0)
    scopes = [
        ("Session", snap.get("cost_breakdown_session") or CostBreakdown(), session_total),
        ("Agent", snap.get("cost_breakdown_agent") or CostBreakdown(),
         snap.get("cost_agent", session_total)),
        ("Project", snap.get("cost_breakdown_project") or CostBreakdown(),
         snap.get("cost_total", session_total)),
    ]
    header = _cost_row("COST", ["Total", "Input", "Output", "Saved", "Saved%"])

    per_scope = [
        (name, total, *_cost_scope_state(breakdown, total))
        for name, breakdown, total in scopes
    ]
    any_approx = any(state == "approx" for *_rest, state in per_scope)
    any_unavail = any(state == "unavail" for *_rest, state in per_scope)

    def _cell(value: float, state: str) -> str:
        if state == "unavail":
            return "—"
        return _format_cost_cell(value, approx=(state == "approx"))

    scope_rows = [
        _cost_row(
            name,
            [
                _format_cost_cell(total, approx=False),
                _cell(inp, state),
                _cell(out, state),
                _cell(sav, state),
                "—" if state == "unavail" else f"{round(100 * pct)}%",
            ],
        )
        for name, total, inp, out, sav, pct, state in per_scope
    ]

    rows = [header, *scope_rows]
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
    rows = [
        *_cost_breakdown_table(snap),
        f"tokens   prompt {p:,} · completion {c:,} · total {agent_tokens:,}",
        _cache_hit_line("cache", cached, p, note="cumulative"),
    ]
    # #3695: the status row can only afford a mark; this pane has room to say
    # what the mark means and how much of the total is unaccounted for. Absent
    # entirely when every call was priced, so it is never a line the reader has
    # to decide is irrelevant.
    unpriced = snap.get("cost_agent_unpriced_calls", 0)
    if unpriced:
        rows.append(
            f"unpriced {unpriced:,} call(s) had no published price — the total "
            f"above is a lower bound, not the amount spent"
        )
    return rows


#: Where every Ctx line's value starts. Named so the labels cannot drift out
#: of alignment one edit at a time — the cache row had already done so.
_CTX_LABEL_W = 13


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
    # The bar sits with the figure it draws (#3691). It has always visualised
    # the WINDOW's fill, and it used to be printed after the compaction line —
    # directly beneath "61% to trigger" while showing 42%. Two percentages, one
    # of them a bar, adjacent and unrelated: the misreading is not a risk, it is
    # the reading. #3691's own principle for this pane says the actual context
    # window and the compaction estimate are not comparable and must stay
    # visually separate; the layout was doing the opposite.
    return [
        f"window       {window:,} tokens  ({snap.get('ctx_source', 'unknown')})",
        f"prompt       {prompt_tokens:,} tokens  ({pct}% of window)",
        f"             {_ctx_bar(prompt_tokens, window)}  {_ctx_pct(snap)}",
        f"free         {free:,} tokens",
        # Label column widened to match its neighbours — it was four spaces
        # where every other label is seven, so the one line about the cache
        # started in a different place from the five around it.
        _cache_hit_line(f"{'cache':<{_CTX_LABEL_W}}", recent_cached, recent_prompt),
        f"compaction   {comp_est:,} / {comp_trigger:,} tokens est.  ({comp_pct}% to trigger)",
    ]


def help_pane_lines(
    app_bindings: "Iterable[tuple[str, str]]" = (),
    *,
    composer_keys: "Sequence[tuple[str, str]]" = tuple(COMPOSER_KEYS),
    menubar_keys: "Sequence[tuple[str, str]]" = tuple(MENUBAR_KEYS),
    sentqueue_keys: "Sequence[tuple[str, str]]" = tuple(SENTQUEUE_KEYS),
    searchbar_keys: "Sequence[tuple[str, str]]" = tuple(SEARCHBAR_KEYS),
    cursor_keys: "Sequence[tuple[str, str]]" = tuple(CONVERSATION_CURSOR_KEYS),
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
    lines += [f"  {key}  {desc}" for key, desc in searchbar_keys]
    lines += [f"  {key}  {desc}" for key, desc in cursor_keys]
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


def pane_commands(
    tab_id: str,
    snapshot: "dict | None" = None,
    *,
    artifacts: "Sequence[ArtifactRow]" = (),
) -> list[str]:
    """The slash command parallel to each row of ``tab_id``'s pane — index-aligned
    with :func:`pane_payload`'s rows for the same ``snapshot``, ``[]`` for a pane
    with no actionable rows. An empty string marks an inert row (a read-only
    fallback listing that has no toggle to dispatch).

    This is what makes the restored categories OPERABLE rather than merely visible
    (#3338): the app maps a selected row straight onto ``/model`` / ``/attach`` /
    ``/session switch`` / ``/visibility`` / ``/hook`` / ``/open`` (#4482) and
    submits it through the same transport seam a typed slash uses."""
    if tab_id == "artifacts":
        return artifact_pane_commands(artifacts)
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
    artifacts: "Sequence[ArtifactRow]" = (),
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
    if tab_id == "artifacts":
        return artifact_pane_options(artifacts)
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


#: Marks a cost that is a LOWER BOUND rather than the amount spent (#3695).
#: Two ASCII cells, chosen to be cheap and reversible: the status row is the
#: ONE always-visible chrome region and #3326 packs it onto the menu row only
#: while it fits, so a longer marker would trade a permanent chrome row for a
#: caveat. Changing it is a one-constant edit.
UNPRICED_MARK = "+?"


def cost_figure(snap: "dict | None") -> str:
    """The cost segment: the figure, plus a mark when it cannot be complete.

    An unpriced model contributes 0 to the total (``estimate_cost`` returns
    ``None`` — "unknown", which ``record_llm`` must not book as "free"), so a
    session using one shows a figure that never moves. The owner watched
    exactly that for a day and read it as the amount spent. The mark is the
    difference between "this is what it cost" and "this is at least what it
    cost"; the count itself stays in the Cost pane rather than on the one row
    that has no space for it.
    """
    snap = snap or {}
    figure = f"${snap.get('cost_agent', 0.0):.4f}"
    return f"{figure}{UNPRICED_MARK}" if snap.get("cost_agent_unpriced_calls") else figure


#: #4542: the context-usage percent at which the Telemetry segment escalates
#: from the bare number to a labelled one (``16%`` -> ``ctx 82%``) — the
#: owner's proposal names the BEHAVIOR ("通常時は簡潔... 閾値を超えた場合の
#: み情報量を増やしてよい") but not a specific number.
#:
#: #4542 review (owner's standing rule — no unjustified number embedded
#: without either a reasoning comment or a user-facing override, same
#: discipline as ``ImageConfig.row_height_cells``): 80 is a plain,
#: unsurprising round number, not a measured "correct" threshold for
#: every operator's own risk tolerance — see ``tui.context_usage_warn_
#: percent`` in ``reyn.yaml``.
#:
#: **The CANONICAL value is :class:`reyn.config.chat.TuiConfig`'s own
#: default, not this constant** — this constant just READS it, so there
#: is exactly one number to keep in sync, not two hand-written literals
#: (#4542 review: an earlier version defined ``80`` here too, a real
#: drift risk the review caught — "the same contract hand-written in two
#: places"). Importing ``TuiConfig`` (not the reverse — ``config/chat.py``
#: is a foundational, TTY-independent module nearly every reyn code path
#: loads, including headless ones; ``chrome.py`` imports ``textual`` at
#: module level and genuinely cannot be imported without it — verified
#: live, and exactly what ``test_phase3_chrome_imports_stay_tty_only``
#: guards) is the only safe direction. If you're tempted to flip this
#: back because "chrome should own its own UI constant" — don't; that
#: direction breaks ``load_config()`` in any textual-less environment.
#: The name ``CTX_WARN_PERCENT`` is kept (existing references stay
#: valid) even though the value now lives elsewhere.
CTX_WARN_PERCENT = TuiConfig().context_usage_warn_percent


def status_line_text(
    snap: "dict | None",
    agent_name: str,
    *,
    attach_state: "str | None" = None,
    warn_percent: int = CTX_WARN_PERCENT,
) -> str:
    """The Telemetry segment (#4542: ``model · agent    $cost  ctx%``, no
    ``│``/``|`` separators — position and text-style carry the grouping
    instead) — from the live status snapshot (F5b: the running cost +
    context percent are visible here even when the drawer is closed).

    #4542 (owner ruling, superseding #4540's ``│``-separator direction):
    the labelled ``model X │ agent Y │ cost Z │ ctx W`` format is replaced
    by an UNLABELLED one — ``model · agent`` (a ``·`` groups the two
    identity fields, distinct from the ``│`` this redesign explicitly
    rejects) followed by the cost figure and context percent, separated by
    plain whitespace. Labels are dropped deliberately: this line's own
    styling (``@quiet@`` / dim, see app.py's ``StatusLine`` CSS) already
    marks it as the "observation" half of the row, so the meaning is
    conveyed by POSITION, not text. ``ctx`` escalates to a labelled ``ctx
    NN%`` only at/past ``warn_percent`` (default :data:`CTX_WARN_PERCENT`,
    operator-overridable via ``tui.context_usage_warn_percent`` — below it,
    the bare percent is unambiguous next to the cost figure).

    ``attach_state`` (#3671 P3): ``"connecting"`` / ``"failed"`` / ``None``
    (attached — the ordinary case). Owner ruling: "not yet attached" and
    "this is the answer" must never render as the same thing, so a non-``None``
    ``attach_state`` short-circuits BEFORE any ``model``/``cost``/``ctx`` field
    is read — there is no attached session behind those numbers yet, so this
    never risks rendering a placeholder (``$0.0000`` / ``—``) that could be
    misread as a real, confirmed value. ``"connecting"`` and ``"failed"``
    render VISIBLY DIFFERENT text (not merely different in a way only a log
    reader would notice) — a permanently-``"connecting"``-looking client on a
    genuine failure is exactly what the owner ruling forbids.

    #3671 P3 review (lead-coder): deliberately plain text, no decorative
    glyph — ``⏳`` measures East-Asian-Width ``W`` (2 terminal cells) while
    every other character on this ONE always-visible row is ``N``/``Na`` (1
    cell); glyph width also varies by terminal/font, and this line has zero
    such glyphs today, so it would be the first. A 1-cell misjudgement on
    the narrowest-terminal case (owner's own environment) breaks the whole
    row, not just this segment. Visual decoration is explicitly a separate,
    still-open owner decision (#3642) — this PR lands the MECHANISM only; a
    glyph can be added later without touching the mechanism, but a broken
    row on a still-forming feature would cast doubt on the mechanism
    itself.
    """
    if attach_state == "connecting":
        return f"connecting… · agent {agent_name}"
    if attach_state == "failed":
        return f"attach failed (see log) · agent {agent_name}"

    # #2280: when ``snap["halted_reason"]`` is set (the session fail-stopped on
    # a persistent durability failure — ``Session.halted_reason``), a
    # ``HALTED`` banner segment is PREPENDED ahead of the usual values — this
    # line is the ONE always-visible (never-collapsed) chrome region, so it is
    # the surface an idle operator (not currently submitting anything) will
    # proactively see the halt on, rather than only learning it from the next
    # op's raised ``DurabilityHaltError``. Purely observability — the halt
    # itself is already enforced synchronously elsewhere
    # (``_fail_stop_if_durability_dead`` / ``run_one_iteration``); this never
    # gates or delays anything.
    snap = snap or {}
    model = snap.get("model_active_class") or snap.get("model") or "—"
    agent = snap.get("attached_name") or agent_name
    ctx_pct = _ctx_pct(snap)
    ctx_segment = ctx_pct
    if ctx_pct.endswith("%"):
        try:
            if int(ctx_pct[:-1]) >= warn_percent:
                ctx_segment = f"ctx {ctx_pct}"
        except ValueError:
            pass  # "—" (no completed call yet) — bare, never escalated
    base = f"{model} · {agent}    {cost_figure(snap)}  {ctx_segment}"
    halted_reason = snap.get("halted_reason")
    if halted_reason:
        return f"⚠ HALTED — {halted_reason} — agent stopped accepting ops · {base}"
    return base


#: #4357: how many unknown-key NAMES the chrome line shows inline before
#: falling back to "N more" — bounded deliberately (owner ruling, #4380:
#: "show everything" was rejected there for a 197-item case). 3 is enough
#: to make the common "a handful of moved keys" case immediately
#: actionable without the line growing unboundedly with the population.
_CONFIG_WARNING_INLINE_KEY_CAP = 3


def config_warning_text(count: int, keys: "dict | None" = None) -> "str | None":
    """The bottom-chrome config-warning indicator's text, or ``None`` when
    there is nothing to show (#4194).

    Architect's ruling (#4194 issue thread, 2026-08-11) fixes exactly THREE
    properties, form left to the implementer: ①doesn't
    scroll away with the conversation ②stays visible for as long as the
    condition holds ③directs the operator to ``reyn config validate`` for
    the 4-element detail (result / destination / full list / fix command).
    Scope: the POLICY TIER only (``reyn.yaml`` /
    ``reyn.local.yaml`` / ``~/.reyn/config.yaml``) — the same scope
    ``reyn config validate`` itself checks (``config.py``'s ``_validate``
    uses ``build_policy_tier_config``), so the indicator's own guidance is
    always answerable by the command it names. The hot-reload IN-set
    (``.reyn/*.yaml`` — mcp/cron/skills/pipelines/presentations) has its
    OWN separate unknown-key warning path that ``validate`` does NOT cover
    (lead-coder's explicit scoping call, #4194) — deliberately not counted
    here; that remaining silence is real and tracked separately (#4235).

    ``keys`` (#4357, optional — ``ReynConfig.unknown_config_keys``): when
    given, up to :data:`_CONFIG_WARNING_INLINE_KEY_CAP` key names are shown
    inline (RENAMED keys with a known destination first — the ones an
    operator can act on immediately by name — then any remaining unknown/
    removed keys), with a "+N more" suffix past the cap. This line
    previously carried NONE of that detail (architect's original #4194
    design, quoted verbatim in this docstring's history) — #4357 measured
    that in practice this meant nobody acted on the warning: 5 real
    instances of a moved key went unfixed for months, including this
    repo's own ``reyn.yaml``, because "N config keys not applied" names no
    key to fix. Still bounded (never "show everything" — #4380's own
    197-item rejection applies here too) and still ends by pointing at
    ``reyn config validate`` for the full list, so property ③ above is
    unchanged; ``keys=None`` (the pre-#4357 call shape) falls back to the
    original count-only line byte-for-byte.

    ``count`` (not a snapshot dict, unlike :func:`status_line_text`): this
    value is CONFIG-derived, not session/live-state-derived — it is fixed
    for the whole session lifetime once ``load_config()`` runs (``reyn.yaml``
    changes need a restart to take effect, unlike the hot-reload IN-set),
    so there is no live snapshot to route through, only
    ``ReynConfig.unknown_config_key_count`` / ``ReynConfig.
    unknown_config_keys`` themselves.

    ``⚠`` matches the existing ``HALTED`` banner glyph above (same
    single-cell-width class of symbol, same "something needs attention"
    register) rather than introducing a new one."""
    if not count:
        return None
    plural = "" if count == 1 else "s"
    base = f"⚠ {count} config key{plural} not applied"
    if not keys:
        return f"{base} → reyn config validate"

    # #4357: destination-bearing (renamed) keys first — an operator can
    # act on "`model` → `llm.model`" immediately; a bare unknown/removed
    # key still names the key, just not where it goes.
    from reyn.config.config_schema import RenamedKeyHint

    ordered = sorted(
        keys.items(),
        key=lambda item: 0 if isinstance(item[1], RenamedKeyHint) and item[1].destination else 1,
    )
    shown = []
    for key, hint in ordered[:_CONFIG_WARNING_INLINE_KEY_CAP]:
        destination = getattr(hint, "destination", None)
        shown.append(f"`{key}` → `{destination}`" if destination else f"`{key}`")
    remaining = len(ordered) - len(shown)
    names = ", ".join(shown) + (f" (+{remaining} more)" if remaining > 0 else "")
    return f"{base}: {names} → reyn config validate"


def build_drawer_pane(tab_id: str, rows: "Sequence[str]") -> Widget:
    """Build the mounted drawer pane widget for ``tab_id`` from its display
    ``rows``: an :class:`OptionList` for a picker pane, a Rich :class:`Static`
    for a readout. The app rebuilds ``rows`` from a fresh snapshot on each open
    (see :meth:`TextualChatApp._refresh_pane`).

    Two rendering idioms co-exist here, deliberately NOT interchangeable:
    the readout branch wraps in a Rich :class:`~rich.text.Text` LITERAL
    (never markup-parsed regardless of content — Cost/Ctx/Help are always
    internal-only figures, so this is fidelity-safe as-is); the OptionList
    branch wraps the :data:`_LITERAL_ROW_PANES` in :class:`~textual.content.Content`
    (:func:`_literal_option_content`) because ``OptionList`` DOES
    markup-parse a bare ``str`` option — do not "simplify" one branch to
    match the other, they guard against different widgets' different
    default behaviors."""
    if pane_is_list(tab_id):
        options = (
            _literal_option_content(rows)
            if pane_needs_literal_rows(tab_id)
            else rows
        )
        return OptionList(*options, id=tab_id)
    # #3699 keeps this a plain ``Static``: the scrolling for an over-tall
    # readout is done by the drawer around it (see the ``#drawer`` rule in the
    # app stylesheet for why it cannot be done here), so this branch stays the
    # simple "render these rows" it has always been.
    return Static(Text("\n".join(rows)), id=tab_id)


class StatusLine(Static):
    """Slim status-values line — plain, not rich. Mirrors reyn's inline REPL
    bottom toolbar (``model │ agent │ cost │ ctx``). #2280: this is the ONE
    always-visible (never-collapsed) chrome region — the halt banner rides on
    it — so wherever :class:`MenuBar` mounts this widget, it must stay on a
    row that is always on screen, never scrolled or wrapped away.

    #3326: owned and positioned by :class:`MenuBar`, not a standalone
    top-level row — it is appended as a trailing widget to whichever wrapped
    menu row has room for it (usually the last), or given its own extra row
    when none does. Never yielded directly by the app; see
    :meth:`MenuBar._repack`."""


class ConfigWarningLine(Static):
    """#4194: the bottom-chrome config-warning indicator — a persistent,
    non-scrolling row naming how many policy-tier ``reyn.yaml`` keys were
    NOT applied, pointing at ``reyn config validate`` for detail. See
    :func:`config_warning_text` for the scope and content rules.

    A plain top-level sibling of :class:`MenuBar` in the app's compose
    order (unlike :class:`StatusLine`, which MenuBar itself owns and
    positions) — real-terminal geometry measurement (headless
    ``App.run_test``, #4194) confirmed a fixed ``height: 1`` sibling here
    is absorbed cleanly by ``textual_flowview.FlowView``'s (the
    conversation pane) ``1fr`` sizing at both 80×24 and 60×20 — no overlap, no clipping,
    every other chrome row just shifts down by one. The app's own
    ``compose()``/``_refresh_status`` mount and update it conditionally
    (absent entirely when there is nothing to warn about, not merely
    hidden — see the app for why: an EMPTY always-visible row would still
    occupy the layout slot the measurement showed the conversation pane
    giving up)."""

    DEFAULT_CSS = """
    ConfigWarningLine {
        height: 1;
        text-style: bold;
        padding: 0 1;
    }
    """


#: Horizontal cells a :class:`Tab` adds around its label (``Tab``'s own
#: ``padding: 0 1``, restated in the app stylesheet). Used to predict a tab's
#: rendered width when packing rows — kept as one named fact so the packer and
#: the stylesheet cannot silently disagree.
_TAB_H_PADDING = 2

#: Horizontal cells :class:`StatusLine` adds around its text (its own
#: ``padding: 0 1`` in the app stylesheet) — the status-segment analog of
#: ``_TAB_H_PADDING``, used to predict whether it fits alongside tabs on a
#: packed row (#3326).
_STATUS_H_PADDING = 2


def pack_menu_rows(
    items: "Sequence[tuple[str, str]]", width: int
) -> "list[list[tuple[str, str]]]":
    """Greedily pack ``(tab_id, label)`` items into rows no wider than ``width``.

    The menu row is a WRAPPING row, not a scrolling one (#3338). A single
    ``Tabs``-style row lays every tab out on one line regardless of terminal
    width, so at 80 columns the last tab and at 60 columns the last FOUR were
    positioned past the right edge — reachable only by arrowing blindly into
    them, with no scroll affordance to say so. Wrapping keeps every tab inside
    the screen at any width, which is the invariant #3326 should inherit when it
    collapses this chrome rather than re-derive.

    Pure, so the geometry is testable without mounting: the caller (
    :meth:`MenuBar._repack`) passes the content width it actually has. A single
    item wider than ``width`` still gets its own row (there is nowhere narrower
    to put it) — with the labels this menu uses that needs a terminal under ~11
    columns, far below anything the app is usable at."""
    rows: "list[list[tuple[str, str]]]" = []
    current: "list[tuple[str, str]]" = []
    used = 0
    for tab_id, label in items:
        cell = len(label) + _TAB_H_PADDING
        if current and used + cell > width:
            rows.append(current)
            current = []
            used = 0
        current.append((tab_id, label))
        used += cell
    if current:
        rows.append(current)
    return rows


def status_fits_last_row(
    rows: "Sequence[Sequence[tuple[str, str]]]", width: int, status_text_len: int
) -> bool:
    """True if the status-values segment fits alongside the last packed tab row.

    Pure, mirroring :func:`pack_menu_rows`'s testability-without-mounting
    convention (#3326). ``rows`` is the output of :func:`pack_menu_rows`; an
    empty ``rows`` (no tabs at all) trivially fits if the text alone fits
    ``width``. Never truncates or squeezes — the caller falls back to giving
    the status segment its own row when this returns ``False``."""
    status_cell = status_text_len + _STATUS_H_PADDING
    if not rows:
        return status_cell <= width
    used = sum(len(label) + _TAB_H_PADDING for _tab_id, label in rows[-1])
    return used + status_cell <= width


class MenuBar(Widget, can_focus=True):
    """Focusable menu row (the collapsed bottom chrome). ``← →`` move the
    highlight, ``Enter`` opens the highlighted item's drawer, and ``↑``/``Esc``
    close it and hand focus back to the composer. Moving the highlight does NOT
    open anything — opening is an explicit ``Enter``.

    This was a :class:`~textual.widgets.Tabs` subclass until #3338 grew the menu
    from 7 to 13 items. ``Tabs`` lays its children out on ONE line and relies on
    scrolling the active tab into view, so the tabs past the right edge sat
    outside the screen with no affordance saying so (measured: 1 tab off at
    80×24, 4 off at 60×20). This widget instead WRAPS: :func:`pack_menu_rows`
    packs the items into as many single-line rows as the current width needs, and
    a resize repacks. The children are still real :class:`~textual.widgets.Tab`
    widgets, so styling and the ``-active`` highlight are unchanged — only the
    layout differs.

    ``active`` is the highlighted tab id, the same public read the drawer control
    and the Phase-3 keyboard gates use.

    #3326: also owns the :class:`StatusLine` status-values segment — appended
    as a trailing widget on whichever packed row has room for it (see
    :func:`status_fits_last_row`), or given its own extra row when none does.
    This is what collapses the bottom chrome toward one line: the two
    previously-separate always-visible rows (status line above, menu row
    below) become one shared row whenever the terminal is wide enough, and
    fall back to their previous two-row shape (no regression) otherwise."""

    # NOTE: the row's HEIGHT is not declared here. The app stylesheet
    # (``TextualChatApp.CSS``'s ``MenuBar`` rule) overrides a widget's
    # ``DEFAULT_CSS``, so a ``height`` here would be inert — measured: stripping
    # it changes nothing, stripping the app rule breaks the layout. It must stay
    # ``auto`` for the wrapped rows to be visible at all (a fixed height clips
    # them below the last screen line), so the one place to change it is
    # ``app.py``'s rule — not this block, which is where the wrapping logic below
    # would otherwise send you looking.
    DEFAULT_CSS = """
    MenuBar {
        layout: vertical;
    }
    MenuBar > .menubar-row {
        height: 1;
        width: 100%;
        layout: horizontal;
    }
    /* #4542: the stretching gap between Navigation and Telemetry — a plain
       empty widget, no border/content (the owner's own non-goal: "box や
       separator を追加しない"). 1fr consumes whatever width the row's
       natural-width children (tabs + StatusLine) don't need, so Telemetry
       sits at the row's right edge on any width wide enough to merge at
       all — see status_fits_last_row's own docstring for the FIT decision
       this doesn't change (the spacer only expands into slack space that
       already existed; it never causes a row that used to fit to stop
       fitting). */
    MenuBar .menu-spacer {
        width: 1fr;
        height: 1;
    }
    """

    class Selected(Message):
        """Posted on an explicit ``Enter`` (opening ``tab_id``) or on ``↑``/``Esc``
        (the sentinel ``"__close__"``)."""

        def __init__(self, tab_id: str) -> None:
            self.tab_id = tab_id
            super().__init__()

    def __init__(
        self,
        items: "Sequence[tuple[str, str]]",
        *,
        status_text: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._items = list(items)
        self._packed_width = -1
        self._last_seen_width = -1
        self._status_text = status_text
        #: The highlighted tab id. Seeded to the first item so the row has a
        #: highlight from the very first frame (before any resize has landed).
        self.active = self._items[0][0] if self._items else ""

    def _repack(self, width: int) -> None:
        """Rebuild the child rows for ``width``. A no-op when the width has not
        changed, so an ordinary resize storm does not remount 13 widgets a frame."""
        if width <= 0:
            return
        self._last_seen_width = width
        if width == self._packed_width:
            return
        self._packed_width = width
        rows = pack_menu_rows(self._items, width)
        merge_status = status_fits_last_row(rows, width, len(self._status_text))
        self.remove_children()
        # #4542: Navigation (tabs) / Telemetry (StatusLine) as two visually
        # distinct regions when they SHARE a row — a stretching
        # ``.menu-spacer`` between them (see DEFAULT_CSS's own comment)
        # rather than the tabs and StatusLine sitting immediately adjacent,
        # so Telemetry reads as pinned to the right edge instead of merely
        # "the next thing after the last tab". Only the MERGED case needs
        # the spacer: it requires StatusLine at ``width: auto`` (the
        # ``-shared`` class) for the spacer to have slack to expand into.
        # The own-row fallback below does NOT get a spacer — StatusLine
        # there stays at its base rule's ``width: 100%`` (load-bearing
        # containment for a long status string, see that CSS rule's own
        # comment) and is pinned right via ``text-align: right`` instead
        # (app.py's ``StatusLine`` rule) — a spacer would have nothing to
        # push against there, since StatusLine already claims the whole row.
        menu_rows = [
            Horizontal(
                *(Tab(label, id=tab_id) for tab_id, label in row),
                *(
                    [
                        Static("", classes="menu-spacer"),
                        StatusLine(self._status_text, classes="-shared"),
                    ]
                    if merge_status and i == len(rows) - 1
                    else []
                ),
                classes="menubar-row",
            )
            for i, row in enumerate(rows)
        ]
        if not merge_status:
            menu_rows.append(
                Horizontal(StatusLine(self._status_text), classes="menubar-row")
            )
        self.mount_all(menu_rows)
        self.call_after_refresh(self._sync_active_class)

    def update_status(self, text: str) -> None:
        """Update the merged status-values text (#3326).

        A length change can flip the merge decision (:func:`status_fits_last_row`)
        — most sharply for the #2280 halt banner, prepended ahead of the usual
        values and far longer than the steady-state text — so a length change
        forces a fresh repack rather than an in-place ``Static.update``. An
        unchanged length (the common case: cost/ctx ticking within the same
        format) updates the mounted widget directly, no remount."""
        changed_len = len(text) != len(self._status_text)
        self._status_text = text
        if not self.is_mounted:
            return
        if changed_len:
            self._packed_width = -1  # force status_fits_last_row to be re-evaluated
            self._repack(self._last_seen_width)
        else:
            try:
                self.query_one(StatusLine).update(text)
            except NoMatches:
                pass

    def _sync_active_class(self) -> None:
        for tab in self.query(Tab):
            tab.set_class(tab.id == self.active, "-active")

    def on_mount(self) -> None:
        self._repack(self.content_size.width or self.size.width)

    def on_resize(self, event: events.Resize) -> None:
        self._repack(self.content_size.width or event.size.width)

    def _move(self, delta: int) -> None:
        ids = [tab_id for tab_id, _label in self._items]
        if not ids:
            return
        try:
            index = ids.index(self.active)
        except ValueError:
            index = 0
        self.active = ids[(index + delta) % len(ids)]
        self._sync_active_class()

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
        if event.key in ("left", "right"):
            event.stop()
            event.prevent_default()
            self._move(-1 if event.key == "left" else 1)
            return
        await super()._on_key(event)
