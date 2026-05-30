---
type: how-to
topic: operations
audience: [human]
---

# Understand why Reyn stops

When Reyn aborts a run mid-flight, it does so for **one of three reasons**:

1. **Loop detection** — the agent is doing the same thing over and over.
2. **Timeout** — something is taking too long.
3. **Budget exceeded** — token / USD spend hit a configured cap.

Each category has its own configuration namespace and its own "raise this
key to allow more" hint embedded in the error message. This page maps
the failure modes to the knobs.

> **TL;DR:** the unified namespace is `safety.*` for loop / timeout
> conditions, and `cost.*` for financial caps.

---

## ① Loop detection — `safety.loop.*`

Loop limits catch *runaway repetition*: a phase that re-enters itself
forever, a router that keeps re-routing, a delegation chain that grows
without bound. Hitting one is normal during exploratory development.
Raise the cap when the workload genuinely needs more iterations;
investigate when it should not.

| Limit | What it catches | Default | Config key |
|---|---|---|---|
| Phase visits | One phase entered too many times in one skill run | 25 | `safety.loop.max_phase_visits` |
| Act turns per phase | LLM ↔ op volleys inside one phase visit | 10 | `safety.loop.max_act_turns_per_phase` (skill / phase frontmatter wins) |
| Router calls per turn | Chat router invoked too many times per user turn | 3 | `safety.loop.max_router_calls_per_turn` (0 = unlimited) |
| Agent delegation depth | `user → A → B → C` chain too deep | 3 | `safety.loop.max_agent_hops` |
| Skill spawns per chain | Same skill spawned too many times in one chain | unlimited | `safety.loop.skill_calls_per_chain.hard_limit` |
| Skill tokens per chain | Same skill consumed too many tokens in one chain | unlimited | `safety.loop.skill_tokens_per_chain.hard_limit` |

### Example error

```
Phase 'revise' reached max_phase_visits=25.
→ Raise safety.loop.max_phase_visits to allow more iterations.
```

### Example fix

```yaml
# reyn.local.yaml
safety:
  loop:
    max_phase_visits: 50      # allow up to 50 visits per phase
    max_router_calls_per_turn: 5
```

---

## ② Timeout — `safety.timeout.*`

Timeout limits catch *things taking too long*: a slow LLM call, a stuck
delegation, a phase that's been running for an hour. Raise the cap when
the workload legitimately needs longer; investigate when it should not.

| Limit | What it catches | Default | Config key |
|---|---|---|---|
| LLM call | One litellm.acompletion exceeded the timeout | 60s | `safety.timeout.llm_call_seconds` |
| LLM retries | Transient-error retry budget per call | 3 | `safety.timeout.llm_max_retries` |
| Phase wall-clock | One phase visit ran past its budget | unlimited (`0`) | `safety.timeout.phase_seconds` |
| Chain wait | Multi-agent pending chain waited too long for a delegate reply | 60s | `safety.timeout.chain_seconds` (0 = no timeout) |

### Example error

```
chain timeout: 1 delegate(s) (writer) did not respond within 60s.
→ Raise safety.timeout.chain_seconds to wait longer (0 = no timeout).
```

### Example fix

```yaml
# reyn.local.yaml
safety:
  timeout:
    llm_call_seconds: 120     # let slow models finish
    chain_seconds: 300        # let long-running delegates reply
```

---

## ③ Budget exceeded — `cost.*`

Budget limits are **financial caps** (token count, USD spend, daily /
monthly quota). They are intentionally kept under `cost:` rather than
`safety:` because the operator's mental model is different: a loop /
timeout should usually be raised when hit; a budget should usually
trigger an investigation or an explicit user approval.

| Limit | What it catches | Config key |
|---|---|---|
| Per-agent tokens | One agent hit its token cap | `cost.per_agent_tokens.hard_limit` |
| Per-agent USD | One agent hit its USD cap | `cost.per_agent_cost_usd.hard_limit` |
| Daily quota | All work today exceeded `daily_tokens` / `daily_cost_usd` | `cost.daily_tokens.hard_limit`, `cost.daily_cost_usd.hard_limit` |
| Monthly quota | This month exceeded `monthly_tokens` / `monthly_cost_usd` | `cost.monthly_tokens.hard_limit`, `cost.monthly_cost_usd.hard_limit` |
| Rate limit | One model hit its requests-per-minute cap | `cost.rate_limit_per_minute.<model>` |

(Per-(chain, skill) call / token caps are loop-detection limits and
live under `safety.loop.skill_calls_per_chain` /
`safety.loop.skill_tokens_per_chain` — see §① above.)

### User-approval flow on hit

For per-(chain, skill) call caps, you can opt into an interactive
approval flow instead of a hard refusal:

```yaml
# reyn.local.yaml
safety:
  loop:
    skill_calls_per_chain:
      hard_limit: 5
      ask_on_exceed: true       # prompt the user via ask_user
      extension_calls: 3        # +3 spawns granted on approval
```

When the cap is hit, Reyn asks: *"Skill `X` has reached its cap of 5
spawns. Allow 3 more?"* — the user can approve repeatedly; each
approval extends the cap further.

---

## What happens on a limit hit (`safety.on_limit`)

By default, a limit hit prompts the user via `ask_user` to extend the
limit (= `mode: interactive`, `ask_timeout_seconds: 0` — wait forever).
Refusal / timeout / no intervention surface aborts the run with a
`RunResult` whose `status` is one of `loop_limit_exceeded` /
`phase_budget_exceeded` / `budget_exceeded`, and `partial_data`
populated with the last completed phase artifact — *"here's what we
have so far"*. Headless paths (`bus=None`, non-TTY stdin) short-circuit
to that abort path cleanly — `interactive` is safe everywhere.

You can change this with `safety.on_limit.mode`:

```yaml
# reyn.local.yaml
safety:
  on_limit:
    mode: interactive      # default — prompt the user via ask_user; on approval extend the limit
    # mode: unattended     # abort on hit (= opt-in for CI / cron / scripted runs that cannot pause)
    # mode: auto_extend    # auto-extend N times, then abort
    auto_extend_times: 1   # only consulted when mode == auto_extend
    ask_timeout_seconds: 0  # only consulted when mode == interactive; 0 = wait forever
```

| Mode | Use case |
|---|---|
| `interactive` (default) | `reyn chat`, TUI / a2a sessions — the user is reachable and can decide whether to extend |
| `unattended` | CI / cron / scripted invocations that genuinely cannot pause for a human; opt-in to skip the prompt and fail fast |
| `auto_extend` | Trusted long-running tasks where the operator knows up front that N extensions are acceptable |

**Where each mode is wired:**

| Limit | Site | Mode behaviour |
|---|---|---|
| `safety.loop.max_phase_visits` | `OSRuntime._enter_phase` | interactive / auto_extend |
| `safety.timeout.phase_seconds` | `OSRuntime._check_phase_budget` | interactive / auto_extend |
| `safety.loop.max_act_turns_per_phase` | OSRuntime act-loop | interactive / auto_extend |
| `safety.loop.max_router_calls_per_turn` | `ChatSession._check_and_increment_router_cap` | interactive / auto_extend |
| `safety.loop.max_agent_hops` | `ChatSession._send_to_agent` | interactive / auto_extend |
| `safety.timeout.chain_seconds` | chain timeout watchdog | interactive / auto_extend (re-arm) |
| `safety.loop.skill_calls_per_chain` | spawn budget gate | interactive (= `ask_on_exceed`) |

`safety.timeout.llm_call_seconds` is excluded by design — litellm
already auto-retries within `safety.timeout.llm_max_retries`, so an
extra `ask_user` layer would just add latency.
