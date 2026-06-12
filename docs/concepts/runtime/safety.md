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
| Router iterations | `safety.loop.max_router_iterations` | 5 | ✅ | partial |
| Plan step iterations | `plan.step_max_iterations` | 5 | ✅ | partial |
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
  │
  ├─ mode=auto_extend ──► within budget  → allow=True,  reason="auto_extended"
  │                       budget exhausted → allow=False, reason="unattended"
  │
  └─ mode=interactive
        ├─ bus=None   ──► allow=False, reason="no_bus"
        └─ bus present ──► UserIntervention dispatched
              ├─ yes  ──► allow=True,  reason="user_approved"
              └─ no   ──► allow=False, reason="user_refused"

every allow=False path ──► force-close wrap-up (#1496)
      emit `limit_denied` event (kind = max_iterations | router_cap)
      → one final tool-less LLM turn summarizing what was accomplished
          ├─ wrap-up has text ──► outbox kind="agent",
          │                        meta.limit_stopped=True, meta.limit_kind=<kind>
          └─ wrap-up fails/empty ──► decision-enabling outbox error (fallback)
```

**A2A peer sessions.** A2A sessions use the same `on_limit` config as CLI
sessions (default: `interactive`). When a limit fires in `interactive` mode,
the intervention is surfaced to the A2A peer via `A2AInterventionBus`:
the run's status is mirrored to `"input-required"` and the payload is appended
to the SSE stream / POSTed to the webhook. The peer answers via the A2A answer
endpoint (`POST /a2a/agents/<name>` `{task_id, answer}`), which resolves the
iv and allows the loop to continue. If a caller wants bounded behaviour instead
of waiting indefinitely for a peer answer, set `safety.on_limit.ask_timeout_seconds`
to a finite value (e.g. `ask_timeout_seconds: 60.0`) — a timeout refusal produces
the same decision-enabling error as a "no" answer.

**Force-close wrap-up on deny (#1496).** A denied limit no longer goes
straight to a canned error. The OS first emits a `limit_denied` event
(audit truth, P6) and gives the LLM one final **tool-less** turn to
summarize what was accomplished before the turn ends. The stop cause is
injected into that wrap-up's system prompt (the steady-state SP stays
cause-neutral; the cause is not appended as a trailing user message
because some providers reject a user turn immediately after a
`tool_result`). When the wrap-up produces text it is delivered as an
ordinary `kind="agent"` outbox message carrying a structured
`meta.limit_stopped=True` + `meta.limit_kind` marker — the UI reads the
marker to indicate a forced stop without a competing prose block. For
phase/plan hosts the wrap-up is also handed back for checkpoint
persistence (`record_force_close`); chat hosts no-op that hook.

**Decision-enabling error message contract** — emitted only on the
**fallback** path (the wrap-up call raised or produced no text). All
`allow=False` paths still degrade to a message containing:
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
    max_router_iterations: 5   # max LLM tool-call iterations per user turn (CLI --max-iterations overrides)
    plan_invalid_retries: 1
  timeout:
    phase_seconds: 0.0         # 0 = disabled
    chain_seconds: 60.0
    llm_call_seconds: 60.0

plan:
  step_max_iterations: 5       # max RouterLoop iterations per plan step
```
