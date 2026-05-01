---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# `multi_agent` config

Top-level block in `reyn.yaml` controlling agent-to-agent messaging behavior.

## Schema

```yaml
multi_agent:
  max_hop_depth: 3   # default: 3
```

## `max_hop_depth` (integer, default `3`)

Caps how deep an agent-to-agent message chain may traverse before the runtime refuses further sends. Modeled after LangGraph's recursion limit.

**Depth meaning**:

- `depth = 0` — the original user input
- `depth = 1` — first agent-to-agent send (e.g., `default → researcher`)
- `depth = 2` — researcher delegates further (e.g., `researcher → archivist`)
- `depth = N` — Nth hop

A send with `depth > max_hop_depth` is refused: the originator gets an `error` outbox message ("agent message depth N exceeds limit M; chain refused") and an `agent_message_refused` event is recorded with `reason="max_hop_depth"`. The deferred-reply pending chain in the upstream agent will time out via the chain's own missing-response handling (see [residuals](#) — chain timeout is not yet implemented; an unanswered chain currently hangs until process exit).

The default of `3` allows `user → A → B → C` (= 3 hops) but stops `user → A → B → C → D`. Raise it for deeply hierarchical topologies (e.g., a 5-level tree expressed as overlapping teams).

## Example

```yaml
multi_agent:
  max_hop_depth: 5

# rest of reyn.yaml ...
```

## Where it's read

- `cli/commands/chat.py` reads it on `reyn chat` startup and passes it to `ChatSession.__init__` via `max_hop_depth=`.
- Per-process scope; not per-agent. Every agent in the process shares the same cap.

## Future fields (residuals)

Planned but not yet implemented:

- `chain_timeout_seconds` — time-bound for pending chains, with automatic error-response back to the upstream agent
- `topology_policy` — was considered but rejected in favor of the auto-managed `_default` topology (see [concepts/topology](../../concepts/topology.md))

## See also

- [Concepts: multi-agent](../../concepts/multi-agent.md)
- [Reference: chat CLI](../cli/chat.md)
- [Reference: events](../runtime/events.md) — `agent_message_*` events carry `chain_id` and `depth`
