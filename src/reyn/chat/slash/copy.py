"""/copy — copy an agent reply to the system clipboard.

The TUI captures mouse events for its own widget interactions, which breaks
terminal-native click-and-drag selection. Most terminals offer a modifier-
key bypass (Option/Fn/Shift drag) but that's an awkward two-handed gesture
when you just want to grab a response.

ConversationView keeps a bounded ring of the most recent agent replies, so
this command can target older ones too — not just the latest. The argument
selects which reply (1 = newest, 2 = one before that, …).

This command sends a sentinel ``__copy_last_reply__`` OutboxMessage with the
parsed argument in ``text``; the TUI app intercepts it, fetches the right
reply via ``ConversationView.reply_at(n)``, and pipes to the platform
clipboard (``pbcopy`` / ``xclip`` / ``wl-copy`` / ``clip``).

Usage::

    /copy            # copy the most recent agent reply
    /copy 2          # copy the reply before that (one turn back)
    /copy 3          # ... two turns back
    /copy list       # show what's currently buffered
"""
from __future__ import annotations

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import slash


@slash("copy", summary="Copy an agent reply to the clipboard (/copy [N] or /copy list)")
async def copy_cmd(session: "object", args: str) -> None:
    # Forward the raw arg; the TUI handler validates and surfaces errors so
    # we don't duplicate the parsing logic across the slash + outbox layers.
    await session._put_outbox(OutboxMessage(
        kind="__copy_last_reply__", text=(args or "").strip(),
    ))
