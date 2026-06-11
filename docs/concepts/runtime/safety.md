# Safety framework — limits, modes, and intervention flow

Reyn's safety framework bounds how long an agent can run, how deep it can
recurse, and how many times it can loop before the system stops it. All
bounded operations share a single checkpoint API (`handle_limit_exceeded`)
so operators configure behaviour once and it applies uniformly.

Design principle: **no limit hard-stops without either an ask (interactive)
or a clean partial/degrade** — every checkpoint either asks the operator for
permission to continue, auto-extends within a configured budget, or stops
with a decision-enabling message that explains what to change.

---

## Modes (`safety.on_limit.mode`)

| mode | behaviour on limit |
|---|---|
| `interactive` (default) | Dispatch a yes/no question to the operator via the intervention bus. Allow on yes; abort on no / timeout. |
| `auto_extend` | Automatically extend up to `safety.on_limit.auto_extend_times` times per `(run_id, limit_kind)`, then abort. |
| `unattended` | Abort immediately. Never ask. |

When no intervention bus is available (headless / non-TTY run), `interactive`
degrades to the same abort path as `unattended`. In all abort paths the
outbox error message is **decision-enabling**: it states what limit was hit,
the current configured value, and which config key to change.

---

## Limit inventory

| limit | config path | default | checkpointed? | partial data? |
|---|---|---|---|---|
| Phase act-turns | `safety.loop.max_act_turns_per_phase` | 10 | ✅ | yes |
| Phase visits | `safety.loop.max_phase_visits` | 25 | ✅ | yes |
| Router calls/turn | `safety.loop.max_router_calls_per_turn` | 3 | ✅ | yes |
| Skill calls/chain | _(configured per skill)_ | — | ✅ | yes |
| Agent hops | `safety.loop.max_agent_hops` | 3 | ✅ | yes |
| Phase wall-clock | `safety.timeout.phase_seconds` | 0 (off) | ✅ | yes |
| Chain wait | `safety.timeout.chain_seconds` | 60 | ✅ | yes |
| Router iterations | `router_max_iterations` _(ChatSession param)_ | 5 / 80 | ❌ → ✅ _(pending)_ | partial |
| Plan step iterations | `plan.step_max_iterations` | 5 | ❌ → ✅ _(pending)_ | partial |
| LLM call timeout | `safety.timeout.llm_call_seconds` | 60 | ❌ auto-retry/abort | — |
| Media cap | `multimodal.max_bytes` | 5 MB | ❌ auto-degrade | — |
| Summary body cap | `chat.compaction.body_token_cap` | 1500 | ❌ auto-truncate | — |

Rows marked ✅ flow through `handle_limit_exceeded`.
Rows marked ❌ have autonomous behaviour that does not require operator input.

---

## Intervention flow

```
limit hit
  │
  ├─ mode=unattended  ──► allow=False, reason="unattended"
  │                       caller → decision-enabling outbox error
  │
  ├─ mode=auto_extend ──► within budget  → allow=True,  reason="auto_extended"
  │                       budget exhausted → allow=False, reason="unattended"
  │
  └─ mode=interactive
        ├─ bus=None   ──► allow=False, reason="no_bus"
        │                 caller → decision-enabling outbox error (not silent)
        └─ bus present ──► UserIntervention dispatched
              ├─ yes  ──► allow=True,  reason="user_approved"
              └─ no   ──► allow=False, reason="user_refused"
```

**Decision-enabling error message contract** (all `allow=False` paths):
1. What limit was hit and its current configured value
2. The config key to increase, or the `safety.on_limit.mode` to set for
   interactive or auto-extend behaviour
3. Whether partial results are available

---

## Config reference (`reyn.yaml`)

```yaml
safety:
  on_limit:
    mode: interactive          # interactive | auto_extend | unattended
    auto_extend_times: 1       # extensions granted per (run_id, limit_kind) in auto_extend mode
    ask_timeout_seconds: 0.0   # 0 = wait forever; >0 = timeout then refuse
  loop:
    max_act_turns_per_phase: 10
    max_phase_visits: 25
    max_router_calls_per_turn: 3
    max_agent_hops: 3
    plan_invalid_retries: 1
  timeout:
    phase_seconds: 0.0         # 0 = disabled
    chain_seconds: 60.0
    llm_call_seconds: 60.0

# Router iterations — ChatSession constructor parameter (default 5 interactive / 80 run-once).
# Not a safety.loop key; configured per deployment context.

plan:
  step_max_iterations: 5       # max RouterLoop iterations per plan step
```
