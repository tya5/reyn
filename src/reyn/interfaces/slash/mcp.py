"""``/mcp`` — per-server MCP actions (#4401 ③: retry a failed probe).

The mcp pane's "↻ retry probe" row (chrome.py's ``_mcp_pane_entries``,
appended only under a ``"failed"`` server row) submits this command; it
dispatches through ``ClientTransport.request_mcp_retry`` (#3595 S4: a
slash handler reaches the CLIENT seam, never a ``Session`` member added
just so it could — ``Session.retry_mcp_probe`` was BLOCKING'd for exactly
that shape, PR #5761 lead-coder review, and folded into
``Session._retry_mcp_probe``, private, reachable only from
``request_mcp_retry``'s own production implementations). This handler
AWAITS it — deliberately not fire-and-forget (see
``RouterHostAdapter.retry_mcp_probe``'s own docstring for why a background
task is out of scope here). The row's own state (``Session.mcp_probe_
state``, a genuinely public read-model forwarder) already reads
"retrying…" the instant the retry starts (any render that happens during
this await sees it); this command's own reply just confirms the outcome
once the probe settles, up to ``per_server_timeout`` later.
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashContext, reply, reply_error, slash


@slash(
    "mcp",
    summary="MCP server actions — retry a failed probe",
    locus="session",
    usage="/mcp retry <server>",
)
async def mcp_cmd(ctx: "SlashContext", args: str) -> None:
    """``/mcp retry <server>`` — re-probe one mcp server (#4401 ③), waiting
    for it to settle. Bypasses the server's own #5674 failure cooldown (a
    manual retry that silently no-ops until 60s have passed would look
    like nothing happened) — see ``Session._retry_mcp_probe``'s own
    docstring."""
    parts = args.split()
    if len(parts) != 2 or parts[0] != "retry":
        await reply_error(ctx, "usage: /mcp retry <server>")
        return
    server = parts[1]
    happened = await ctx.transport.request_mcp_retry(server)
    if not happened:
        await reply_error(ctx, f"mcp probe retry for {server!r} is not available here")
        return
    await reply(ctx, f"mcp probe retry for {server!r} finished — see the mcp pane for the result")
