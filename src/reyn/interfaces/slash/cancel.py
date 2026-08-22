"""``/cancel`` — the user-facing slash-command cancel口 (#3903).

Esc / Ctrl+C already cancel the in-flight turn (``app.py``'s keybindings,
both delegating straight to ``ClientTransport.cancel_inflight()`` — the
same seam this command uses). #3903's owner ruling required a cancel口
reachable via slash command specifically, distinct from the keybindings:
a slash command is discoverable (``/help``, Tab-completion), scriptable,
and reachable from any client that types text but doesn't wire a
keybinding (e.g. a plain ``--cui`` terminal without the inline CUI's key
handling). No confirmation step, unlike ``/reset``: cancelling an
in-flight turn is low-stakes and trivially retried (re-prompt), the same
posture Esc/Ctrl+C already take without asking.
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, slash


@slash(
    "cancel",
    summary="Cancel the in-flight turn (same as Esc / Ctrl+C)",
    locus="client",
)
async def cancel_cmd(ctx: "SlashContext", args: str) -> None:
    """``/cancel`` — reports WHAT was cancelled, never a blanket "done": the
    live #4166 finding was `cancel_task` returning `cancel_requested` while
    the target kept running with no way to tell. `ClientTransport.
    cancel_inflight()`'s own contract (#3903) now returns the real outcome
    (`Session.cancel_inflight()`'s summary for local/server-side dispatch;
    a generic string only for the one fire-and-forget wire path this
    command never actually routes through — see `agui/client.py`)."""
    summary = await ctx.transport.cancel_inflight()
    await reply(ctx, f"⏹ {summary}")
