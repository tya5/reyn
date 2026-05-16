---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# Budget and cost tracking

## Overview

Reyn tracks LLM token usage and USD cost per session, per-agent, per-chain,
and per-model. Token and USD totals accumulate as LLM calls complete;
configured caps refuse or warn before any call (or spawn) that would exceed
them. The system is entirely opt-in: without a `cost:` block in `reyn.yaml`,
runs are unlimited.

## `reyn.yaml` schema

All budget configuration lives under the top-level `cost:` key. Every field
is optional; omitting a sub-key (or setting its `hard_limit` to `null`) means
unlimited for that dimension.

```yaml
cost:
  # Per-agent caps — in-memory, reset on restart or /budget reset
  per_agent_tokens:
    hard_limit: 50000    # refuse after this many tokens for one agent
    warn_ratio: 0.8      # warn at 80% of hard_limit (default: 0.8)
  per_agent_cost_usd:
    hard_limit: 2.00     # refuse after $2.00 spent by one agent
    warn_ratio: 0.8

  # Per-model rate limit (hard cap, calls per 60-second window)
  rate_limit_per_minute:
    openai/gpt-4o: 60
  rate_limit_warn_ratio: 0.8   # warn at 80% of rate limit (default: 0.8)

  # Daily / monthly quotas — persistent across process restarts (PR25)
  # Stored in .reyn/state/budget_ledger.jsonl; auto-reset at midnight /
  # month boundary (local time).
  daily_tokens:
    hard_limit: 100000   # refuse after 100k tokens today
    warn_ratio: 0.8
  daily_cost_usd:
    hard_limit: 5.00     # refuse after $5.00 today
  monthly_tokens:
    hard_limit: 1000000  # refuse after 1M tokens this month
  monthly_cost_usd:
    hard_limit: 50.00    # refuse after $50.00 this month
```

> **Migration note**: `per_chain_skill_calls`, `per_chain_skill_tokens`, and `router_invocations_per_turn` were moved from `cost:` to `safety.loop` in FP-0004/0005. Use `safety.loop.skill_calls_per_chain`, `safety.loop.skill_tokens_per_chain`, and `safety.loop.max_router_calls_per_turn` instead. See [Reference: `reyn.yaml` — `safety` block](reyn-yaml.md#safety-block).

### Field reference

| Field | Scope | Persists | Resets |
|---|---|---|---|
| `per_agent_tokens` | per agent | in-memory | `/budget reset` or restart |
| `per_agent_cost_usd` | per agent | in-memory | `/budget reset` or restart |
| `rate_limit_per_minute` | per model | in-memory (60s window) | automatic sliding window |
| `rate_limit_warn_ratio` | global | — | — |
| `daily_tokens` | process-global | ledger file | midnight (local time) |
| `daily_cost_usd` | process-global | ledger file | midnight (local time) |
| `monthly_tokens` | process-global | ledger file | 1st of month (local time) |
| `monthly_cost_usd` | process-global | ledger file | 1st of month (local time) |

Each cap dimension has two optional sub-fields:

| Sub-field | Type | Default | Description |
|---|---|---|---|
| `hard_limit` | float or null | null (unlimited) | Refuse the next LLM call or spawn when this value is reached or exceeded. |
| `warn_ratio` | float | 0.8 | Emit a warning when usage reaches `hard_limit * warn_ratio`. A warning is emitted at most once per dimension per session. |

### USD cost calculation

USD cost is estimated via [LiteLLM's pricing lookup](https://github.com/BerriAI/litellm)
after each call. Both proxy-mode (LiteLLM) and direct-API paths are supported.
If the lookup returns no price for the model in use, the USD counter stays at
`$0.0000` and only tokens accumulate. Token counts are always reliable
regardless of pricing availability.

## Slash commands

While `reyn chat` is running, two slash commands expose the budget state.

### `/cost`

One-line summary for the currently attached agent:

```
/cost
```

Example output:

```
alice: 12,450 tokens, $0.0187  (this session)
```

Reports the in-memory counters for this agent since last restart (or last
`/budget reset`). Returns nothing when no `cost:` block is configured
(unlimited mode).

### `/budget`

Full breakdown across all dimensions and all agents seen this session:

```
/budget
```

Example output:

```
Usage (process invocation):

  Today (2026-05-09):   tokens 12,450 / 100,000 (12%) | $0.0187 / $5.00 (0%)
  Month (2026-05):      tokens 12,450 / 1,000,000 (1%) | $0.0187 / $50.00 (0%)

  alice (attached)
    tokens:       12,450 / 50,000  (warn at 40,000)
    cost:         $0.0187 / $2.00     (warn at $1.60)

  Per-chain skill calls:
    chain-abc/text_summarizer:  2 / 5

  Rate limit (last minute):
    openai/gpt-4o:  14 / 60  (warn at 48)

  Reset counters with `/budget reset`.
```

The "Today / Month" section appears only when `daily_*` or `monthly_*` caps
are configured and at least one LLM call has been made since startup.

### `/budget reset`

Clear the in-memory per-agent and per-chain counters:

```
/budget reset
```

Daily and monthly counters are **not** affected — they are backed by the
persistent ledger (`.reyn/state/budget_ledger.jsonl`) and auto-reset at
period boundaries. To clear them, delete or archive the ledger file while
the process is stopped.

## Cap tiers

Each dimension has two tiers:

**Soft warn** — emitted once when usage crosses `hard_limit * warn_ratio`.
The LLM call proceeds; a `[budget warn]` status message is shown to the user
in the REPL and an event is written to the event log.

**Hard refuse** — emitted when usage reaches or exceeds `hard_limit`. The LLM
call is refused *before* it is made (no tokens are consumed). A `[budget
exceeded]` message is shown to the user with current usage, the triggered
dimension, and three recovery actions:

```
[budget exceeded] agent 'alice' is over the hard limit.

  Triggered:  per_agent_tokens (50,123/50,000)
  Also used:  $0.0374

The next LLM call has been refused.

What you can do:
  • Raise the limit in `reyn.yaml` or `reyn.local.yaml` (cost: section)
  • Reset counters with `/budget reset`
  • Restart `reyn chat` (limits are per-process)
  • See current usage with `/budget`
```

For rate-limit violations (`rate_limit_per_minute`), the call is refused until
the next invocation falls within the 60-second window (no automatic sleep /
throttle — the user or calling code must retry).

## Events emitted

| Event | When emitted |
|---|---|
| `router_retry_exhausted` | `safety.loop.max_router_calls_per_turn` cap is reached; carries `count`, `cap`, `last_reason` |
| `budget_reset` | `/budget reset` is executed; carries `before` snapshot of counters |

Warning and refusal events are surfaced as outbox messages to the user rather
than as distinct event types. The `budget_warned` / `budget_refused` signal is
embedded in the outbox message text and the `BudgetCheck` return value that the
runtime inspects.

Cross-link: [reference/runtime/events.md](../runtime/events.md)

## Per-call accumulation

Counters update after each LLM call completes successfully:

1. Token usage (`input_tokens + output_tokens`) is added to the per-agent and
   per-chain accumulators.
2. USD cost is estimated via LiteLLM pricing and added to the USD accumulators.
3. A record is appended to `.reyn/state/budget_ledger.jsonl` (fsync'd for
   durability) for the daily / monthly dimensions.
4. The updated counters are checked against warn thresholds; any newly crossed
   threshold emits a warning outbox message (once per dimension per session).

Pre-call checks run before the call: if a hard cap is already exceeded, the
call is refused at that point — no tokens are consumed.

## Ledger file

Daily and monthly counters persist across process restarts via
`.reyn/state/budget_ledger.jsonl`. One record per LLM call:

```json
{"ts": "2026-05-09T10:23:00+09:00", "agent": "alice", "model": "openai/gpt-4o", "tokens": 312, "cost_usd": 0.00234}
```

Records are fsync'd on append. On startup, Reyn re-aggregates today's and
this month's totals from the ledger. The file is append-only and grows at
roughly a few MB per month; it can be manually archived if needed (stop the
process first, or wait for the period rollover).

## What is not yet implemented

Be aware of the following limitations:

- **Persistent per-agent / per-chain counters across restarts** — `per_agent_tokens`,
  `per_agent_cost_usd`, `per_chain_skill_calls`, and `per_chain_skill_tokens` are
  in-memory only. A process restart or `/budget reset` zeroes them out. Only the
  daily / monthly quotas survive restarts via the ledger.
- **Auto-throttle** — when a rate limit is hit, Reyn refuses the call rather than
  sleeping until the window opens. The caller must retry.
- **Cross-process / multi-tenant budgets** — each `reyn chat` or `reyn web`
  process maintains its own in-memory counters. If multiple processes share one
  project, the ledger aggregates correctly for daily / monthly quotas, but
  in-memory caps (per-agent, per-chain) are enforced independently per process.

## See also

- [reference/runtime/events.md](../runtime/events.md) — full event catalog
- [reference/cli/chat.md](../cli/chat.md) — `/cost`, `/budget`, and other slash commands
- [reference/config/reyn-yaml.md](reyn-yaml.md) — top-level config schema; the `cost:` block is documented there as part of the full key reference
