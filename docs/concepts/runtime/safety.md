# Safety framework — limits, modes, and intervention flow

> **Status**: Draft (safety framework wave) — implementation design under owner review.
> Describes the DESIRED state after the wave. Pre-wave state is in survey matrix
> `/tmp/187_n5/findings_safety_survey.md`.

## Overview

Reyn's safety framework bounds how long an agent can run, how deep it can
recurse, and how many times it can loop before the system stops it. All
bounded operations share a single checkpoint API (`handle_limit_exceeded`,
FP-0005) so operators configure behaviour once and it applies uniformly.

Goal: **no limit hard-stops without either an ask (interactive) or a clean
partial/degrade** — the "flat abort without context" anti-pattern is
structurally removed.

---

## Modes (`safety.on_limit.mode`)

| mode | behaviour on limit |
|---|---|
| `interactive` (default) | Dispatch a yes/no question to the user via the intervention bus. Allow on yes, abort on no / timeout. |
| `auto_extend` | Automatically extend up to `safety.on_limit.auto_extend_times` times per `(run_id, kind)`, then abort. |
| `unattended` | Abort immediately. Never ask. |

When no intervention bus is available (headless / non-TTY run-once), `interactive`
degrades to the same abort path as `unattended` — **but the error message must
be decision-enabling** (what limit, which config key to change, any partial result).

---

## Limit inventory

| limit | config path (current key) | default | checkpointed? | partial data? |
|---|---|---|---|---|
| Phase act-turns | `safety.loop.max_act_turns_per_phase` | 10 | ✅ FP-0005 | yes |
| Phase visits | `safety.loop.max_phase_visits` | 25 | ✅ FP-0005 | yes |
| Router calls/turn | `safety.loop.max_router_calls_per_turn` | 3 | ✅ FP-0005 | yes |
| Skill calls/chain | _(FP-0003)_ | — | ✅ FP-0005 | yes |
| Agent hops | `safety.loop.max_agent_hops` | 3 | ✅ FP-0005 | yes |
| Phase wall-clock | `safety.timeout.phase_seconds` | 0 (off) | ✅ FP-0005 | yes |
| Chain wait | `safety.timeout.chain_seconds` | 60 | ✅ FP-0005 | yes |
| **Router iterations** | `router_max_iterations` (ChatSession param) | 5 / 80 | ❌ → ✅ *(wave)* | partial |
| **Plan step iterations** | `plan.step_max_iterations` | 5 | ❌ → ✅ *(wave)* | partial |
| LLM call timeout | `safety.timeout.llm_call_seconds` | 60 | ❌ auto-retry | — |
| Media cap | `safety.media.max_bytes` | 5 MB | ❌ auto-degrade | — |
| Summary body cap | `summary.body_token_cap` | 1500 | ❌ auto-truncate | — |

Rows marked ✅ flow through `handle_limit_exceeded`; ❌ rows have autonomous behaviour.

---

## Intervention flow

```
limit hit
  │
  ├─ mode=unattended  ──► allow=False, reason="unattended"
  │                       caller → decision-enabling outbox error
  │
  ├─ mode=auto_extend ──► within budget → allow=True, reason="auto_extended"
  │                       budget exhausted → allow=False, reason="unattended"
  │
  └─ mode=interactive
        ├─ bus=None   ──► allow=False, reason="no_bus"
        │                 caller → decision-enabling outbox error (NOT silent)
        └─ bus present ──► UserIntervention dispatched
              ├─ yes  ──► allow=True,  reason="user_approved"
              └─ no   ──► allow=False, reason="user_refused"
```

**Decision-enabling error message contract** (every `allow=False` path):
- What limit was hit and its current configured value
- The config key to increase, or the mode to set for interactive extension
- Whether partial results are available and where to find them

---

## Config reference (`reyn.yaml` — current structure, unchanged by this wave)

```yaml
safety:
  on_limit:
    mode: interactive          # interactive | auto_extend | unattended
    auto_extend_times: 1       # extensions granted in auto_extend mode
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

# Router iterations — currently a ChatSession constructor param, not a safety.loop key.
# Naming-unification to safety.loop.max_iterations is a FUTURE PR (see below).
# router_max_iterations: 80  (set via ChatSession / run-once config, not reyn.yaml safety section)

plan:
  step_max_iterations: 5       # max RouterLoop iterations per plan step
```

---

## FP-0004 / FP-0005 status

- **FP-0004** (permission model): stable. Sandbox/file permissions use
  `PermissionDeniedError` / `PermissionResolver` — independent of `safety.on_limit`.
  *Status:* No changes in this wave.

- **FP-0005** (unified limit handler): core API stable (`handle_limit_exceeded`,
  `LimitDecision`). The `max_iterations` / `step_max_iterations` bypass gap closed
  in this wave. *Status after wave:* all checkpoint sites listed above are wired.

---

## Implementation design (this wave)

### Fix A: Wire bypass cluster

**`router_loop.py`** — `run_loop()` currently flat-aborts when
`for _iteration in range(self.max_iterations):` exhausts. Fix wraps the
inner `for` loop in an outer `while True:` and, after natural exhaustion,
calls `handle_limit_exceeded(kind="max_iterations", ...)`. On
`allow_continue=True`: extends `self.max_iterations` and re-runs. On
refusal: emits decision-enabling error.

Requires adding to `RouterLoop.__init__`:
- `on_limit: OnLimitConfig | None = None` (passed from all call sites)

Requires adding to `RouterHostAdapter` (and `RouterLoopHost` protocol):
- `make_intervention_bus() → RequestBus | None` — returns
  `self._intervention_bus_factory()` if wired, else `None`

**`planner.py`** — pass `on_limit=on_limit` to the `RouterLoop` constructor
for plan steps. The planner already accepts `on_limit`; it just wasn't
threaded into the step sub-loop.

### Fix B: Decision-enabling error messages

All `allow_continue=False` paths in `run_loop` (and symmetric plan-step path)
replace the bare error string with a structured message including:
the limit kind, current value, config key to adjust, and `reason` from
`LimitDecision`.

### Owner-veto decision points (listed for morning review)

1. **Extension amount for `max_iterations`**: pass `extension_amount=float(self.max_iterations)`
   (doubles on each extension) OR `extension_amount=1.0` (one iteration at a time, consistent
   with router_cap). Recommendation: `float(self.max_iterations)` (meaningful step; one-at-a-time
   for 80-iteration loops is too granular for interactive ask).

2. **Naming unification timing**: `router_max_iterations` is not in `safety.loop` today.
   This wave leaves it as-is (just wires it). A follow-up PR moves it to
   `safety.loop.max_iterations` with deprecated-alias migration (FP-0004 pattern). Acceptable?

3. **`step_max_iterations` extension amount**: same question as (1). Recommendation: `float(step_max_iterations)`.

4. **Gate verification method**: live interactive-ask verification via a unit test
   with `ChatInterventionBus` fake + triggering `max_iterations` exhaustion. The
   survey's deliverable-2 live-TTY gap is closed by a Tier-2 test with a real
   `RequestBus` fake (not a mock). Acceptable in lieu of TTY?
