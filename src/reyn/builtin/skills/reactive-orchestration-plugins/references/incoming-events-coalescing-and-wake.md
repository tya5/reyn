## The push path

A server-pushed `notifications/resources/updated` surfaces as the
`mcp_resource_updated` hook point. Bridge: `src/reyn/mcp/message_handler.py`.
Subscribe call: `src/reyn/mcp/client.py`.

The notification carries the **URI, not the resource body** -- the reacting
side re-reads.

**Getting subscribed (#5167).** A hook whose `matcher` names a CONCRETE
`server` and a non-glob `uri` (e.g. `{"server": "docs", "uri":
"orch://job/42/done"}`) is subscribed AUTOMATICALLY at session start -- no
tool call needed on your plugin's part. A hook whose `matcher` globs `uri`
(see below) is NOT auto-subscribed -- the agent still needs to call
`subscribe_mcp_resource` explicitly for each concrete URI that pattern is
meant to cover, since a glob names a SET of resources, not the one exact URI
an MCP subscribe request requires. If your plugin's design depends on a
specific URI firing without the agent ever having to call the tool, declare
that URI literally rather than only under a glob matcher.

**The URI is your signal namespace.** `matcher` glob-matches `uri` (see the
glob-field set in `src/reyn/hooks/matcher.py`), so ONE hook point carries
unlimited distinct signals: `orch://job/<id>/done`, `orch://job/<id>/failed`,
`app://status`. Carry correlation ids in the URI. Do not ask for a new event
kind per signal.

## Coalescing bursts

A flapping job, or a file saved five times while someone edits, is the
**Composer's** job -- not your server's, and not a sleep in a hook.
`window` / `debounce` live in `src/reyn/hooks/composer.py`, are declared as
`composers:` and consumed as `composed:<name>`.

Trailing debounce = **"react to the settled state"**, which is almost always
what you want after a burst of edits. Check that module's docstring for the
crash-durability guarantee before depending on a pending buffer surviving.

## Wake vs ride-along

The action seams are listed at the top of `src/reyn/hooks/dispatcher.py`.
Two of them decide whether the agent is interrupted:

- **wake=true** (inbox push) -- starts a turn NOW. Loop-valved; find the knob
  under `safety.loop` in the chat config and confirm the current default.
- **wake=false** (next-turn ride-along) -- benign, costs nothing, but **if no
  next turn ever comes it is never consumed**.

Choose by asking *"is a human already in this loop?"* If yes, ride-along:
the context is already staged when they next speak, so the first reply is
fully informed and no turn is spent on noise. If nobody is watching, wake --
and expect the valve to bound a runaway edit->break->fix->break cycle.

**Do not make ride-along the only path for something that must be acted on.**
