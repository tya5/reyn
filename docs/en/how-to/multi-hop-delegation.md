---
type: how-to
topic: multi-agent
audience: [human]
applies_to: [reyn chat, agent_request, agent_response]
---

# Trace and debug a multi-hop delegation

**Goal:** Understand what's happening when one agent's request fans out to peers and comes back as a synthesized reply. Useful for debugging, capacity planning, and writing skills that depend on chain semantics.

## When to use

- The user got a final reply but you want to know *which* agents contributed.
- A chain hangs and you suspect a delegate isn't responding.
- You're tuning `multi_agent.max_hop_depth` and need to see real chains.
- You're building a skill that emits `messages_to_agents` and want to verify the deferred-reply mechanic from the outside.

## What you'll see at the user seat

For a user-initiated chain, the originating agent's first router pass sends an interim reply immediately:

```
> Investigate DuckDB v1's breaking changes and produce a 200-word changelog summary.
[lead] (researching with researcher and writer)
```

After every delegate responds, the originating agent's router runs again with their replies in history and produces the final synthesized text:

```
[lead] DuckDB v1.0 (2024-06) introduced four breaking changes...
       (200-word summary follows)
```

The interim message is **not** an artifact of streaming or partial output — it's a separate, complete LLM turn. PR14's deferred-reply only applies to *agent-initiated* chains; user-initiated chains keep the interim+final UX so you can see "I'm working on it" right away.

## Setup for this walkthrough

```bash
reyn agent new lead       --role "team lead. Triages and synthesizes."
reyn agent new researcher --role "deep technical research, primary sources only."
reyn agent new archivist  --role "verifies historical context (release notes, blog posts)."
```

No topologies — the auto-managed `_default` covers them, so all three can talk to each other freely. Attach to `lead`:

```bash
reyn chat lead
```

## Tracing one chain end-to-end

Every top-level user submission gets a fresh `chain_id` (uuid4 hex), threaded through every subsequent agent-to-agent message. Find it for a single chain:

```bash
# After your turn finishes:
tail -1 .reyn/agents/lead/events.jsonl | jq -r '.data.chain_id'
# → 71d6c8b8e7e04a0d8b6f1e3c8d92a4ab
```

Now find every event that touched this chain across all agents:

```bash
CHAIN=71d6c8b8e7e04a0d8b6f1e3c8d92a4ab
for agent in lead researcher archivist; do
    echo "=== $agent ==="
    grep "$CHAIN" .reyn/agents/$agent/events.jsonl
done
```

You'll see something like:

```
=== lead ===
{"type":"user_message_received","data":{"chain_id":"71d6...","text":"Investigate DuckDB v1..."}}
{"type":"agent_message_sent","data":{"kind":"agent_request","from_agent":"lead","to_agent":"researcher","depth":1,"chain_id":"71d6..."}}
{"type":"agent_response_received","data":{"from_agent":"researcher","depth":1,"chain_id":"71d6..."}}

=== researcher ===
{"type":"agent_request_received","data":{"from_agent":"lead","depth":1,"chain_id":"71d6..."}}
{"type":"agent_message_sent","data":{"kind":"agent_request","from_agent":"researcher","to_agent":"archivist","depth":2,"chain_id":"71d6..."}}
{"type":"agent_response_received","data":{"from_agent":"archivist","depth":2,"chain_id":"71d6..."}}
{"type":"agent_message_sent","data":{"kind":"agent_response","from_agent":"researcher","to_agent":"lead","depth":1,"chain_id":"71d6..."}}

=== archivist ===
{"type":"agent_request_received","data":{"from_agent":"researcher","depth":2,"chain_id":"71d6..."}}
{"type":"agent_message_sent","data":{"kind":"agent_response","from_agent":"archivist","to_agent":"researcher","depth":2,"chain_id":"71d6..."}}
```

Reading top-to-bottom: `user → lead → researcher → archivist → researcher → lead → user`. The depths tell you how far from the user submission each hop is.

## What deferred reply looks like in the events

Notice that `researcher` does NOT emit `agent_message_sent (response)` to `lead` until **after** `agent_response_received` from `archivist` arrives. That's PR14's deferred-reply mechanic: when `researcher`'s router emits `messages_to_agents` (here, to `archivist`), the registry holds a `_PendingChain` keyed by `chain_id`, and `lead`'s reply waits until every entry in `waiting_on` resolves.

For a fan-out (researcher delegates to multiple peers in one turn), every delegate must respond before researcher's router runs again to synthesize. A single slow delegate currently delays the whole synthesis — chain timeout is on the residual list.

## Watching live with `:attach`

While `lead` is processing the user turn, you can switch the REPL pointer to a delegate to watch its progress:

```
> Investigate DuckDB v1's breaking changes and produce a 200-word changelog summary.
[lead] (researching with researcher and writer)

:attach researcher
attached: researcher

[researcher] (verifying with archivist)
[researcher] DuckDB v1 introduced...
```

`lead`'s `session.run()` keeps consuming its inbox in the background, so when you switch back (`:attach lead`) the synthesized final reply is already there.

## `max_hop_depth` refusal

If your overlapping topologies form a deeper tree than `multi_agent.max_hop_depth` allows, the runtime refuses the over-deep send:

```
[error] agent message depth 4 exceeds limit 3; chain refused
```

and emits an audit event:

```json
{"type":"agent_message_refused","data":{"reason":"max_hop_depth","to_agent":"deep_specialist","depth":4,"chain_id":"71d6..."}}
```

The originating chain's pending state in the upstream agent will not auto-recover currently — that's the `chain_timeout_seconds` work on the residual list. Until then, restart the process if a chain hangs after a max-hop refusal.

## Inspecting history meta

Each agent's `history.jsonl` records the messages it sent and received with `meta.source` identifying which side it was on, plus the `chain_id`:

```bash
grep "71d6c8b8" .reyn/agents/researcher/history.jsonl | jq '{role, source: .meta.source, depth: .meta.depth, text: .text[:60]}'
```

```
{"role":"user","source":"agent_request","depth":1,"text":"Look up breaking changes..."}
{"role":"agent","source":"agent_request_outgoing","depth":2,"text":"Verify the v0.x release notes..."}
{"role":"user","source":"agent_response","depth":2,"text":"v0.9 had no breaking changes..."}
{"role":"agent","source":"agent_response_outgoing","depth":1,"text":"DuckDB v1 introduced..."}
```

Four entries, four `meta.source` values: incoming request, outgoing delegation, incoming response, outgoing reply. The full chain on this agent's side is reconstructable from the file alone.

## Anti-pattern: relying on chain_id in skill input

`chain_id` is **audit-only** — the router LLM does not see it. Don't write phase prompts that reference it; treat it strictly as a debugging breadcrumb. If you need cross-skill correlation in skill code, use `run_id` (which the OS already plumbs into `meta`).

## See also

- [Concepts: multi-agent](../concepts/multi-agent.md) — chain semantics, deferred reply, fan-out
- [Reference: events](../reference/runtime/events.md) — `agent_message_*` event payloads with `chain_id`
- [Reference: multi-agent config](../reference/config/multi-agent.md) — `max_hop_depth`
- [How-to: debug with events](debug-with-events.md)
- [How-to: build an agent team](build-an-agent-team.md)
