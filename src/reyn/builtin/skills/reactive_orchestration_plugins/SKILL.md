---
name: reactive_orchestration_plugins
description: How to build something that REACTS to an external system (an orchestrator, a watcher, a UI, any MCP server that pushes) -- which reyn mechanism already answers each requirement, and the anti-pattern list of things people re-invent. Read this BEFORE designing any external-event-driven plugin, server-push handling, wake/notification behaviour, or browser UI integration. Companion to reyn_cheat_sheet (which covers choosing between skill/pipeline/mcp/hook/present in general).
---

# Reactive / orchestration plugins

reyn<->MCP is **bidirectional**: you call the server's tools, and the server
pushes back at you. The recurring failure when designing here is **proposing
a mechanism reyn already has**.

**How to read this file.** It names *where to look*, not what the code
currently does. A doc that restates behaviour goes stale and then actively
misleads -- that is exactly how this file's own author got six design
decisions wrong in one session, including reading a stale `Status:` header
and concluding an implemented subsystem did not exist. **Open the cited path
and confirm before you rely on it.** If a claim here and the code disagree,
the code wins and this file is the bug.

## Reuse map -- check here before designing anything

| You are about to build | Already exists |
|---|---|
| A new event kind per signal | **URI namespace** on one hook point |
| Burst/flap suppression in your server | **Composer** `window` / `debounce` |
| A callback convention to return results | **`pipeline_launch`** + a `shell` step |
| A channel to ask the human a question | **MCP elicitation** |
| A wire protocol for a browser UI | **AG-UI** |
| A crash-recovery story for external state | Nothing -- **out of scope by ruling** |

## The push path

A server-pushed `notifications/resources/updated` surfaces as the
`mcp_resource_updated` hook point. Bridge: `src/reyn/mcp/message_handler.py`.
Subscribe call: `src/reyn/mcp/client.py`.

The notification carries the **URI, not the resource body** -- the reacting
side re-reads.

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

## Returning a conclusion to the outside

You do **not** need a callback convention. `pipeline_launch` renders
`input_template` against the event's template vars -- so a correlation id
carried in the URI reaches the pipeline -- runs async, and the result comes
back on this session's own inbox. A `shell` step is the write-back leg to the
external system. A worked `pipeline_launch` example lives in the Hooks
section of the `reyn_cheat_sheet` skill.

## Asking the human

MCP **elicitation** is installed per connection with a timeout and a
listener check (`src/reyn/mcp/connection_service.py`). Use it instead of
inventing a question channel.

**Sampling is a different primitive** (server asks the client for a model
completion). Check whether it is wired at all before designing around it.

## Scope -- a workspace-level hook fires in EVERY session

Config layers: the workspace `hooks:` config, and per-agent under
`.reyn/agents/<name>/hooks.yaml` (`load_per_agent_hooks` in
`src/reyn/config/loader.py`). The bus and registry are per-session
(`src/reyn/hooks/bus.py`).

If your reaction is only meaningful while a particular server is attached,
say so in the design -- otherwise unrelated sessions react to your events.

## What a plugin may ship

The capability union is `src/reyn/plugins/manifest.py`; what install actually
registers is `src/reyn/core/op_runtime/plugin_install.py`. **Read both before
assuming your plugin can ship a given part** -- the set is smaller than the
set of things reyn supports.

## Vocabulary -- two different UI protocols

Never write bare "UI protocol"; they are not interchangeable.

- **AG-UI** -- the agent<->UI **wire transport** (event stream + turn
  submit). This is what a browser or a thin client speaks to reyn:
  `src/reyn/interfaces/transport/agui/`.
- **A2UI** -- an agent-generated-UI **component spec**. reyn's present layer
  is *structurally isomorphic* to it, which is **not** the same as speaking
  it on the wire. See `docs/deep-dives/proposals/0054-present-layer.md`.

## Three constraints that surprise people

**Progress is not a hook.** `notifications/progress` is deliberately NOT
bridged to a hook point — the SDK already dual-delivers it to the per-call
`progress_callback` of the call *reyn itself made*. Your server's own
background-job progress has no such call to ride. Publish progress as a
**resource** instead (`orch://job/<id>/progress`) and let the ordinary
subscribe→`mcp_resource_updated` path carry it.

**A webhook's body never reaches your matcher.** `webhook_received` delivers
routing metadata ONLY (`transport`, `sender`) — the raw request body is never
put in template_vars, so tokens/PII cannot leak there
(`src/reyn/hooks/ingress.py`, the adapter's SECURITY invariant). You can match
"something arrived from X", not "the amount was 500". Content-driven flows
must fetch the content themselves at the gateway-plugin layer.

**Nothing detects absence.** Every Composer op (`all`/`any`/`seq`/`window`/
`debounce`/`correlate_by`/`count`) composes events that *happened*. There is
no "fire if Y did NOT arrive within T" — so deadman monitoring, heartbeat-gone
alerts, and approval-timeout escalation are not expressible today. Do not
mistake an inbox `escalate_after` for this: that watches an unconsumed inbox
entry, not a missing external event.

## Size ceiling for skills

A skill body is read through the ordinary read op, whose inline cap is
**derived from the model window** (`_read_inline_cap` in
`src/reyn/core/op_runtime/file.py`). With no model resolved the cap is small.
A body over the cap comes back `status: "truncated"` -- the reader silently
gets a partial skill. Keep a skill body well under it, and prefer a new
sibling skill over growing an existing one past its ceiling.
