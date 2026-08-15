"""``SearchBar`` — the ctrl+n in-conversation search bar (#3476 ⑤, moved off
ctrl+f by #3692 PR-B ③).

A one-line bar docked directly above the input row (region order:
conversation / intervention panel / rewind picker / sent-queue / completion
popup / **search bar** / input — the last chrome region before the composer).
Collapsed by default (``display=False`` in :meth:`on_mount`), shown by the
app's ``ctrl+n`` binding.

This widget is PURE CHROME, the same split every sibling region uses (see
:class:`~reyn.interfaces.inline.textual_chat.sent_queue.SentQueue`): it owns
the query :class:`~textual.widgets.Input`, the ``n/M`` match-count label, and
the key surface, and POSTS messages — the app owns the actual search state
(match computation over the flow model, selection, scrolling), because only
the app holds the :class:`~textual_flowview.FlowView` and the lazily-held
older-history prefix (#3476 ④) a correct search must see.

Key surface (owner-decided: Enter = next / Shift+Enter = previous):

- ``Enter`` → :class:`Navigate` toward OLDER matches — the search scans a
  bottom-anchored conversation, so "next result" walks backward in time,
  the same direction iTerm2/less searches walk (``↓`` steps back toward
  newer as the mirror). ``Enter`` arrives as the Input's own ``Submitted``
  message; the rest arrive through :meth:`on_key` (an :class:`Input` binds
  none of them, so they bubble here from the focused Input).
- ``Shift+Enter`` → :class:`Navigate` toward NEWER matches. Requires an
  extended-keys terminal (the same caveat as the composer's own
  Shift+Enter-for-newline), which is why ``↑``/``↓`` mirror the two
  directions as a fallback — a one-line Input has no other use for them.
  The arrows map SPATIALLY (``↑`` = older, the direction the viewport
  moves; ``↓`` = newer), not by the next/prev labels — so the arrow you
  press and the way the conversation scrolls always agree.
- ``Escape`` → :class:`Dismissed`; the app hides the bar, clears the
  selection, and returns focus to the composer (the package-wide "``Esc``
  alone owns back-to-composer" rule, #3365).
"""
from __future__ import annotations

from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Input, Static

from reyn.interfaces import palette


class SearchBar(Horizontal):
    """The ctrl+n search bar: query input + ``n/M`` match count."""

    DEFAULT_CSS = palette.css("""
    SearchBar {
        display: none;
        height: 1;
        background: @surface@;
        padding: 0 1;
    }
    SearchBar Input {
        width: 1fr;
        background: @surface@;
    }
    SearchBar #search-count {
        width: auto;
        height: 1;
        /* #3523 ①: the count is the interface answering, not what the operator
           typed — `alpha 1/1` should not read as two equal halves. `dim` rather
           than a muted colour because under the ansi themes `$text-muted` is
           the same marker as ordinary text and receded by nothing. */
        text-style: @recede@;
        padding: 0 0 0 1;
    }
    """)

    class QueryChanged(Message):
        """The query text changed — the app recomputes matches incrementally."""

        def __init__(self, query: str) -> None:
            self.query = query
            super().__init__()

    class Navigate(Message):
        """Step to the adjacent match. ``older=True`` walks backward in time
        (model order ``find_previous``), ``False`` forward toward newer."""

        def __init__(self, *, older: bool) -> None:
            self.older = older
            super().__init__()

    class Dismissed(Message):
        """The user closed the bar (Escape)."""

    def compose(self):
        # ``compact=True`` is Textual's own one-line Input mode (border
        # stripped with !important, height 1) — hand-CSS overrides lose to
        # the ``Input:focus`` border rule and paint half-block border rows
        # (measured), so use the first-class knob instead.
        yield Input(placeholder="Search conversation…", id="search-input", compact=True)
        yield Static("", id="search-count")

    def on_mount(self) -> None:
        self.display = False

    # ── the app-facing surface ──────────────────────────────────────────────

    def open(self) -> None:
        """Show the bar and focus the query input. The previous query is KEPT
        (reopening resumes the last search, the browser ctrl+f convention);
        the app re-syncs the count/selection on open."""
        self.display = True
        self.query_one("#search-input", Input).focus()

    def hide(self) -> None:
        self.display = False

    @property
    def query(self) -> str:
        return self.query_one("#search-input", Input).value

    def set_count(self, current: int, total: int) -> None:
        """``current`` is the 1-based model-order position of the selected
        match (0 = none selected); ``total`` the match count."""
        label = f"{current}/{total}" if total else ("0/0" if self.query else "")
        self.query_one("#search-count", Static).update(label)

    # ── key surface ─────────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self.post_message(self.QueryChanged(event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter — the Input consumes the key itself and posts Submitted.
        event.stop()
        self.post_message(self.Navigate(older=True))

    def on_key(self, event) -> None:
        # Everything Enter is not: an Input binds none of these, so they
        # bubble here from the focused query box.
        if event.key in ("shift+enter", "down"):
            event.stop()
            self.post_message(self.Navigate(older=False))
        elif event.key == "up":
            event.stop()
            self.post_message(self.Navigate(older=True))
        elif event.key == "escape":
            event.stop()
            self.post_message(self.Dismissed())
