"""/find — search the conv pane buffer for a substring or regex, with history recall.

Categorical UX gap fill (= "I said something about X earlier, where
is it?"). Searches the current RichLog buffer and jumps to the
first match near the current scroll position. Default is
case-insensitive substring; flags opt into regex / case-sensitive
modes. The "search what's visible in scrollback" scope is the
natural TUI-internal boundary — agent-reply history past the
ring-buffer trim lives in ``.reyn/events/agents/<name>/`` and is
reachable via the right-panel Events tab + ``/list`` slash; this
command surfaces just the in-pane content.

Pattern: same shape as ``/copy`` — the slash command emits a
sentinel ``__find__`` OutboxMessage with the raw arg in ``text``;
the TUI app intercepts via ``_on_find`` in app_outbox, parses
leading flags off the front, runs ``find_in_buffer`` with the
chosen mode, scrolls to the nearest match, and writes a status.

History recall: every non-empty ``/find <q>`` invocation appends
``q`` to a module-level LRU deque (capped at
``_FIND_HISTORY_MAX``). The picker hint mode reads the deque
via ``_find_completer`` when the user types ``/find ``
(trailing space, no further args) — the dim completion rows
show the most-recent queries first. Tab inserts the selected
entry; Enter inserts + submits. In-memory only — history is
naturally gone on session restart.

Usage::

    /find tokens         # case-insensitive substring (default)
    /find -c Foo         # case-sensitive substring
    /find -r f.*o        # case-insensitive regex
    /find -rc Foo.*      # case-sensitive regex (combined flag)
    /find -cr Foo.*      # same as -rc (flag order doesn't matter)
    /find -hyphen-term   # leading hyphen + unrecognised letters →
                          treated as the literal query (= the user
                          is searching for ``-hyphen-term``)
    /find <space>        # picker shows last 5 queries to recall via Tab
    /find                # empty query → usage hint
"""
from __future__ import annotations

from collections import deque

from reyn.interfaces.slash import slash
from reyn.runtime.outbox import OutboxMessage

# Cap on the recall list. 5 matches the user mental model of "few
# recent searches I might want to re-run" without overwhelming the
# picker's 8-row max — leaves room for the hint + usage line.
_FIND_HISTORY_MAX = 5

# Module-level LRU deque. Newest at the front (= ``appendleft``).
# Exported for tests via ``_find_history_snapshot`` so the cache
# state stays inspectable without mutating the deque directly.
_find_history: deque[str] = deque(maxlen=_FIND_HISTORY_MAX)


def _record_find_history(arg: str) -> None:
    """LRU insert — duplicate moves to front; new entry goes to front.

    Stores the raw arg INCLUDING any leading flags (= ``-r foo.*``
    keeps the regex flag so Tab-recall re-runs the same search mode).
    Empty input is a no-op so the empty-query usage-hint branch
    doesn't pollute history.
    """
    if not arg:
        return
    if arg in _find_history:
        _find_history.remove(arg)
    _find_history.appendleft(arg)


def _find_history_snapshot() -> list[str]:
    """Read-only copy of the current history deque (front = newest).

    Tests use this to assert on history state without poking the
    private deque directly. Not exported in __all__; module-private.
    """
    return list(_find_history)


def find_history_has_entries() -> bool:
    """Return True when the find history deque is non-empty.

    Public surface used by :func:`_find_tab_footer` to gate the
    Tab-recall footer row (= "↳ Tab inserts a recent query") on
    whether there's actually anything to recall. Callers must NOT
    reach into ``_find_history`` directly — that would bypass the
    module-level encapsulation. This wrapper is the minimal clean
    public surface.
    """
    return bool(_find_history)


def _find_tab_footer() -> str | None:
    """Picker-hint footer message for /find — the Tab-recall affordance.

    Returns the dim sub-row message ("Tab inserts a recent query")
    surfaced once the user types ``/find ``, but ONLY when there is
    history to recall — otherwise ``None`` so the picker renders no
    footer (avoids implying Tab does something when there's nothing).

    This is the command-owned half of the SlashPicker footer contract
    (``SlashCommand.tab_footer_fn``): /find owns the message text + the
    "show only with history" condition; the picker owns the ``↳`` chrome
    and never hardcodes the command name.
    """
    if find_history_has_entries():
        return "Tab inserts a recent query"
    return None


def _find_completer(session: "object", arg_partial: str = "") -> list[str]:
    """Surface recent /find queries as picker hint completions.

    Empty ``arg_partial`` (= bare ``/find ``) returns the full
    history (newest first). Non-empty ``arg_partial`` filters by
    prefix so the user can type a few chars + Tab to narrow down.

    ``session`` is ignored — history is module-level, not session-
    bound. The completer signature accepts it because the SlashPicker
    completer contract requires ``(session, arg_partial)`` shape.
    """
    if not arg_partial:
        return list(_find_history)
    return [h for h in _find_history if h.startswith(arg_partial)]


@slash(
    "find",
    summary="Search the conv pane (substring or regex)",
    usage="/find [-r|-c|-rc] <query>",
    completer=_find_completer,
    tab_footer_fn=_find_tab_footer,
)
async def find_cmd(session: "object", args: str) -> None:
    # Forward the raw arg; the TUI handler validates and surfaces
    # errors so we don't duplicate the parsing logic across the
    # slash + outbox layers. Matches the ``/copy`` pattern.
    arg = (args or "").strip()
    await session._put_outbox(OutboxMessage(
        kind="__find__", text=arg,
    ))
    # Record AFTER the outbox put — empty / error-bound queries
    # still emit the message (the TUI side surfaces usage / no-
    # match status). History only tracks non-empty arg shapes.
    _record_find_history(arg)
