"""``RewindPicker`` — the ``/rewind`` checkpoint picker region (#3362).

Bare ``/rewind`` is a two-part command: the slash handler
(:mod:`reyn.interfaces.slash.rewind`) publishes a **command-UI request**
(``session.set_pending_command_ui({"kind": "rewind", "points": [...]})``) that
the front-end is expected to render as an interactive selector, AND a
``__rewind_list__`` display sentinel carrying a plain-text fallback list for
front-ends that host no such region. The plain / ``--cui`` client already
implements both legs (``stream_client.run_output_loop``: swallow the text when
the client hosts the region, render it when it does not); the Textual TUI
skipped the sentinel outright and read the command-UI request nowhere, so bare
``/rewind`` produced no list AND no way to act — this widget is the missing
region.

Same widget contract as
:class:`~reyn.interfaces.inline.textual_chat.intervention_panel.InterventionPanel`:
the widget owns display + selection and **never touches the transport**. Picking
a row posts :class:`RewindPicker.PointSelected`, which the app relays as the
ordinary ``/rewind <seq>`` slash command through its normal submit seam — so the
rewind ACTION is the very same ``AgentRegistry.checkout`` path a typed
``/rewind <seq>`` reaches, not a second implementation beside it. That matters
here more than for most pickers: rewind is destructive, and a picker with its own
private action path would be a second place for the destructive contract to drift.

``Esc`` dismisses without rewinding (:class:`RewindPicker.Dismissed`) — a picker
you cannot back out of would make an accidental ``/rewind`` a trap.

This module is part of the TTY-only ``textual_chat`` package (imported lazily via
:mod:`reyn.interfaces.repl.client_driver`); its ``textual`` imports never reach an
always-loaded module.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import OptionList, Static

from reyn.interfaces import palette

#: Max characters of a point's ``ts`` shown in a row — the WAL timestamp is a
#: free-form string read straight off the WAL entry, so it is truncated rather
#: than allowed to push ``kind`` off a narrow terminal.
_TS_MAX = 40


def rewind_row_text(point: "dict") -> str:
    """The one-line label for one rewind point.

    Carries the three columns the user guide documents for this picker
    (``docs/guide/for-users/time-travel.md``) in that order — ``seq`` (also what
    the user would type as ``/rewind <seq>``), the checkpoint time, and the
    OS-level boundary kind — off the keys ``AgentRegistry.list_rewind_points``
    returns. A row missing ``ts`` simply omits that column rather than
    fabricating one.

    Pure and importable without Textual mounting anything, so the row content is
    testable on its own.
    """
    bits = [f"seq {point.get('seq')}"]
    ts = point.get("ts")
    if ts:
        ts = str(ts)
        if len(ts) > _TS_MAX:
            ts = ts[: _TS_MAX - 1] + "…"
        bits.append(ts)
    bits.append(str(point.get("kind") or "?"))
    return " · ".join(bits)


class RewindPicker(Vertical):
    """The collapsed-by-default ``/rewind`` checkpoint region."""

    DEFAULT_CSS = palette.css("""
    RewindPicker {
        height: auto;
        max-height: 12;
        background: @surface@;
        padding: 0;
    }
    RewindPicker OptionList {
        height: auto;
        max-height: 10;
        background: @surface@;
        border: none;
        padding: 0;
    }
    /* #3523 ②: a heading should look like a heading — it labels the rows
       below it rather than competing with them. `dim` leaves the hue to the
       terminal (the owner's rule: adopt the meaning the terminal already has)
       instead of pinning a colour. */
    RewindPicker #rewind-picker-title {
        text-style: @recede@;
        height: auto;
        color: @quiet@;
        padding: 0 1;
    }
    """)

    BINDINGS = [("escape", "dismiss", "Cancel rewind")]

    class PointSelected(Message):
        """A checkpoint row was picked — ``seq`` is the WAL boundary to check out."""

        def __init__(self, seq: int) -> None:
            super().__init__()
            self.seq = seq

    class Dismissed(Message):
        """Esc: leave the picker without rewinding."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: The seq of each currently-listed row, index-parallel to the
        #: ``OptionList`` options so an ``option_index`` can never address a
        #: different checkpoint than the row the user highlighted (the same
        #: parallel-list discipline the drawer's ``_pane_commands`` uses).
        self._seqs: "list[int]" = []

    def compose(self) -> ComposeResult:
        yield Static(
            "rewind to a checkpoint (enter to check out · esc to cancel)",
            id="rewind-picker-title",
        )
        yield OptionList(id="rewind-picker-options")

    def on_mount(self) -> None:
        self.display = False

    def has_points(self) -> bool:
        """Whether the picker is currently offering any checkpoint."""
        return bool(self._seqs)

    def show_points(self, points: "list[dict]") -> None:
        """Populate + reveal the picker from the command-UI request's points.

        Rows are built as ``Content`` LITERALS (never bare ``str``) for the same
        reason the intervention tabs and the History pane are: an anchor is
        free-form text that can contain square brackets, which Textual would
        otherwise eat as console markup.
        """
        options = self.query_one("#rewind-picker-options", OptionList)
        options.clear_options()
        self._seqs = [int(p["seq"]) for p in points if p.get("seq") is not None]
        rows = [
            Content(rewind_row_text(p)) for p in points if p.get("seq") is not None
        ]
        if not rows:
            self.hide()
            return
        options.add_options(rows)
        options.highlighted = 0
        self.display = True
        options.focus()

    def hide(self) -> None:
        """Collapse the picker and forget its rows.

        Idempotent AND safe before mount: the app calls this from the
        session-switch reset barrier, which runs UNGUARDED by design and must
        not be broken by a widget query that has no child yet.
        """
        self._seqs = []
        try:
            self.query_one("#rewind-picker-options", OptionList).clear_options()
        except Exception:
            pass  # not composed yet — there are no rows to clear
        self.display = False

    def action_dismiss(self) -> None:
        self.hide()
        self.post_message(self.Dismissed())

    def on_option_list_option_selected(
        self, event: "OptionList.OptionSelected"
    ) -> None:
        """Translate the raw row pick into a seq-carrying app message.

        ★Honest scope on ``event.stop()``: it is message hygiene, NOT a fix for
        a live defect — stripping it was measured (#3362's strip matrix) and
        changes no observable behaviour today. The app's own
        ``on_option_list_option_selected`` serves the DRAWER, looking an option
        index up in ``_pane_commands`` by the list's id; an un-stopped bubble
        from here misses that lookup and falls through to a drawer-collapse that
        is already a no-op while the drawer is shut. It stays because a widget
        that TRANSLATES its child's message into its own should consume the
        original — leaving both in flight is what makes a future
        ``_pane_commands`` keyed differently a cross-widget index collision.
        """
        event.stop()
        index = event.option_index
        if not (0 <= index < len(self._seqs)):
            return
        seq = self._seqs[index]
        self.hide()
        self.post_message(self.PointSelected(seq))


__all__ = ["RewindPicker", "rewind_row_text"]
