"""/copy — copy the last agent reply to the system clipboard.

The TUI captures mouse events for its own widget interactions, which breaks
terminal-native click-and-drag selection. Most terminals offer a modifier-
key bypass (Option/Fn/Shift drag) but that's an awkward two-handed gesture
when you just want to grab the last response.

This command sends a sentinel ``__copy_last_reply__`` OutboxMessage; the TUI
app intercepts it, fetches ``ConversationView.last_reply_text()``, and pipes
the text to the platform clipboard (`pbcopy` / `xclip` / `wl-copy` / `clip`).

Usage::

    /copy            # copy the most recent agent reply
"""
from __future__ import annotations

from reyn.chat.outbox import OutboxMessage
from reyn.chat.slash import slash


@slash("copy", summary="Copy the last agent reply to the system clipboard")
async def copy_cmd(session: "object", args: str) -> None:
    await session._put_outbox(OutboxMessage(kind="__copy_last_reply__", text=""))
