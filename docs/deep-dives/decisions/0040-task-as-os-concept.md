# ADR-0040: `task` as an OS-level concept — vocabulary, collection, and who authors state

**Status**: Accepted (2026-08-10, owner) — **not yet implemented**. Sequencing and IF live in
[proposal 0067](../proposals/0067-task-model-and-arbiter.md); tracking issue #3978.
**Track**: multi-agent / async orchestration — the unit of one asynchronous execution and its handle

## Context

reyn grew four unrelated asynchronous mechanisms, each with its own handle store:

| mechanism | handle | store |
|---|---|---|
| `run_pipeline_async` | `run_id` | work-order (`run_dir/invocation.json`) |
| `delegate_to_agent` | `chain_id` | `pending_chains` (session snapshot) |
| `session_spawn` | `sid` | `AgentRegistry` |
| A2A async run | `run_id` | `RunRegistry` (`interfaces/web/run_registry.py`) |

"List what is running" therefore requires a union over four stores. The fourth is the most
complete — `RunEntry` already carries `status` / `result` / `error` / `webhook_url` plus
persistence (#267 Gap 5) — but it lives in an interface layer and serves A2A only.

Two external protocols converged on the same concept while reyn was fragmenting:

- **MCP Tasks** (2026-07-28): `taskId`, `ttlMs`, states `working / input_required / completed /
  failed / cancelled`, `tasks/get`, cooperative `tasks/cancel`. `tasks/list` was **deleted**
  ("without sessions, listing all tasks isn't safe").
- **A2A**: `taskId` **plus `contextId`** — "groups multiple Task objects … collaboration
  towards a common goal … shared contextual session".

The owner's framing: *"MCP の task は単に非同期要求とその回収制御の単位"* and
*"a2a task → reyn task (agent/session) → mcp task と綺麗につながるのではないか"*.

reyn previously had a concept named `task` and removed it: #3214 (fix #2839) deleted the
**internal LLM task-decomposition system** — the `task__*` ops, `TaskWaker`, `src/reyn/task/*`,
`docs/concepts/runtime/tasks.md`, and the `task_start` / `task_end` hook points. **This ADR is
not that system returning.** The removed one had the *LLM* driving task state; D3 below forbids
exactly that. The name is reused; the responsibility placement is inverted.

## Considered alternatives

**A. Leave the four mechanisms separate, unify only naming.** Rejected: the observability cost
is the union-over-four-stores, and naming alone does not remove it. It also leaves
`dispatch_kind` (which actually means "end this turn", see Consequences) as the only
cross-mechanism declaration, which it is not.

**B. A task registry with a retention policy.** Explored at length and **rejected**. A stored
handle needs a lifetime, and every wall-clock answer is unpredictable to an LLM
(*"なぜ 24H みたいな時間依存になってしまうのか。llm にとって予測可能性がないよね"*). The
24-hour window `RunRegistry` uses exists because an A2A peer **is not a session** — the clock is
a proxy for an absent owner. Internal tasks have an owner, so they need no clock. See D4.

**C. Deliver results through hook configuration.** Rejected after the owner observed that it
would require a hook per task issuance (*"task 起動のたびに hook も作らなきゃいけなくなる"*).
hook-event is the **reactivity extension point**, not the delivery implementation. See D4.

**D. Bound the inbox with a valve.** Rejected. The premise ("an unbounded queue is a hole in the
cost/budget band") is false: `runtime/budget/budget.py:1270` `check_pre_llm` fires *before* the
LLM call and is producer-agnostic, so queue depth does not translate into unbounded spend. What
remains is an **observability** gap, not a bounding one.

**E. Offload results to a store and pass references.** Rejected (owner: *"offload 依存は避けたい"*).
Filtering large results is expressible as a pipeline attached to the settle path, which is a
capability reyn already has, rather than a new storage dependency.

## Decision

### D1. Three words, and `message` is not one of them

| word | meaning | instances |
|---|---|---|
| **definition** | *what* | `pipeline` / `prompt` / `argv` |
| **resource** | *where* | **session** (like an MCP connection) / subprocess |
| **task** | **one execution + a handle** | the unit of asynchrony |

`message` is **delivery only** — no execution, no handle — and is therefore **not** a degenerate
task. A2A's "Message or Task" split is likewise about whether a lifecycle exists, not about
whether an identifier is returned.

The confusion this resolves: *"pipeline はタスクです。session で動くタスクもタスクです"* is the
same type error as "a program is a process". `pipeline` names a **definition**; `task` names an
**execution**.

### D2. `kind` is the definition axis, and each kind's terminal is deterministic

```
task(kind="exec")     one argv       terminal = process exit
task(kind="pipeline") one pipeline   terminal = terminal step reached
task(kind="prompt")   one prompt     terminal = turn end
```

The resource follows from the definition (prompt/pipeline → session, exec → subprocess), so
`kind` needs one axis, not two. Because `kind="prompt"` terminates at **turn end** — a
structural fact, not a claim — `completed` is decidable without asking the model. No per-task
confidence flag is needed: confidence is a property of the kind, settled once.

A prompt task that ends without a reply is `completed` with an empty answer, not `failed`. reyn
already draws this line (`_no_reply_marker`).

### D3. reyn alone authors `status`; the LLM authors `result` / artifacts

```
LLM authors    result / artifacts       ← payload
reyn authors   status / terminal / TTL  ← state machine
how the LLM "tells" reyn something: by calling an op (an observable act)
```

There is deliberately **no verb through which a model declares its own state**. "I need input"
is expressed by calling `ask_user`; reyn observes that call and transitions to `input_required`.
If a model could write `status`, `completed` would become a claim, and a Composer `all` would
fire on a lie.

This applies to supervisor agents too (see the monitoring direction in #3978 §8b): a monitor may
`cancel_task` — a cooperative **act** — but may not write another task's status. No exception is
carved for privileged observers.

### D4. Collection is push-at-settle with immediate deletion; delivery is a task attribute

```
① at issue, the task carries its own disposition:
     on_settle = "deliver" (default) | "<pipeline name>" | "drop"
② on terminal, the disposition is executed
③ the task_id is deleted immediately — no registry retention, no clock
④ a `task_settled` hook-event fires separately, for observation / Composer / monitoring
```

This is what `delegate_to_agent` already does: `chain_manager.resolve()` **pops** the chain at
delivery. The retention problem was introduced by the store, not inherent to the concept.

`collect="attached"` creates nothing at all — the result returns inline, so there is nothing to
retain and `on_settle` is ignored.

**"deliver" requires no configuration** — this is the property that makes D4 different from the
rejected alternative C. `"<pipeline name>"` is how a large result is filtered before it reaches
the issuer's context, and it costs an argument rather than a hook definition.

Undeliverable results (the issuing session vanished) are **not** boxed. Measurement narrowed this
to one case: crash restores the issuer (`registry.py:1281` — a session holding
`inbox` / `pending_chains` / `outstanding_interventions` is a restore target), and an executor
disappearing is already handled by the existing vanish guard (`registry.py` ~2587, which scans
for a waiter and notifies it). What remains is an **ephemeral issuer that finished its one turn
and vanished** — nobody is waiting for that result. An audit-event beside `session_vanished`
records it; that is observation, not retention.

### D5. A task's settle wakes the issuer; only a `message` chooses

```
task settle        wake = true, fixed
send_to_session    wake: bool = False, selectable
```

The answer to something you asked for may interrupt you; something merely told to you can ride
along on your next turn. This also yields a symmetry: **issuing spends one turn on the target,
settling spends one turn on the issuer.**

### D6. The requester is `(agent, session)`, and the arbiter owns it

`session_id` is namespaced **per agent** (`registry.py:591` — "Scope is WITHIN one Agent"), so a
session identifier alone is ambiguous the moment an agent boundary is crossed. The address is the
pair — which is exactly what #2130 named ("First-class **(agent, sid)** addressing") and left
unbuilt: only the per-chain `origin_sid` patch landed, not the "coherent addressing primitive
rather than per-site patches" the issue proposed.

```
current_task.requester : typed wrapper over (agent_name, session_id)   — identity, persists
current_task.reply_to  : TransportRef | None                            — live delivery handle,
                                                                          dies with the process
```

`TransportRef` is explicitly documented as runtime-only ("refs are purely runtime objects — they
do NOT survive crash recovery … `AgentRef` may need persistence in a later wave"). Splitting
identity from delivery handle honours that rather than fighting it: after recovery a task
survives **with no destination**, which is truthful, instead of inventing one.

Holding `current_task` in the arbiter collapses three approximations into one present-tense fact:

```
_last_sender      free string, edge-triggered, populated by 3 of 10 producers  ─┐
_last_reply_to    "the last one" — inherited when a trigger carries no reply_to ─┼→ current_task
payload["sender"] free string                                                   ─┘
```

The `_last_reply_to` inheritance is a live misdelivery path: a trigger without `reply_to`
(`AGENT_RESPONSE` / `PIPELINE_RESULT` / `HOOK` / `CRON`) keeps the previous turn's destination
(`session.py:2731-2733`). Exclusion means the value is correct *during* a turn; the defect is
that a turn can end while its task has not (see D8's ordering note).

### D7. `context_safe`: carrying a value and rendering it are different permissions

`task_settled.result` is LLM-authored, and proposal 0059 §220 names cross-session LLM-authored
payload interpolated into a template as **session-to-session injection**. But 0059 §221 equally
says all payload fields may be used by matcher / EventPattern / Composer, because control
decisions do not enter LLM context. The gate is on **rendering**, not on **carrying**:

```
0059 §222  interpolation into a pushed message is allowed only for fields declared
           context_safe: true (default false)
```

`context_safe` was never implemented (0 occurrences in `src/` and `tests/`); today what keeps
untrusted text out is the *absence* of dangerous fields from the eight builtin schemas.
Declaring `result` would be the first such field, so **the gate is built before the field is
added**.

The gate's real subject is narrower than "may this content reach the context": `on_settle="deliver"`
puts the same bytes into the same session already, through `_fence_inbound` with attribution. What
the gate prevents is content entering **unfenced, as operator-authored prose**. Hence a hook push
may carry the result — appended, fenced and attributed — via a new `include:` field, while
template interpolation of it stays refused:

```yaml
push:
  message: "<operator-authored frame>"   # template; context_safe fields only
  include: [result]                      # fenced + attributed, appended after the message
  wake: false                            # a quiet, content-bearing collection
```

### D8. The design's body is the arbiter, not the task type

The task type is a bundle of inbox-item attributes; every question raised in design —
per-requester ordering, where a ride-along attaches, message vs task, foreground vs background,
exclusion, mid-turn injection — resolves at the same place: **what the arbiter selects next and
what it attaches where**. Today that logic is spread across four sites in `session.py`
(`_drain_to_wake`, `_peek_mid_turn_injection`, `_handle_sender_attribution`, and `_run_turn_body`'s
kind dispatch), which is why "where does this rule live" had no answer.

Ordering constraint, load-bearing: **a task must stay open while its issuer delegates** before
`current_task` becomes the source of the reply destination. `delegate_to_agent` is
`dispatch_kind="async"`, which ends the turn (`router_loop.py:2249`), so `MessageBus.request`
sees an empty inbox, judges quiescence, and returns "I delegated" while the real work continues.
Deriving a destination from a `current_task` that can be empty does not remove the inheritance
bug — it relocates it.

## Consequences

**Desirable**

- One handle surface replaces four stores; `list_tasks` is safe here precisely because reyn has
  sessions, which is the property MCP cited when deleting `tasks/list`.
- No retention policy and no clock: the predictability question the owner raised disappears
  rather than being answered.
- `run_prompt` and `run_pipeline` become the same shape, so D2's "kind is the definition axis"
  becomes visible in the tool surface itself.
- Cooperative cancellation becomes reachable from the model. The machinery already exists for all
  three kinds (`session.cancel_inflight`; the pipeline executor's step-boundary `cancel_check`
  leaving a resumable journal under a terminal `cancelled` marker; `_subprocess_io`'s
  terminate/kill) — only the op was missing.
- `context_safe` closes an ingress that is currently held shut by convention alone.

**Undesirable / accepted**

- **`cancel` is added to the canonical verb lexicon.** It is not a fifth removal verb — R2's four
  (`delete` / `drop` / `forget` / `uninstall`) all mean "a thing ceases to exist", while a
  cancelled task's record persists with `status="cancelled"`. The gate keeps `CANONICAL_VERBS`
  and `REMOVAL_VERBS` as separate frozensets, so R2 is untouched.
- **Four LLM-visible tools retire**: `delegate_to_agent`, and `run_pipeline_{async,inline,inline_async}`
  collapsing into `run_pipeline(collect=…, inline=…)`. Renames of LLM-visible strings require the
  system prompt and tool descriptions to move in the same PR.
- **`dispatch_kind` keeps a misleading name.** Measurement showed it does not declare asynchrony;
  it commands "end this turn". Two tools set it, and that is correct, not a lag. Reusing it as the
  task declaration point would silently end turns for pipeline launches. Renaming it is out of
  scope here.
- **A ninth builtin hook point** (`task_settled`) opens a closed vocabulary. Note the vocabulary
  is currently **eight**, not the ten that five source comments and one doc claim (#3996).
- **`poll` is demoted from "the floor of the contract"**. It was justified by the vanished-requester
  case, which D4 resolves by accepting the loss. `describe_task` / `list_tasks` survive for
  *running* tasks only — the owner's stated uses are anomaly detection and progress checking.

**Supersedes / extends**

- Extends **[ADR-0034](0034-a2a-task-lifecycle.md)** (A2A task lifecycle): 0034 made `task` an
  A2A-protocol surface backed by `RunRegistry`. This ADR makes it an OS-level concept, of which
  the A2A surface becomes one projection. 0034's own vocabulary (`RunStatus`, deliberately "NOT
  the 7-state Task-tree `TaskState`") is the model D2/D3 generalize.
- Completes the direction declared and abandoned in **#2130** (D6).
- Implements the unbuilt `context_safe` half of **proposal 0059 §222** (D7).

## References

- Tracking issue and full discussion record: **#3978** (including §12, sixteen retractions made
  during design — several conclusions here reverse an earlier, measured, wrong one)
- Sequencing and interface: [proposal 0067](../proposals/0067-task-model-and-arbiter.md)
- Naming: [tool-naming.md](../../reference/runtime/tool-naming.md); the `cancel` addition and the
  `spawn` family word-order question are tracked in **#4004**
- Stale hook-point count: **#3996**
- MCP Tasks: <https://modelcontextprotocol.io/extensions/tasks/overview>
- A2A Life of a Task: <https://a2a-protocol.org/latest/topics/life-of-a-task/>
