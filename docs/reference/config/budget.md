---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# Budget and cost tracking

## Overview

Reyn tracks LLM token usage and USD cost per session, per-agent, and
per-model. Token and USD totals accumulate as LLM calls complete;
configured caps refuse or warn before any call (or spawn) that would exceed
them. The system is entirely opt-in: without a `cost:` block in `reyn.yaml`,
runs are unlimited.

## `reyn.yaml` schema

All budget configuration lives under the top-level `cost:` key. Every field
is optional; omitting a sub-key (or setting its `hard_limit` to `null`) means
unlimited for that dimension.

```yaml
cost:
  # Per-agent caps — ledger-backed (survive restart + crash); cleared in
  # memory by /budget reset
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

> **Migration note**: `router_invocations_per_turn` was moved from `cost:` to `safety.loop`. Use `safety.loop.max_router_calls_per_turn` instead. See [Reference: `reyn.yaml` — `safety` block](reyn-yaml.md#safety-block).

### Field reference

| Field | Scope | Persists | Resets |
|---|---|---|---|
| `per_agent_tokens` | per agent | ledger file | `/budget reset` |
| `per_agent_cost_usd` | per agent | ledger file | `/budget reset` |
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

Reports the per-agent counters for this agent. These are restored from the
ledger on startup (so they accumulate across restarts) and cleared in memory
by `/budget reset`. Returns nothing when no `cost:` block is configured
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
    chain-abc/direct_llm:  2 / 5

  Rate limit (last minute):
    openai/gpt-4o:  14 / 60  (warn at 48)

  Reset counters with `/budget reset`.
```

The "Today / Month" section appears only when `daily_*` or `monthly_*` caps
are configured and at least one LLM call has been made since startup.

### `/budget reset`

Clear the in-memory per-agent counters:

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

1. Token usage (`input_tokens + output_tokens`) is added to the per-agent
   accumulators.
2. USD cost is estimated via LiteLLM pricing and added to the USD accumulators.
3. A record is appended to `.reyn/state/budget_ledger.jsonl` (fsync'd for
   durability). The daily / monthly / per-agent counters are reconstructed
   from these records on the next startup.
4. The updated counters are checked against warn thresholds; any newly crossed
   threshold emits a warning outbox message (once per dimension per session).

Pre-call checks run before the call: if a hard cap is already exceeded, the
call is refused at that point — no tokens are consumed.

## Ledger file

Budget counters persist across process restarts — and crashes — via the
fsync-per-append `.reyn/state/budget_ledger.jsonl`. One record per LLM call:

```json
{"ts": "2026-05-09T10:23:00+09:00", "agent": "alice", "model": "openai/gpt-4o", "tokens": 312, "cost_usd": 0.00234}
```

Legacy note: a pre-existing ledger may also contain skill-spawn records
(`{"kind": "spawn", ...}`) written before the per-chain skill-spawn cap was
removed. They are no longer written; hydrate skips them on read.

Records are fsync'd on append. On startup, Reyn re-aggregates from the ledger:
today's and this month's daily / monthly totals (period-filtered), and the
cumulative per-agent token + USD totals. The ledger is the cap-critical
source of truth; `.reyn/state/budget_state.json` is a throttled best-effort
cache layered on top (it can lag the ledger by up to a second, so the ledger
value always wins on recovery). The ledger is append-only and grows at
roughly a few MB per month; it can be manually archived if needed (stop the
process first, or wait for the period rollover).

**Startup is bounded, not a lifetime re-parse (#2945).** Because the ledger is
never rotated, a naive "re-parse the whole ledger on every startup" hydrate
would grow slower forever as the ledger grows. Instead `hydrate` reads a
compacted checkpoint (`.reyn/cache/budget_checkpoint.json` — a per-agent
lifetime-total summary anchored to an exact byte position in the ledger) and
only re-parses the ledger *tail* written after that anchor. The checkpoint is
refreshed automatically (same throttle as `budget_state.json`) and is always
safe to delete: it holds no fact the ledger doesn't already durably hold, so
if it is missing, corrupted, or tampered, `hydrate` transparently falls back
to a full ledger re-scan (slower, but the cap can never silently
under-count). Daily / monthly totals are not checkpointed — they self-heal
at their period boundary, so only the lifetime per-agent aggregate needed
the compaction.

**Only an explicit operator action may lower a per-agent cap counter.**
Every implicit path that cannot AFFIRMATIVELY prove the ledger is a
different one from the checkpoint's — truncated, deleted entirely, or an
identity check that could not be established on one or both sides (e.g. a
checkpoint written before this ledger-identity mechanism existed) — is
non-decreasing: `hydrate` never simply discards the checkpoint and re-scans
whatever ledger remains. Instead the checkpoint's per-agent totals are
merged in as a **floor** (never lower than what the checkpoint recorded) on
top of the re-scan. The floor is the *default*; the ONE exception is a
ledger whose **identity** — a hash of its leading (first) record line,
stable across truncation/growth because the ledger is append-only and never
rotated — is AFFIRMATIVELY computable on both the checkpoint and the current
ledger AND differs: that is a genuinely different ledger (cross-workspace
copy, deliberate archive+recreate), and its past totals are simply not this
checkpoint's business, so no floor applies. Identity, not file size, is the
discriminator: an earlier version of this mechanism classified "same size or
larger with a content mismatch" as a replacement, but size is
attacker-controllable (a replacement can be padded to any length) — identity
lets the truncated-same-ledger case floor correctly even when it happens to
land at or above the old anchor's size. The governing question is still not
"over-count vs under-count" but which operation is allowed to lower the
counter at all: over-count is observable and has an explicit remedy (archive
both files, see below); under-count is silent and has none. The one explicit
remedy is archiving **both** `budget_ledger.jsonl` **and**
`budget_checkpoint.json` together (see below).

**A floor firing is never silent.** `/budget` shows a `⚠` line naming the
reason (`truncated` / `missing` / `identity_absent`) whenever the most
recent startup had to preserve a per-agent total this way, so an operator
seeing a higher-than-expected number can tell it was deliberately preserved
rather than wondering if it's a bug. (No `⚠` line means the ledger's
identity was affirmatively proven different — an intentional replacement —
and no floor was applied.)

See the `.reyn/cache/budget_checkpoint.json` entry in
[reference/runtime/reyn-dir-layout.md](../runtime/reyn-dir-layout.md) for
the full anchor-classification rationale.

## Per-agent cap recovery semantics

`per_agent_tokens` and `per_agent_cost_usd` are **lifetime/persistent** — they
are reconstructed from the all-time durable ledger on every startup and survive
crash and restart unchanged.

**They do not reset per-conversation.** The counters accumulate continuously
and are only cleared explicitly by `/budget reset` (in-memory clear) or by
archiving **both** `.reyn/state/budget_ledger.jsonl` **and**
`.reyn/cache/budget_checkpoint.json` while the process is stopped (#2945:
archiving the ledger alone is no longer sufficient — the checkpoint's
per-agent totals survive as a floor precisely so an accidentally-truncated
ledger can never silently under-count; removing only the ledger is treated
the same way as a truncation, not as an intentional reset).

Contrast with daily / monthly caps, which auto-reset at their period boundary
(midnight or 1st of month, local time) regardless of process restarts or
crashes.

**Crash-recovery guarantee**: a crash cannot lower a per-agent cap counter
below its durable ledger value. On recovery, `load_state` (the
throttled best-effort cache) is merged with `hydrate` (the ledger) using
`max()` — so a stale or garbage-corrupted state file can never cause the cap
to under-count spending and permit an over-budget call. Rationale: crash
recovery must be complete; a crash that resets a lifetime cap would allow
unbounded over-spend in the window before a human notices.

## What is not yet implemented

Be aware of the following limitations:

- **Auto-throttle** — when a rate limit is hit, Reyn refuses the call rather than
  sleeping until the window opens. The caller must retry.
- **Cross-process / multi-tenant budgets** — each `reyn chat` or `reyn web`
  process maintains its own in-memory counters and only picks up another live
  process's ledger records on its next startup (hydrate). Concurrently running
  processes therefore enforce every cap independently in real time; the shared
  ledger reconciles daily / monthly / per-agent totals only when a process
  restarts.

## See also

- [reference/runtime/events.md](../runtime/events.md) — full event catalog
- [reference/cli/chat.md](../cli/chat.md) — `/cost`, `/budget`, and other slash commands
- [reference/config/reyn-yaml.md](reyn-yaml.md) — top-level config schema; the `cost:` block is documented there as part of the full key reference
