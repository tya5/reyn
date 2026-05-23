"""/find — search the conv pane buffer for a substring.

Categorical UX gap fill (= "I said something about X earlier, where
is it?"). Searches the current RichLog buffer for case-insensitive
substring matches and jumps to the first one near the current
scroll position. The "search what's visible in scrollback" scope
is the natural TUI-internal boundary — agent-reply history past
the ring-buffer trim lives in ``.reyn/events/agents/<name>/`` and
is reachable via the right-panel Events tab + ``/list`` slash;
this command surfaces just the in-pane content.

Pattern: same shape as ``/copy`` — the slash command emits a
sentinel ``__find__`` OutboxMessage with the raw query in
``text``; the TUI app intercepts via ``_on_find`` in app_outbox,
runs ``ConversationView.find_in_buffer`` against the live
RichLog, scrolls to the nearest match, and writes a status line
with the match count.

Usage::

    /find tokens     # find lines containing "tokens" (case-insensitive)
    /find foo bar    # multi-word query (literal substring, not regex)
    /find            # empty query → status reports usage
"""
from __future__ import annotations

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import slash


@slash(
    "find",
    summary="Search the conv pane for a substring",
    usage="/find <query>",
)
async def find_cmd(session: "object", args: str) -> None:
    # Forward the raw arg; the TUI handler validates and surfaces
    # errors so we don't duplicate the parsing logic across the
    # slash + outbox layers. Matches the ``/copy`` pattern.
    await session._put_outbox(OutboxMessage(
        kind="__find__", text=(args or "").strip(),
    ))
