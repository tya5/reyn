---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# Multi-agent config

> **Migration note**: The `multi_agent:` top-level YAML key was removed in FP-0004/0005. Both settings now live under the unified `safety:` block in `reyn.yaml`. Update any existing `multi_agent:` entries:
>
> | Old (`multi_agent:`) | New (`safety:`) |
> |---|---|
> | `multi_agent.max_hop_depth` | `safety.loop.max_agent_hops` |
> | `multi_agent.chain_timeout_seconds` | `safety.timeout.chain_seconds` |

The behavior is unchanged; only the YAML key path moved.

## Current schema (under `safety:`)

```yaml
safety:
  loop:
    max_agent_hops: 3          # default: 3
  timeout:
    chain_seconds: 60.0        # default: 60.0; 0 disables
```

See [Reference: `reyn.yaml` — `safety` block](reyn-yaml.md#safety-block) for the full schema.

## `safety.loop.max_agent_hops` (integer, default `3`)

Caps how deep an agent-to-agent message chain may traverse before the runtime refuses further sends. Modeled after LangGraph's recursion limit.

**Depth meaning**:

- `depth = 0` — the original user input
- `depth = 1` — first agent-to-agent send (e.g., `default → researcher`)
- `depth = 2` — researcher delegates further (e.g., `researcher → archivist`)
- `depth = N` — Nth hop

A send with `depth > max_agent_hops` is refused: the originator gets an `error` outbox message ("agent message depth N exceeds limit M; chain refused") and an `agent_message_refused` event is recorded with `reason="max_hop_depth"`. The upstream pending chain stays registered until `chain_seconds` (see below) elapses, at which point it's resolved with a synthesized error response — so a hop refusal mid-tree degrades gracefully rather than hanging.

The default of `3` allows `user → A → B → C` (= 3 hops) but stops `user → A → B → C → D`. Raise it for deeply hierarchical topologies (e.g., a 5-level tree expressed as overlapping teams).

## `safety.timeout.chain_seconds` (float, default `60.0`)

Wall-clock budget for a pending chain in a delegating agent. When a router decision emits `messages_to_agents`, the runtime registers a `_PendingChain` keyed by `chain_id` and arms a watchdog task. If every delegate responds, the watchdog is cancelled when the chain resolves; if not, after `chain_seconds` the runtime synthesizes an error response upstream:

```
chain timeout: 1 delegate(s) (gamma) did not respond within 60s
```

and emits a `chain_timeout` event with `chain_id`, `waiting_on`, `timeout_seconds`, `origin_agent`. The pending chain is cleared so the upstream agent's loop is no longer blocked.

Set `chain_seconds: 0` (or any non-positive value) to disable the watchdog — useful for tests and experiments where slow delegates are expected. Disabled chains can still hang indefinitely if a delegate never responds.

The default of `60.0` is a compromise: most chains finish in 10–30s for typical 3-hop trees with light/strong models. Raise it for skill chains that genuinely take longer (large web research fan-outs, long compaction passes); lower it for tighter SLAs.

## Example

```yaml
safety:
  loop:
    max_agent_hops: 5
  timeout:
    chain_seconds: 120.0
```

## Where it's read

- `chat/session.py` reads `safety.loop.max_agent_hops` and `safety.timeout.chain_seconds` on `reyn chat` startup.
- Per-process scope; not per-agent. Every agent in the process shares the same caps.

## Considered but not adopted

- `topology_policy` — was considered but rejected in favor of the auto-managed `_default` topology (see [concepts/topology](../../concepts/topology.md))

## See also

- [Concepts: multi-agent](../../concepts/multi-agent.md)
- [Reference: chat CLI](../cli/chat.md)
- [Reference: events](../runtime/events.md) — `agent_message_*` events carry `chain_id` and `depth`
