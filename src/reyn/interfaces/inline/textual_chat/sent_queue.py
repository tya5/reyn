"""``SentQueue`` — the sent-queue region widget (#3300 P2b).

Renders the client-side view of the server-authoritative sent queue (the
undispatched inbox items, published by P2a) between the
:class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`
and the input row (region order: conversation / intervention panel /
sent-queue / input — shared with the sibling #3299 arc, see
``app.py``'s :meth:`~reyn.interfaces.inline.textual_chat.app.TextualChatApp.compose`).

The "upward conveyor" lifecycle (the owner-ratified sent-queue exit contract,
#3300 issue §6a) this widget renders the MATERIALIZE half of:

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
- ``inbox_cancel`` removal is Phase 3 Y — NOT built here.

The app owns the :class:`~reyn.interfaces.transport.agui.state.RemoteQueueView`
seq-gated merge (the P2a order-race protocol, reused as-is — see the app
module docstring); this widget is a pure renderer of whatever the app tells it
to show/remove, keyed by ``msg_id``.

**Security**: a queued item's text is LLM-adjacent/untrusted user-derived
content reaching the terminal — the SAME injection class as the #3302 panel-
label bug. Every row is neutralized at THIS display boundary
(:func:`~reyn.interfaces.inline.textual_chat.presenter._neutralized_label` —
the same ``core/present/guard.get_neutralizer("terminal")`` seam the
intervention panel and the ``user_submitted`` flow-entry echo both use) and
wrapped in a :class:`~textual.content.Content` LITERAL, never passed to
``Static.update``/mount as a bare ``str`` (which Textual markup-parses).
"""
from __future__ import annotations

from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Static

from .presenter import _neutralized_label


class SentQueue(Vertical):
    """The sent-queue region: one dim row per undispatched queued message,
    keyed by ``msg_id``. Collapsed (``display=False``) when empty — the
    default state until the first :meth:`show_item` call."""

    DEFAULT_CSS = """
    SentQueue {
        height: auto;
        max-height: 6;
        color: $text-muted;
        padding: 0 1;
    }
    SentQueue Static { height: auto; }
    """

    def on_mount(self) -> None:
        self.display = False
        self._rows: "dict[str, Static]" = {}

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

    def remove_item(self, msg_id: str) -> None:
        """Remove a queued item's row (the PROMOTE or, later, cancel exit).
        No-op for an unknown ``msg_id``. Collapses the region back to hidden
        once the last item is gone."""
        row = self._rows.pop(msg_id, None)
        if row is not None:
            row.remove()
        if not self._rows:
            self.display = False

    def rendered_texts(self) -> "list[str]":
        """The currently-queued rows' rendered text, oldest first — the
        public read a caller (a test, or a future consumer) uses to inspect
        displayed content without reaching into private widget state."""
        return [str(row.content) for row in self._rows.values()]

    def is_empty(self) -> bool:
        return not self._rows


__all__ = ["SentQueue"]
