# Proposal 0067: task model and the inbox arbiter — sequencing and interface

**Status**: Accepted (2026-08-10, owner) — not yet implemented.
**Decisions**: [ADR-0040](../decisions/0040-task-as-os-concept.md) records *why*; this document
records *what to build and in what order*.
**Tracking**: #3978 (live record: retractions, measurements, per-step progress).

## Scope

Unify reyn's four asynchronous mechanisms behind one concept (`task`), move the reply-routing
approximations (`_last_sender` / `_last_reply_to`) onto a present-tense `current_task`, and
extract the inbox arbiter out of `session.py`.

Out of scope: renaming `dispatch_kind` (misleading but correct in behaviour, ADR-0040
Consequences); the `spawn_*` word-order flip and the `create` verb (**#4004**, independent);
the stale hook-point count (**#3996**).

## Interface

### Summary

| tool | arguments | returns | verb source | `dispatch_kind` |
|---|---|---|---|---|
| `run_prompt` | `agent`, `session`, `prompt`, `collect`, `schema?`, `on_settle`, `ttl_seconds?` | attached → `{result}` / async → `{task_id, status}` | lexicon `run` | `sync` |
| `describe_task` | `task_id` | `{task_id, kind, status, session, requester, ttl_seconds}` — running only | lexicon `describe` (R3 metadata) | `sync` |
| `list_tasks` | `kind?` | `[{task_id, kind, status, session}]` — running only | lexicon `list` | `sync` |
| `cancel_task` | `task_id` | `{task_id, status}` | **`cancel`, added to the lexicon** | `sync` |
| `send_to_session` | `agent`, `session`, `text`, `wake=False` | `{}` | pairs with existing `send_to_agent` | `sync` |
| `run_pipeline` | `pipeline`, `collect`, `inline`, `on_settle`, … | as `run_prompt` | lexicon `run` | `sync` |
| ~~`delegate_to_agent`~~ | — | — | — | retired |
| ~~`run_pipeline_{async,inline,inline_async}`~~ | — | — | — | retired (folded into `collect` / `inline`) |

```
new tools   5      4 task-facing + 1 delivery
new verb    1      `cancel`   (`create` belongs to #4004)
retired     4      delegate_to_agent + three run_pipeline_* variants
```

Every tool is `dispatch_kind="sync"`. Only the retiring `delegate_to_agent` was `"async"`, which
does not mean "asynchronous" — it commands the router loop to end the turn
(`router_loop.py:2249`). None of the new tools needs that: each returns immediately.

### Issuing

```python
run_prompt(
    agent: str,                    # sid is namespaced per agent (registry.py:591), so the
    session: str,                  # address is the pair — #2130's "(agent, sid) addressing"
    prompt: str,
    collect: "attached" | "async",
    schema: str | None = None,     # structured output: a SchemaRegistry *name*, same shape as
                                   # run_agent_step(schema=...). reyn builds response_format and
                                   # validates the parsed value.
    on_settle: str = "deliver",    # "deliver" | "<pipeline name>" | "drop"
                                   # async only; ignored for attached
    ttl_seconds: int | None = None,
)
```

The asymmetric return (`attached` → result, `async` → handle) follows the existing convention:
`pipeline_verbs.py:434,437` keeps `run_id` — "the completion-message handle" — on the async path
and drops it from the LLM-visible side of the sync path.

### Collection

Results arrive by **push at settle**; the handle is deleted at the same moment (ADR-0040 D4).
`describe_task` and `list_tasks` therefore answer only about *running* tasks — their purpose is
anomaly detection and progress checking, not retrieval. There is no `read_task_result`.

### Delivery, message vs task

```python
send_to_session(agent: str, session: str, text: str, wake: bool = False)
```

A message has no handle and no collection. `wake` is selectable here and **only** here: a task's
settle always wakes its issuer (ADR-0040 D5).

## Sequencing

```
P-1   #4004 — spawn word-order flip + the family-validity clause + `create` in the lexicon
      Independent of this arc. Landing it first means the new task tools arrive onto a
      consistent surface.
```

### Types and substrate (no behaviour change)

```
P0    current_task / inbox-item attributes
        requester : typed wrapper over (agent_name, session_id)   — #2130's primitive
        reply_to  : TransportRef | None                            — volatile; None after crash
        + kind / collect / on_settle / schema / ttl_seconds / wake

P1'   keep the task open while its issuer delegates
      Today dispatch_kind="async" ends the turn, MessageBus.request sees an empty inbox, judges
      quiescence and returns "I delegated" while the work continues.
      🔴 Strictly before P1 — deriving a destination from a current_task that can be empty
      relocates the inheritance bug instead of removing it.

P1    extract `InboxArbiter` (one module out of session.py)
      Today four sites: _drain_to_wake, _peek_mid_turn_injection, _handle_sender_attribution,
      and _run_turn_body's kind dispatch.
      The arbiter holds current_task; _last_sender and _last_reply_to fold into it.
```

### The gate, before the field that needs it

```
P2    `context_safe` + hook-push `include`
        declare the eight existing schemas' current fields context_safe: true
        gate applies to message interpolation only (render.py:166)
          — pipeline_launch input is a different session (a different permission boundary)
          — shell argv is already load-time rejected (0059 §223), unchanged
        push gains `include: [<field>]` — fenced and attributed, appended after the message,
        never interpolated into it
      🔴 Strictly before P3: build the gate before adding the first field that needs it.
```

### The task mechanism

```
P3    builtin hook point `task_settled` + the settle path
        payload = {point, task_id, kind, status, session, result}   (result: context_safe false)
        disposition comes from the task's own on_settle, not from hook configuration
        task_id deleted at settle — no retention, no clock
      ⚠️ ALLOWED_HOOK_POINTS is eight today, not the ten several comments claim (#3996)

P4    task ops: run_prompt / describe_task / list_tasks / cancel_task
        `cancel` joins CANONICAL_VERBS; REMOVAL_VERBS is untouched (separate frozensets)
        cancellation machinery already exists for all three kinds — this exposes it
      ⚠️ Same PR: migrate `[task_completed] kind=` values from agent|spawned_session to
         prompt|pipeline|exec, plus the system prompt's TASK_SPAWNED rule and
         descriptions/catalog.py

P5    send_to_session

P6    retire delegate_to_agent (its own PR; ~19 src / ~45 tests / ~51 docs files mention it,
      measured at 27da0d6b2 — a scope estimate that drifts, re-measure before starting)
      pending_chains is repurposed as P3/P4's collection substrate

P7    run_pipeline: four names → one (collect / inline as arguments)

P8    ttl expiry: reuse the chain-timeout shape, plus persist `arm_at`
      chain_manager.py:362 re-arms "a fresh timeout watchdog" on restore, so a crash currently
      extends the effective deadline
```

### Observation (explicitly not bounding)

```
P9    inbox depth on the monitoring read surface; an audit-event for a settle whose destination
      is gone
      ⚠️ Neither is a valve. `check_pre_llm` already bounds spend regardless of producer.
```

## Verification notes for implementers

Measured during design; each has bitten or would bite:

| | finding |
|---|---|
| `dispatch_kind` | means "end this turn", not "is asynchronous" (`router_loop.py:2249`). P1′ and P6 touch it directly. |
| `agent_locks` | the serialization lock is keyed by **agent name** while what it protects is a **session's** history. P0/P1 move identity to the session axis; this key is then inconsistent. |
| `_is_quiescent` | its docstring promises three conditions; the implementation checks `inbox.empty()` only (`message_bus.py:188` vs `:193`). |
| `RunEntry` | `interfaces/web/run_registry.py` is a fourth handle store already carrying status/result/error/webhook_url plus persistence. §7's type is its generalization; whether it moves to core is a layering decision. |
| `_last_reply_to` | inherited when a trigger carries no `reply_to` (`session.py:2731-2733`) — a live misdelivery path that P1 removes. |
| chain timers | re-armed *fresh* on restore, so a crash extends the deadline (P8). |
| hook points | eight, not ten (#3996). |

LLM-visible strings change in P-1, P4, P5, P6 and P7. Each of those PRs updates the system
prompt, `descriptions/catalog.py` and the affected docs **in the same PR** (CLAUDE.md's
doc-freshness rule).

## Open at design close

None. Twelve decisions were settled by the owner on 2026-08-09/10; three items remain
*unmeasured* rather than undecided, and are measurements to take during implementation:

- whether FP-0041's `[context shift]` survives compaction (summariser behaviour, not statically
  decidable)
- other consumers affected by adding a point to `ALLOWED_HOOK_POINTS`
- the MCP SDK v2 surface (same constraint as #3698)
