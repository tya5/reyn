"""Shared ``__copy_last_reply__`` (``/copy``) sentinel handling for every client.

``/copy`` is a CLIENT-side effect by design: the slash handler only posts a
``__copy_last_reply__`` sentinel carrying the raw argument
(:mod:`reyn.interfaces.slash.copy`), and whichever client consumes the frame
stream owns the reply ring and the clipboard call. That leaves the resolution
rule ("which buffered reply does ``/copy 2`` mean, and what does the user see
when there is none?") in the client — so it lives HERE, once, rather than being
re-derived per surface.

Two clients consume it today and both go through this module:
:mod:`reyn.interfaces.repl.stream_client` (the plain / ``--cui`` PromptSession
driver, which introduced this handling) and
:mod:`reyn.interfaces.inline.textual_chat.app` (the default Textual TUI, #3362 —
which skipped the sentinel entirely and so was a silent no-op: no status line
AND no clipboard write). Extracting the pair rather than re-implementing it in
the TUI is deliberate: a second idiom would let the two surfaces drift on the
arg grammar and on the empty/out-of-range wording, which is exactly the
divergence that let the TUI hole survive unnoticed.
"""
from __future__ import annotations

from reyn.runtime.outbox import OutboxMessage

from ._clipboard import copy_to_clipboard_async

#: How many recent agent replies ``/copy`` can target (1 = newest). Clients size
#: their newest-first reply ring with this so ``/copy N``'s upper bound is the
#: same number on every surface.
COPY_BUFFER_MAX = 20


def resolve_copy_target(recent_replies, arg: str) -> "tuple[str | None, str]":
    """Pure: resolve a ``/copy`` arg against the newest-first reply buffer.

    Returns ``(text_to_copy, status)``. ``text_to_copy`` is None when there is
    nothing to copy — ``status`` then explains why (list view / empty buffer /
    bad arg / out of range). ``recent_replies[0]`` is the newest reply.
    """
    arg = (arg or "").strip()
    n_buf = len(recent_replies)

    def _plural(n: int) -> str:
        return "reply" if n == 1 else "replies"

    if arg == "list":
        if not n_buf:
            return None, "no replies buffered yet"
        return None, f"{n_buf} {_plural(n_buf)} buffered (/copy N — 1 = newest)"
    n = 1
    if arg:
        if not arg.isdigit() or int(arg) < 1:
            return None, f"bad /copy arg {arg!r}; use a number (1 = newest) or 'list'"
        n = int(arg)
    if not n_buf:
        return None, "no agent reply to copy yet"
    if n > n_buf:
        return None, f"only {n_buf} {_plural(n_buf)} buffered"
    return recent_replies[n - 1], ""


async def handle_copy_sentinel(recent_replies, arg: str) -> OutboxMessage:
    """Resolve a ``/copy`` request, copy, and return a status frame to render.

    Replaces the unhandled ``__copy_last_reply__`` sentinel (a silent no-op
    before this) with a real clipboard copy + a visible result line. The
    clipboard write is the EFFECT; the returned ``status`` frame is only its
    report, so a caller that renders the frame without awaiting this coroutine
    would witness nothing.
    """
    text, status = resolve_copy_target(recent_replies, arg)
    if text is not None:
        ok, tool = await copy_to_clipboard_async(text)
        status = (
            f"copied reply to clipboard ({tool})" if ok
            else "no clipboard tool found — install pbcopy / xclip / wl-copy / xsel"
        )
    return OutboxMessage(kind="status", text=status)


__all__ = ["COPY_BUFFER_MAX", "handle_copy_sentinel", "resolve_copy_target"]
