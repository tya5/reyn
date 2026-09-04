"""``SentQueue`` — the sent-queue region widget (#3300 P2b / Y-client).

Renders the client-side view of the server-authoritative sent queue (the
undispatched inbox items, published by P2a) between the
:class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`
and the input row (region order: conversation / intervention panel /
rewind picker / sent-queue / input — shared with the sibling #3299 arc, see
``app.py``'s :meth:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp.compose`).

The "upward conveyor" lifecycle (the owner-ratified sent-queue exit contract,
#3300 issue §6a) — this widget renders all THREE exits:

- ``user_submitted`` → :meth:`show_item` — a submitted message first appears
  HERE (dim, queued), not immediately as a flow entry. This REPLACES P1 C's
  "render the echo directly as a flow entry": for an idle server the
  promotion below follows almost instantly; for a busy/queued submission the
  item stays visible here until dispatch.
- ``turn_started`` → :meth:`remove_item` — the PROMOTE exit: the app removes
  the item from this widget and appends it as a flow entry (the user line) in
  the SAME step (see ``app.py``'s ``_pump_frames`` — the removal here and the
  flow-entry append are driven from one delta so there is never a frame where
  the item is both queued and already in the flow, or neither).
- ``inbox_cancel`` → :meth:`remove_item` — the REMOVE exit (#3300 Y-client):
  driven by the server-authoritative delta, never by a local "cancel
  succeeded" return value (``app.py``'s ``_handle_inbox_cancel_event``). The
  canceller ADDITIONALLY restores the text into the composer — that half is
  the app's job (it owns the composer), not this widget's.

The app owns the :class:`~reyn.interfaces.transport.agui.state.RemoteQueueView`
seq-gated merge (the P2a order-race protocol, reused as-is — see the app
module docstring); this widget is a pure renderer of whatever the app tells it
to show/remove, keyed by ``msg_id``.

**A fourth, LOCAL entry (#4409)**: before any of the three server-driven
exits above can fire, ``app.py``'s ``on_composer_submitted`` calls
:meth:`show_item` with ``sending=True`` and a LOCALLY-generated id,
synchronously with clearing the composer — closing the gap the owner
reported ("input box から消えると同時に... 消えること多い"): between a
message leaving the input box and the (async, network-round-trip-bound)
``user_submitted`` broadcast materializing it here, there was previously a
window where the message was visible NOWHERE. The row renders with
:data:`_SENDING_GLYPH` (not :data:`_QUEUED_GLYPH`) while in this state —
an honest "not yet confirmed sent" distinct from "confirmed queued,
awaiting dispatch". :meth:`rekey` promotes it in place once
``submit_user_text`` acks (``app.py``'s ``_reconcile_local_send``); if the
real broadcast already materialized the row first (:meth:`has_row`), the
placeholder is simply dropped as redundant.

**Cancel affordance (#3300 Y-client)**: this widget is focusable
(``can_focus = True``) and keeps a highlighted row index, keyed by the SAME
``msg_id`` ordering as :meth:`rendered_texts` (never a guessed/head-of-queue
target — the arc's own #3299/#3287 lesson about correlating on an
authoritative id, applied to selection too). Keymap, chosen to reuse EVERY
convention already established elsewhere in this package rather than invent a
new one:

- ``↑``/``↓`` move the highlighted row — the SAME up/down-cycles-a-list
  idiom the bottom-chrome drawer's ``OptionList`` pickers already use
  (``chrome.py``).
- ``Enter`` cancels the highlighted row — the SAME "Enter acts on the
  highlighted item" idiom those same pickers use (there, Enter *selects*; a
  cancel affordance's "select" action IS "cancel this one").
- ``Escape`` returns focus to the composer — copied VERBATIM from
  :class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`'s
  identical binding for the identical purpose. ``Tab``'s equivalent binding
  was REMOVED (#3365, architect ruling): ``Tab`` is forward-only everywhere
  in the app, ``Esc`` alone owns "back" — gated on
  ``test_textual_chat_esc_sufficiency_3365.py`` machine-verifying ``Esc``
  already reaches the Composer from every focus state this widget (and its
  siblings) can hold.
- The composer's own ``↑`` (on its first line) focuses this widget when it
  is non-empty — the mirror image of the composer's existing ``↓``-on-last-
  line-focuses-the-menubar rule (``chrome.py``'s ``Composer._on_key``), so
  the arrow-steps-to-the-adjacent-zone-at-the-edge rule now works in BOTH
  directions across the whole conversation/panel/queue/input/chrome stack.

Selecting a row only changes which row is highlighted (a CSS class, not the
neutralized text) — :meth:`rendered_texts` is unaffected by focus/selection
state, so the P2b render tests keep passing unchanged.

**Security**: a queued item's text is LLM-adjacent/untrusted user-derived
content reaching the terminal — the SAME injection class as the #3302 panel-
label bug. Every row is neutralized at THIS display boundary
(:func:`~reyn.interfaces.inline.textual_chat.presenter._neutralized_label` —
the same ``core/present/guard.get_neutralizer("terminal")`` seam the
intervention panel and the ``user_submitted`` flow-entry echo both use) and
wrapped in a :class:`~textual.content.Content` LITERAL, never passed to
``Static.update``/mount as a bare ``str`` (which Textual markup-parses). The
SAME neutralized text is what the app later restores into the composer on a
canceller-local restore (``app.py``'s ``_restore_cancelled_text`` re-derives
from the queue view, not from this widget's rendered content, but the source
text is identical either way).
"""
from __future__ import annotations

from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from reyn.interfaces import palette

from .presenter import _neutralized_label

#: The queued-row glyph (#3777, owner call: "再生グリフ" — a play-family
#: mark, unfilled to read as "not yet running", pairing with
#: :data:`~reyn.interfaces.inline.textual_chat.activity_row._STATE_GLYPH`'s
#: filled counterpart on the NOW row once a queued item is promoted —
#: replaces the unrelated hourglass ``⧗``, which shared no shape with
#: anything the promoted row shows. ``wcwidth`` and a full-repo grep both
#: confirmed single-column / collision-free before this landed (see #3777).
_QUEUED_GLYPH = "▷"

#: The SELECTED queued row's glyph (#3777, owner call). The same shape as
#: :data:`_QUEUED_GLYPH`, filled — so selection reads as the row the operator
#: is pointing at, in the shape it already had, rather than as a second mark
#: arriving in a column beside it. It replaces ``palette.SELECTED_MARKER``
#: (``▸``), which the owner reported as "two glyphs on one row": the marker
#: column and the queue glyph were both present and neither explained the
#: other.
#:
#: Carrying selection on the glyph rather than beside it keeps the property
#: the ``▸`` was there for — that selection survives on a terminal where no
#: style renders — because a differently SHAPED row is legible with every
#: attribute stripped. It also frees the two columns the marker held, which
#: is what lets a queue row's text and the NOW row's text start in the same
#: column.
#:
#: The pairing is now three-way and the direction is the point: ``▷``
#: (queued) -> ``▶`` (selected, i.e. the one Enter would cancel) is the same
#: hollow-to-filled step as ``▷`` -> the NOW row's running state, so "filled"
#: consistently means "this is the one being acted on".
_SELECTED_GLYPH = "▶"

#: The SENDING (not-yet-confirmed) row's glyph — #4409. A row keyed by a
#: LOCAL id (``app.py``'s ``on_composer_submitted``, no server ``msg_id``
#: yet) renders with this instead of :data:`_QUEUED_GLYPH`: it is showing
#: BEFORE any server round trip has confirmed the submission even reached
#: the inbox, which ``▷`` — "queued, confirmed, waiting on dispatch" — would
#: overstate. A diamond, not a variant of the triangle family ``▷``/``▶``
#: already own (#3777's own hollow→filled PROMOTION pairing) — this state
#: is not a step in that pair, it precedes it, and reusing a shape from
#: that family would read as a third rung on the SAME ladder rather than
#: the separate, prior fact it is. ``rekey`` (below) is what promotes a row
#: OUT of this state once the server acks it, in place, never by
#: remove+re-add (see that method's own docstring for why position and
#: on-screen continuity both depend on that).
_SENDING_GLYPH = "◇"

#: What separates a row's glyph from its label. Two spaces, not one: the NOW
#: row above the queue carries no glyph at all (#3777), so its text has to
#: start where a queue row's LABEL starts for the two regions to read as one
#: column of text. One glyph plus this gap is that offset, and naming it here
#: keeps the two files from drifting by a space.
_GLYPH_GAP = "  "

#: Which column a row's TEXT starts in, glyph included. Exported because the
#: NOW row above the queue has to start its text in the same column and has no
#: glyph of its own to derive it from — importing it is what keeps the two
#: regions aligned, where a comment saying "keep these equal" would only
#: record the intent and let a one-space edit break it silently.
#:
#: Owned here rather than in ``activity_row`` because the offset is a
#: consequence of THIS region's glyph: the queue is the region that has one,
#: and the NOW row aligns to the queue, not the other way round.
ROW_TEXT_COLUMN = 1 + len(_GLYPH_GAP)


class SentQueue(Vertical):
    """The sent-queue region: one dim row per undispatched queued message,
    keyed by ``msg_id``. Collapsed (``display=False``) when empty — the
    default state until the first :meth:`show_item` call."""

    can_focus = True

    DEFAULT_CSS = palette.css("""
    SentQueue {
        height: auto;
        max-height: 6;
        /* #3688: the cap keeps a long queue from eating the conversation, but
           WITHOUT this the overflow was CLIPPED — a 7th row was mounted, in the
           model, ``display=True``, and simply not on screen. Measured before
           the fix: 9 queued items rendered 6, and the six were the OLDEST, so
           the row that vanished was always the one the operator had just
           submitted ("I sent it and it never appeared", owner report). Silent
           truncation of a region whose whole job is "what you sent is still
           waiting" is the one thing it must not do. Scrolling keeps every item
           reachable — including by the up/down/Enter cancel bindings below,
           which a "show only the newest 6" rule would have cut off from the
           older items it is most likely to be aimed at. */
        overflow-y: auto;
        padding: 0 1;
    }
    /* An unselected row recedes by ATTRIBUTE, not by colour. ``color: @quiet@``
       was the previous rule and it receded by nothing: under the ansi themes
       ``$text-muted`` resolves to the same ``ansi_default`` marker as ordinary
       text, so the queue was drawn in exactly the body's colour while claiming
       to be quiet (the #3523 family, measured). ``dim`` is an SGR attribute —
       it leaves the hue to the terminal, which is the operator's to choose,
       and it actually changes what is drawn. */
    SentQueue Static { height: auto; text-style: @recede@; }
    /* Selection is an ATTRIBUTE plus the row's own GLYPH, never a filled
       background. ``background: $accent 30%`` was the previous rule and under
       the ansi themes the alpha is DROPPED, so it painted a solid ANSI green
       bar with default-coloured text on top — reported as unreadable. #3490
       settled the same question on the conversation: surviving the style merge
       is necessary and not sufficient, the mark has to be CONTENT.

       #3777 moved which content carries it. The mark used to be a separate
       ``▸`` in a column of its own; it is now the row's OWN glyph filling in
       (``▷`` -> ``▶``), so selection costs no column and reads as the same
       object changing state rather than as a pointer arriving beside it. The
       "survives with no styling at all" property the ``▸`` existed for is
       kept, and by the same means: a reader who sees neither ``dim`` nor
       ``bold`` still sees one row shaped differently from the rest. */
    SentQueue Static.-selected { text-style: @selected-style@; }
    """)

    BINDINGS = [
        Binding("up", "select_prev", "Previous queued", show=False),
        Binding("down", "select_next", "Next queued", show=False),
        Binding("enter", "cancel_selected", "Cancel selected", show=False),
        # #3365: Tab's own "back to composer" binding was removed — Esc alone
        # owns "back" everywhere now (see the module docstring and
        # test_textual_chat_esc_sufficiency_3365.py).
        Binding("escape", "focus_composer", "Back to composer", show=False),
    ]

    class Cancelled(Message):
        """Posted when the user cancels the highlighted queued row (the
        Enter binding). ``msg_id`` is the authoritative correlation key the
        app hands to ``ClientTransport.cancel_queued`` — never a guessed/
        positional target."""

        def __init__(self, msg_id: str) -> None:
            self.msg_id = msg_id
            super().__init__()

    def on_mount(self) -> None:
        self.display = False
        self._rows: "dict[str, Static]" = {}
        self._labels: "dict[str, str]" = {}
        #: #4409: keys (of ``_rows``) currently in the SENDING (not-yet-
        #: confirmed) sub-state — a row's own key set membership, not a
        #: second row list, so :meth:`rekey`/:meth:`remove_item` only ever
        #: have ONE place tracking a row's identity to keep in sync.
        self._sending: "set[str]" = set()
        self._selected_index = 0
        #: #3680: when the terminal is too short to give the queue a row per
        #: item, it renders as one ``Queued: N`` line instead. Every item is
        #: still HERE — the rows are what is given up, never an entry, because
        #: this region's contents are durable state somebody is waiting on.
        self._summarised = False
        # The one-line stand-in, mounted once and hidden until it is needed.
        self._summary = Static("", id="sent-queue-summary")
        self.mount(self._summary)
        self._summary.display = False

    def show_item(self, msg_id: str, text: str, *, sending: bool = False) -> None:
        """Materialize a queued item (``user_submitted``): neutralize the
        untrusted text at this display boundary, wrap it in a ``Content``
        literal (never a bare ``str`` — see the module docstring's security
        note), and mount it as a new dim row. A duplicate ``msg_id`` (should
        not happen server-side, but guarded) replaces the existing row rather
        than stacking a second one.

        ``sending`` (#4409): the row is a LOCAL, not-yet-server-confirmed
        placeholder — ``app.py``'s ``on_composer_submitted`` calls this,
        keyed by a local id, synchronously with clearing the composer, so
        the gap between "left the input box" and "visible somewhere" is
        zero rather than however long the server round trip takes. Renders
        with :data:`_SENDING_GLYPH` instead of :data:`_QUEUED_GLYPH` — see
        that constant's own docstring. :meth:`rekey` is how a caller
        promotes it out of this state once the server acks."""
        if msg_id in self._rows:
            self.remove_item(msg_id)
        label = _neutralized_label(text)
        self._labels[msg_id] = label
        if sending:
            self._sending.add(msg_id)
        glyph = _SENDING_GLYPH if sending else _QUEUED_GLYPH
        row = Static(Content(f"{glyph}{_GLYPH_GAP}{label}"))
        self._rows[msg_id] = row
        self.mount(row)
        self.display = True
        self._clamp_selection()
        self._apply_highlight()
        # #3688: a new row appends at the BOTTOM, i.e. past the cap once the
        # queue is deeper than the visible window — so without this the item
        # the operator just submitted is precisely the one they cannot see.
        # Only when the region is not being navigated: while it holds focus the
        # selected row owns the viewport (``_apply_highlight`` scrolls to it),
        # and yanking the view to the bottom mid-navigation would move the
        # cancel target out from under them.
        if not self.has_focus:
            self._bring_into_view(row)

    def remove_item(self, msg_id: str) -> None:
        """Remove a queued item's row (the PROMOTE exit or the ``inbox_cancel``
        REMOVE exit, #3300 Y-client). No-op for an unknown ``msg_id``.
        Collapses the region back to hidden once the last item is gone."""
        self._labels.pop(msg_id, None)
        self._sending.discard(msg_id)
        row = self._rows.pop(msg_id, None)
        if row is not None:
            row.remove()
        if not self._rows:
            self.display = False
        self._clamp_selection()
        self._apply_highlight()

    def clear_all(self) -> None:
        """Remove every queued row unconditionally (#3310 N2 session-switch
        reset barrier) — the widget-level exit that has no server delta of
        its own to ride: a switch discards ALL of the OLD session's queued
        rows client-side (the new session's own queue, if any, is reseeded
        from its snapshot immediately after — the SAME "seed on first frame"
        path a fresh :class:`~reyn.interfaces.transport.agui.state.RemoteQueueView`
        drives, #3305-shaped). Reuses :meth:`remove_item` per row so the
        collapse-when-empty / selection-clamp invariants stay in ONE place
        rather than being re-derived here."""
        for msg_id in list(self._rows):
            self.remove_item(msg_id)

    def rendered_texts(self) -> "list[str]":
        """The currently-queued rows' rendered text, oldest first — the
        public read a caller (a test, or a future consumer) uses to inspect
        displayed content without reaching into private widget state.

        #3680: while the region is summarised it returns the summary — what is
        ON SCREEN, not what would be there at full height. A reader of this
        surface asking "what does the queue show" must not be told about rows
        the operator cannot see; the item COUNT is still ``len(queue())`` on
        the model, which is where "what is queued" belongs."""
        if self._summarised:
            return [str(self._summary.content)] if self._rows else []
        return [str(row.content) for row in self._rows.values()]

    def item_count(self) -> int:
        """How many messages are queued — independent of how they are drawn.

        Distinct from ``len(rendered_texts())`` on purpose. ``rendered_texts``
        answers "what is on screen", which while summarised is ONE line
        whatever the queue holds; this answers "how many are waiting", which
        the collapse cannot change.

        The distinction is not academic. ``_apply_compact_layout`` used
        ``rendered_texts`` to decide whether the queue must collapse, so the
        decision's INPUT moved with its own OUTPUT: collapse made the count
        read 1, which said there was room, which un-collapsed it, which made
        the count read 3 again. Measured flipping on every re-decide (#3680
        follow-up). A count that survives the thing being decided is what
        breaks the loop.
        """
        return len(self._rows)

    def has_items(self) -> bool:
        """Whether at least one queued row is currently shown.

        Named to avoid ``DOMNode.is_empty`` (a base Textual PROPERTY, "are
        there no displayed children?", read by the ``:empty`` CSS
        pseudo-class hook — ``textual/widget.py``'s
        ``"empty": lambda widget: widget.is_empty``). A same-named METHOD
        override would make that lookup evaluate to a bound method — always
        truthy — turning ``:empty`` permanently ON for this widget, a live
        foot-gun independent of whether current CSS happens to use
        ``:empty`` yet (co-vet finding on #3314)."""
        return bool(self._rows)

    def selected_msg_id(self) -> "str | None":
        """The currently-highlighted row's ``msg_id`` — the public read a
        test uses to assert selection without reaching into
        ``self._selected_index``/``self._rows`` directly."""
        order = list(self._rows.keys())
        if not order:
            return None
        return order[max(0, min(self._selected_index, len(order) - 1))]

    def has_row(self, msg_id: str) -> bool:
        """Whether a row keyed by ``msg_id`` is currently shown — #4409's
        reconciliation read: the app asks this to tell "the server's
        ``user_submitted`` broadcast already materialized this id" apart
        from "still waiting", before deciding whether :meth:`rekey`ing a
        local placeholder onto it would create a duplicate."""
        return msg_id in self._rows

    def rekey(self, old_id: str, new_id: str) -> None:
        """Rename a row's key in place — #4409: promotes a local SENDING
        placeholder (``old_id``) to the server-confirmed ``msg_id``
        (``new_id``) once ``submit_user_text`` acks it, without moving its
        queue position or re-mounting it. A plain ``remove_item`` +
        ``show_item`` pair would do both — the row would jump to the END
        (dict re-insertion order) and flash off/on for one frame — neither
        of which is true here: the SAME message is still exactly as queued
        as it was a moment ago, only its id changed from a local guess to
        the authoritative one. No-op if ``old_id`` is not currently a row
        (already reconciled, or removed by a cancel/dispatch that raced
        this call — the caller's own docstring covers that race)."""
        if old_id not in self._rows:
            return
        self._rows = {new_id if k == old_id else k: v for k, v in self._rows.items()}
        self._labels = {new_id if k == old_id else k: v for k, v in self._labels.items()}
        self._sending.discard(old_id)  # now confirmed — drops out of SENDING
        self._apply_highlight()

    def _bring_into_view(self, row: Static) -> None:
        """Scroll ``row`` into the visible window, best-effort.

        Deferred to after the next refresh because a row scrolled to on the
        same beat it was mounted has no laid-out region yet, so the scroll
        would target nothing. Guarded because this is a display convenience on
        a region whose job is to keep showing what is queued — a scroll fault
        must never take the queue's own rendering down with it."""
        def _scroll() -> None:
            try:
                self.scroll_to_widget(row, animate=False)
            except Exception:  # noqa: BLE001 — display convenience, never load-bearing
                pass

        self.call_after_refresh(_scroll)

    def _clamp_selection(self) -> None:
        last = max(len(self._rows) - 1, 0)
        self._selected_index = max(0, min(self._selected_index, last))

    def set_summarised(self, summarised: bool) -> None:
        """Render as one ``Queued: N`` line (``True``) or a row per item.

        The queue keeps every item either way, so this is reversible the
        moment the room comes back."""
        if self._summarised == summarised:
            return
        self._summarised = summarised
        for row in self._rows.values():
            row.display = not summarised
        self._apply_highlight()

    @property
    def summarised(self) -> bool:
        """Whether the queue is currently showing a count instead of rows."""
        return self._summarised

    def _apply_highlight(self) -> None:
        """Mark the selected row, in its TEXT as well as its style.

        The marker is what makes selection legible where a background cannot
        go (see the CSS above); the two-space indent on unselected rows keeps
        the queue text aligned so the marker reads as a pointer rather than as
        the rows shifting."""
        order = list(self._rows.keys())
        if self._summarised:
            # One line, and it names the count rather than implying the rows
            # were dropped. Selection is untouched underneath: restoring the
            # rows restores exactly what was selected.
            # Indented to where a row's LABEL sits, not to column 0: collapsing
            # the queue should look like the rows closing up, and a summary
            # that started a column further left would read as a different
            # kind of line arriving rather than as the same region compacting.
            self._summary.update(
                Content(f"{' ' * ROW_TEXT_COLUMN}Queued: {len(order)}")
            )
            self._summary.display = bool(order)
            return
        self._summary.display = False
        for i, msg_id in enumerate(order):
            selected = i == self._selected_index
            row = self._rows[msg_id]
            row.set_class(selected, "-selected")
            # #4409: SENDING (not yet server-confirmed) wins over selection —
            # a selected-but-unconfirmed row still needs to read as
            # unconfirmed; ``_SELECTED_GLYPH`` only applies once a row has
            # graduated to :data:`_QUEUED_GLYPH`'s state.
            if selected and msg_id not in self._sending:
                glyph = _SELECTED_GLYPH
            elif msg_id in self._sending:
                glyph = _SENDING_GLYPH
            else:
                glyph = _QUEUED_GLYPH
            # #3777 (owner call, "先頭行だけを区別する話は終わり" — option ①):
            # the NEXT label singling out the head row is gone, with no
            # replacement mark at that position. Every queued row now renders
            # identically regardless of position — the head is still first in
            # ``order`` (:meth:`selected_msg_id`/the cancel bindings are
            # unaffected), it just is not called out visually anymore.
            row.update(Content(f"{glyph}{_GLYPH_GAP}{self._labels[msg_id]}"))
            # #3688: with the region scrollable (see the CSS cap above), the
            # selected row can sit outside the visible six — arrowing onto a row
            # that stays off screen is the same silent-clip defect wearing a
            # different hat, since Enter then cancels something the operator
            # cannot see.
            if selected:
                self._bring_into_view(row)

    def action_select_prev(self) -> None:
        if self._selected_index > 0:
            self._selected_index -= 1
        self._apply_highlight()

    def action_select_next(self) -> None:
        if self._selected_index < len(self._rows) - 1:
            self._selected_index += 1
        self._apply_highlight()

    def action_cancel_selected(self) -> None:
        msg_id = self.selected_msg_id()
        if msg_id is not None:
            self.post_message(self.Cancelled(msg_id))

    def action_focus_composer(self) -> None:
        # String type-selector (not a direct import of ``chrome.Composer``)
        # so this module stays a leaf the chrome module can safely import
        # (``chrome.py``'s ``Composer`` steps focus INTO this widget on its
        # own ``↑``-at-first-line rule) without a two-way import cycle.
        composer = self.app.query_one("Composer")
        composer.focus()


__all__ = ["SentQueue"]
