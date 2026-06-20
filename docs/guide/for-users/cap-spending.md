---
type: how-to
topic: using-reyn
audience: [human]
---

# Cap your spending

By default Reyn runs without any spending limit — it keeps calling the LLM
until your task is done. If you want a hard ceiling on tokens or dollars,
add a `cost:` block to `reyn.yaml`. Caps are checked *before* each LLM call,
so a refused call costs you nothing.

## Set a daily dollar cap

The most common goal — "never spend more than a few dollars a day":

```yaml
# reyn.yaml
cost:
  daily_cost_usd:
    hard_limit: 5.00     # refuse the next call once today's spend hits $5
    warn_ratio: 0.8      # warn once at 80% ($4.00)
```

Daily and monthly caps are **persistent** — they survive restarts and reset
automatically at local midnight (daily) or the 1st of the month (monthly).
They are stored in `.reyn/state/budget_ledger.jsonl`.

## Cap a single agent or session

Per-agent caps are **in-memory** — they reset when you restart `reyn chat` or
run `/budget reset`. Use them to bound one conversation rather than your whole
day:

```yaml
cost:
  per_agent_tokens:
    hard_limit: 50000
  per_agent_cost_usd:
    hard_limit: 2.00
```

## Check where you stand

While `reyn chat` is running:

| Command | Shows |
|---------|-------|
| `/cost` | One-line spend for the attached agent this session |
| `/budget` | Full breakdown — today, this month, per-agent, rate limits |
| `/budget reset` | Clear the in-memory per-agent counters (daily/monthly are untouched) |

## What happens when you hit a cap

- **At the warn threshold** (`hard_limit × warn_ratio`): you get a one-time
  `[budget warn]` message and the call proceeds.
- **At the hard limit**: the next call is refused before it runs. You'll see
  `[budget exceeded]` with the triggered dimension and your options — raise the
  limit in `reyn.yaml`, run `/budget reset`, or restart.

## Notes

- Without a `cost:` block, runs are unlimited — the whole framework is opt-in.
- USD figures are estimated from [LiteLLM's pricing data](https://github.com/BerriAI/litellm).
  If a model has no price entry the dollar counter stays at `$0.00` but token
  counts are always accurate — so token caps work even when pricing is unknown.
- A rate limit (`rate_limit_per_minute`) refuses calls until the 60-second
  window clears; Reyn does not auto-sleep, so the caller retries.

## See also

- [Reference: Budget and cost tracking](../../reference/config/budget.md) — full schema, every field, the ledger format, and the `/cost` / `/budget` command output
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — the complete config file
- [Understand why Reyn stops](../for-skill-authors/operations/understand-why-reyn-stops.md) — limits and budgets share one stop framework
