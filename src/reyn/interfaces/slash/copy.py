"""/copy — copy an agent reply to the system clipboard.

The inline CUI's output loop keeps a bounded ring of the most recent agent
replies, so this command can target older ones too — not just the latest. The
argument selects which reply (1 = newest, 2 = one before that, …).

This command sends a sentinel ``__copy_last_reply__`` OutboxMessage with the
parsed argument in ``text``; the output loop intercepts it, picks the right
reply from its ring, and pipes it to the platform clipboard (``pbcopy`` /
``wl-copy`` / ``xclip`` / ``xsel`` / ``clip``), rendering the result as a
status line.

Usage::

    /copy            # copy the most recent agent reply
    /copy 2          # copy the reply before that (one turn back)
    /copy 3          # ... two turns back
    /copy list       # show how many replies are currently buffered
"""
from __future__ import annotations

from reyn.interfaces.slash import slash
from reyn.runtime.outbox import OutboxMessage


@slash(
    "copy",
    summary="Copy an agent reply to the clipboard",
    usage="/copy [N|list]",
)
async def copy_cmd(session: "object", args: str) -> None:
    # Forward the raw arg; the TUI handler validates and surfaces errors so
    # we don't duplicate the parsing logic across the slash + outbox layers.
    await session._put_outbox(OutboxMessage(
        kind="__copy_last_reply__", text=(args or "").strip(),
    ))
