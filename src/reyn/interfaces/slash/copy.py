"""/copy — copy an agent reply to the system clipboard.

The inline CUI's output loop keeps a bounded ring of the most recent agent
replies, so this command can target older ones too — not just the latest. The
argument selects which reply (1 = newest, 2 = one before that, …).

This command sends a sentinel ``__copy_last_reply__`` OutboxMessage carrying
the parsed argument; the output loop intercepts it, picks the right reply
from its ring, and pipes it to the platform clipboard (``pbcopy`` /
``wl-copy`` / ``xclip`` / ``xsel`` / ``clip``), rendering the result as a
status line.

#6230 stage 1 (issue thread ruling, architect + lead-coder): ``text`` is the
standard AG-UI channel (``{"text": text}``, reaches a client that carries
ZERO reyn-specific rendering) and MUST carry a genuinely human-readable
representation, never the control value a reyn-aware consumer needs to
act — that control value (the raw arg: a digit, ``"list"``, or empty) now
lives in ``meta["arg"]``, the private channel only a reyn-aware client
reads.

Before this stage ``text`` carried the bare argument itself — a value meant
for :func:`~reyn.interfaces.repl._copy_sentinel.resolve_copy_target`, not a
human. A surface with no reyn-specific handler already printed that raw
argument verbatim (a digit, or "list", or nothing), which was never a
prepared fallback representation, only a leaked control value (#6230 issue
thread).

**Wire-meaning change, disclosed (not fixed — architect ruling, #6230):**
``__copy_last_reply__`` IS forwarded to the AG-UI wire (profiled ``CUSTOM``,
``reyn.display.__copy_last_reply__``) so a remote client of any vintage can
receive it. An OLDER client built against the pre-stage-1 contract still
reads ``text`` AS IF it were the bare arg and would try to act on this new
human sentence as one instead — visibly (it fails to parse as a digit or
"list", never silently), matching this arc's own standard ("見えて間違う方
が良い" — better to fail visibly than silently do nothing). This naturally
stops mattering once that client is rebuilt against the post-stage-1
contract. Nobody has checked whether such an older client actually exists
outside this codebase; this note states that plainly rather than implying
it was verified.

Usage::

    /copy            # copy the most recent agent reply
    /copy 2          # copy the reply before that (one turn back)
    /copy 3          # ... two turns back
    /copy list       # show how many replies are currently buffered
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, slash
from reyn.runtime.outbox import OutboxMessage


@slash(
    "copy",
    summary="Copy an agent reply to the clipboard",
    locus="client",
    usage="/copy [N|list]",
)
async def copy_cmd(ctx: "SlashContext", args: str) -> None:
    # The control arg (validated client-side, in the output loop, so we
    # don't duplicate the parsing logic across the slash + outbox layers)
    # moves to meta; text carries a human-readable fallback instead.
    arg = (args or "").strip()
    label = arg or "most recent reply"
    ctx.transport.put_display(OutboxMessage(
        kind="__copy_last_reply__",
        text=f"copy request: {label}",
        meta={"arg": arg},
    ))
