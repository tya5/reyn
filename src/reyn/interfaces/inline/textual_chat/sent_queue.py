"""``SentQueue`` — the sent-queue region widget (#3300 P2b / Y-client).

Renders the client-side view of the server-authoritative sent queue (the
undispatched inbox items, published by P2a) between the
:class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`
and the input row (region order: conversation / intervention panel /
sent-queue / input — shared with the sibling #3299 arc, see
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
- ``Escape``/``Tab`` return focus to the composer — copied VERBATIM from
  :class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`'s
  identical two bindings for the identical purpose.
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

from .presenter import _neutralized_label


class SentQueue(Vertical):
    """The sent-queue region: one dim row per undispatched queued message,
    keyed by ``msg_id``. Collapsed (``display=False``) when empty — the
    default state until the first :meth:`show_item` call."""

    can_focus = True

    DEFAULT_CSS = """
    SentQueue {
        height: auto;
        max-height: 6;
        color: $text-muted;
        padding: 0 1;
    }
    SentQueue Static { height: auto; }
    SentQueue Static.-selected {
        background: $accent 30%;
        color: $text;
    }
    """

    BINDINGS = [
        Binding("up", "select_prev", "Previous queued", show=False),
        Binding("down", "select_next", "Next queued", show=False),
        Binding("enter", "cancel_selected", "Cancel selected", show=False),
        Binding("escape", "focus_composer", "Back to composer", show=False),
        Binding("tab", "focus_composer", "Back to composer", show=False),
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
        self._selected_index = 0

    def show_item(self, msg_id: str, text: str) -> None:
        """Materialize a queued item (``user_submitted``): neutralize the
        untrusted text at this display boundary, wrap it in a ``Content``
        literal (never a bare ``str`` — see the module docstring's security
        note), and mount it as a new dim row. A duplicate ``msg_id`` (should
        not happen server-side, but guarded) replaces the existing row rather
        than stacking a second one."""
        if msg_id in self._rows:
            self.remove_item(msg_id)
        label = _neutralized_label(text)
        row = Static(Content(f"⧗ {label}"))
        self._rows[msg_id] = row
        self.mount(row)
        self.display = True
        self._clamp_selection()
        self._apply_highlight()

    def remove_item(self, msg_id: str) -> None:
        """Remove a queued item's row (the PROMOTE exit or the ``inbox_cancel``
        REMOVE exit, #3300 Y-client). No-op for an unknown ``msg_id``.
        Collapses the region back to hidden once the last item is gone."""
        row = self._rows.pop(msg_id, None)
        if row is not None:
            row.remove()
        if not self._rows:
            self.display = False
        self._clamp_selection()
        self._apply_highlight()

    def rendered_texts(self) -> "list[str]":
        """The currently-queued rows' rendered text, oldest first — the
        public read a caller (a test, or a future consumer) uses to inspect
        displayed content without reaching into private widget state."""
        return [str(row.content) for row in self._rows.values()]

    def is_empty(self) -> bool:
        return not self._rows

    def selected_msg_id(self) -> "str | None":
        """The currently-highlighted row's ``msg_id`` — the public read a
        test uses to assert selection without reaching into
        ``self._selected_index``/``self._rows`` directly."""
        order = list(self._rows.keys())
        if not order:
            return None
        return order[max(0, min(self._selected_index, len(order) - 1))]

    def _clamp_selection(self) -> None:
        last = max(len(self._rows) - 1, 0)
        self._selected_index = max(0, min(self._selected_index, last))

    def _apply_highlight(self) -> None:
        order = list(self._rows.keys())
        for i, msg_id in enumerate(order):
            self._rows[msg_id].set_class(i == self._selected_index, "-selected")

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
