---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# Multi-agent config

> **Migration note**: The `multi_agent:` top-level YAML key has been removed. Both settings now live under the unified `safety:` block in `reyn.yaml`. Update any existing `multi_agent:` entries:
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

> **Multi-hop is permanently retired; `max_agent_hops` changed MEANING, not
> gone** (architect ruling + lead-coder measurement, #3978/#4135,
> 2026-08-10): `run_prompt(collect="async")` — the current agent-to-agent
> producer (proposal 0067 P4e) — always registers a single-hop chain
> (`depth=1`, `|waiting_on| == 1`); it has no mechanism for a target to
> "further delegate" and extend the chain the way the pre-retirement model
> below describes. `max_agent_hops`'s depth-refusal code is genuinely live
> (`run_prompt(async)`'s delivery goes through the same
> `InterAgentMessaging.send_to_agent` depth check, `depth > max_agent_hops`
> at `inter_agent_messaging.py`) — but because `depth` is now the constant
> `1`, the setting no longer caps a chain's depth (there is no chain to
> traverse). Any value `>= 1` (including the default `3`) always passes;
> `max_agent_hops: 0` (no lower-bound validation exists — it's passed
> through `int()`) does NOT fail the call itself — `run_prompt_async`
> registers the chain, arms the timeout, and returns `{"status":
> "started"}` with a `task_id` BEFORE the depth check runs. The depth
> check fires one step later, at delivery (`_send_to_agent`), refusing
> DELIVERY only — the caller has already gotten a successful `task_id`
> back and the chain then times out under `chain_seconds`, resolving as a
> synthesized error rather than failing immediately. The setting still
> guarantees eventual failure at `0`, just not an immediate one — not a
> depth cap. `chain_seconds` (below) is fully live and unchanged in
> meaning: a `run_prompt(async)` call that never gets a reply genuinely
> times out under it. In short: the cap concept is inherited from the
> retired multi-hop model, but its enforcement point sits on the
> currently-used transport (`InterAgentMessaging.send_to_agent`) — setting
> `0` to "restrict the retired mechanism" would, in effect, stop live
> `run_prompt(async)` delivery instead.

## `safety.loop.max_agent_hops` (integer, default `3`)

Caps how deep an agent-to-agent message chain may traverse before the runtime refuses further sends. Modeled after LangGraph's recursion limit — inherited from the pre-retirement multi-hop model (see the callout above). Today's sole producer always dispatches at `depth=1`, so any positive value keeps agent-to-agent messaging enabled; `0` (or lower) stops delivery instead (the call itself still returns a `task_id`, then resolves as a timeout error — see the callout above for the exact shape).

**Depth meaning**:

- `depth = 0` — the original user input
- `depth = 1` — first agent-to-agent send (e.g., `default → researcher`) — where `run_prompt(async)` always registers today
- `depth = 2` — researcher delegates further (e.g., `researcher → archivist`) — no current producer reaches this
- `depth = N` — Nth hop

A send with `depth > max_agent_hops` is refused: the originator gets an `error` outbox message ("agent message depth N exceeds limit M; chain refused") and an `agent_message_refused` event is recorded with `reason="max_hop_depth"`. The upstream pending chain stays registered until `chain_seconds` (see below) elapses — what happens THEN is governed by `safety.on_limit` (FP-0005), not an unconditional error: the shipped default (`mode: interactive`) pauses and asks whether to keep waiting before synthesizing an error; only `mode: unattended` resolves it immediately as a synthesized error response. See `safety.on_limit` in `src/reyn/config/chat.py`'s `OnLimitConfig` for the full mode set — a hop refusal mid-tree degrades gracefully either way, it just doesn't necessarily resolve silently.

The default of `3` was sized for `user → A → B → C` (= 3 hops) under the pre-retirement multi-hop model. Today, any value `>= 1` behaves identically (agent-to-agent messaging enabled); raise it above `3` only once a producer exists that can create depth beyond 1. `0` does not reject a `run_prompt(async)` call outright — the caller still gets a `task_id` back — but delivery is refused and the chain resolves as a timeout error via `chain_seconds`, so it still guarantees the message never reaches the target, just not synchronously.

## `safety.timeout.chain_seconds` (float, default `60.0`)

Wall-clock budget for a pending chain. `run_prompt(collect="async")`'s registered chain arms this watchdog the same way the pre-retirement model did. What happens when the watchdog fires is `safety.on_limit`-gated (FP-0005, `ChainTimeoutGlue.on_chain_timeout_fire`): the shipped default (`mode: interactive`) pauses and asks whether to re-arm with a fresh deadline; only `mode: unattended` synthesizes the error response immediately, unconditionally. Under `unattended` (or once `interactive` is refused/times out), the runtime synthesizes:

```
chain timeout: 1 delegate(s) (gamma) did not respond within 60s
```

and emits a `chain_timeout` event with `chain_id`, `waiting_on`, `timeout_seconds`, `origin_agent`. The pending chain is cleared so the caller's loop is no longer blocked.

Set `chain_seconds: 0` (or any non-positive value) to disable the watchdog — useful for tests and experiments where a slow peer is expected. Disabled chains can still hang indefinitely if the peer never responds.

The default of `60.0` was sized for the pre-retirement multi-hop model's 3-hop trees; a single `run_prompt(async)` call typically settles faster. Raise it for calls that genuinely take longer (large web research, long compaction passes); lower it for tighter SLAs.

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

- `topology_policy` — was considered but rejected in favor of the auto-managed `_default` topology (see [concepts/topology](../../concepts/multi-agent/topology.md))

## See also

- [Concepts: multi-agent](../../concepts/multi-agent/multi-agent.md)
- [Reference: chat CLI](../cli/chat.md)
- [Reference: events](../runtime/events.md) — `agent_message_*` events carry `chain_id` and `depth`
