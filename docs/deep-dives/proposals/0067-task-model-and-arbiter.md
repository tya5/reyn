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
| `run_pipeline` | `name`, `definition`, `collect`, `on_settle`, … | as `run_prompt` | lexicon `run` | `sync` |
| ~~`delegate_to_agent`~~ | — | — | — | retired |
| ~~`run_pipeline_{async,inline,inline_async}`~~ | — | — | — | retired (folded into `collect`) |

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
        declare every existing schema's current fields context_safe: true
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
      ⚠️ read the point set from `schema_registry.BARE_TO_KIND`, never a count written in
         prose — several comments claimed ten when there were eight (#3996), and the
         prose counts drifted again when `task_settled` landed (#4091 → #4103)

P4    task ops: run_prompt / describe_task / list_tasks / cancel_task
        `cancel` joins CANONICAL_VERBS; REMOVAL_VERBS is untouched (separate frozensets)
        cancellation machinery already exists for all three kinds — this exposes it
      ✅ `[task_completed]`/`[task_spawned] kind=` migrated — landed separately from the
         task-op tools themselves (blocked on an architect ruling that arrived later),
         not literally "same PR" as originally noted here. Architect ruling 2026-08-10:
         both prior values (`agent`, `spawned_session`) collapse to `prompt` — D2's
         `kind` axis names WHAT ran, not WHO; `sid=`/`from=` on the header still carry
         "who", unaffected. `exec`/`pipeline` producers were NOT touched this round
         (architect: unverified whether they carry this same header at all — `exec`
         doesn't have one today; `pipeline` completion may be a different producer).
         `descriptions/catalog.py`'s TASK_SPAWNED/TASK_COMPLETED text updated in the
         same PR as the code.

P5    send_to_session

P6    retire delegate_to_agent (its own PR; 129 files mention it, whole-repo, at 5f80e0a6 —
      re-measure before starting, and see the scope note below for what is NOT work)
      pending_chains is repurposed as P3/P4's collection substrate
      P6 is CATALOG-ONLY and does not touch `chain_manager.py`. Deleting
      `delegate_to_agent.py` removes the only consumer of `RouterCallerState.send_to_agent`,
      so the chain-registration site stops producing chains — and everything the completion
      path still handles afterward is pre-existing (in-flight or restored), carrying
      `kind is None`, i.e. NOT tasks. Folding those into `settle()` would route non-tasks
      through the task settle path; firing `task_settled` alongside would announce a settle
      for something that is not a task. The fold belongs to `run_prompt(collect="async")`,
      the first step that creates task-shaped chains on this path.
      Acceptance:
        - both S3 deny sets shed `delegate_to_agent` in the same PR, and the equality gate
          added in P7 stays green (see the deny-set note below).
        - `kind is None` after P6 means "not a task" and no more: restored relay chains and
          joins both keep it, so the value cannot be narrowed to either one alone.

P4e   run_prompt(collect="async") — the substrate step this arc deferred twice
      ⚠️ P4's letters are identity, not order: P4d landed AFTER P5, and P4e lands after
      P6/P7. Read the position in this block for sequence and the letter for which tool
      family the step belongs to. (Counting "how many steps are done" from the letters
      is what makes P4 look like one step when it is five.)
      Owns the settle fold, and with it the two renames P4 deliberately postponed:
        - `origin_agent` / `origin_depth` / `origin_sid` become the stored form of
          `requester`; the derived property P4 added stops being a translation layer. The
          check is that no field carries a delegate-specific NAME and is still required.
        - `register()` accepts `from_user` and never stores it — the `_PendingChain(...)`
          construction does not reference it. Either it feeds something this step keeps,
          or it goes.
      These were written as P6 conditions while P6 was still assumed to touch the substrate.
      It does not, so they move here rather than being dropped.

P7    run_pipeline: four names → one (`collect` carries the attached/async axis)
      The two SOURCE params are unchanged: `name` (a registered pipeline) and `definition`
      (an ad-hoc DSL string) — exactly one, validated, never inferred from which is present.
      P7 adds `collect` and `on_settle` and renames nothing.

P8    ttl expiry: reuse the chain-timeout shape, plus persist `arm_at`
      chain_manager.py re-arms "a fresh timeout watchdog" on restore, so a crash currently
      extends the effective deadline
      ✅ 解消 — P8 は #4140 で着地（restore が残り時間で張り直す）。以下は着手前の記録で、書き換えない。
      🔴 Requires #4108 first. This line assumed a chain field could simply be persisted;
         it could not. `ChainManager.update()` forwards any field to the WAL, but the
         snapshot write-back handled `waiting_on` alone — so `arm_at` would have been
         dropped from reconstruction AND the same call would have overwritten `waiting_on`
         with `[]`, destroying live state. P8 is the first caller that would have hit it.
```

### Observation (explicitly not bounding)

```
P9    inbox depth on the monitoring read surface; an audit-event for a settle whose destination
      is gone
      ⚠️ Neither is a valve. `check_pre_llm` already bounds spend regardless of producer.
      ✅ Half 1: `describe_task`/`list_tasks` gain `session_inbox_depth` (architect ruling
         2026-08-10 — named to make clear it's a SESSION property, not a task property, per
         lead-coder's review; an instantaneous `asyncio.Queue.qsize()` read, documented as such
         in the field's own LLM-visible description).
      🔴 Known scope limit (architect, not a defect — record here rather than silently accept):
         this surface only reflects sessions that HAVE a running task. `send_to_session`
         (P5) pushes onto a peer's inbox WITHOUT creating a task, so "queued but no task"
         is a normal, unobservable-by-this-surface state. Closing that gap needs a
         session-subject read surface (not a task-subject one) and is out of P9's range.
      ✅ Half 2: `task_settle_undelivered` audit-event (renamed from an earlier
         `task_settle_undeliverable` draft — architect: existing kinds follow
         `<subject>_<past-participle>`, not an adjective), emitted alongside the existing
         `logger.warning` in `PipelineExecutorDriver._deliver`'s `target is None` branch —
         the DROP behavior itself is unchanged (0067's own arc-wide rule: observe a
         vanished destination, never invent a new one for it).
```

## Verification notes for implementers

Measured during design; each has bitten or would bite:

| | finding |
|---|---|
| `dispatch_kind` | means "end this turn", not "is asynchronous" (`router_loop.py`). P1′ and P6 touch it directly. |
| `agent_locks` | the serialization lock is keyed by **agent name** while what it protects is a **session's** history. P0/P1 move identity to the session axis; this key is then inconsistent. ✅ 解消 — P1（#4090）（記録として原文を残す） |
| `_is_quiescent` | its docstring promises three conditions; the implementation checks `inbox.empty()` only (`message_bus.py`). ✅ 解消 — P1′（#4086）（記録として原文を残す） |
| `RunEntry` | `interfaces/web/run_registry.py` is a fourth handle store already carrying status/result/error/webhook_url plus persistence. §7's type is its generalization; whether it moves to core is a layering decision. |
| `_last_reply_to` | inherited when a trigger carries no `reply_to` (`session.py`) — a live misdelivery path that P1 removes. ✅ 解消 — P1（#4090）（記録として原文を残す） |
| chain timers | re-armed *fresh* on restore, so a crash extends the deadline (P8). ✅ 解消 — #4140（記録として原文を残す） |
| hook points | **do not restate the count.** `ALLOWED_HOOK_POINTS` derives from `schema_registry.BARE_TO_KIND`; every prose count has drifted at least once (#3996 found comments claiming ten against eight; #4103 swept the rest after `task_settled` made them wrong again). Read the registry. ✅ 解消 — P3（#4087）（記録として原文を残す） |
| S3 deny sets | the launch-verb deny exists **twice** — `_PIPELINE_STEP_DENY_TOOLS` (`tools/pipeline_verbs.py`, R6 S3, pipeline tool steps) and `_DELEGATION_DENY_TOOLS` (`runtime/session_api.py`, R5, agent steps). Same five names today. Both files say "kept in lock-step" and **nothing enforces it**: the only place `tests/` names both is a module docstring, and no test compares them. P6 and P7 each touch both. See the note below. ✅ 解消 — 等値検査が在る（`tests/tools/test_pipeline_step_deny_gate_3978.py`）（記録として原文を残す） |
| `RunStatus` | the status vocabulary D3 describes **already existed** — five members, with the `input_required` transition live in `a2a_intervention.py` (`self._registry.update(self._run_id, status="input-required")`). What was missing was a bridge to the chain handle, not a state machine. It lived in `interfaces/web/run_registry.py`, and reading it from `runtime/` would have been a new layering inversion (`runtime/ → interfaces/web/` was 0 imports; the reverse, 10), so **P4 moved it to `runtime/task_types.py`** beside `TaskKind` / `Requester`; `run_registry.py` re-exports it. The rename to `TaskStatus` waits for P6, same treatment as `requester`. |

### The two S3 deny sets stay two, and get an equality check

They are not one fact stored twice — they are two rules (R5 on agent steps, R6 S3 on pipeline tool
steps) that currently name the same tools. Divergence could one day be correct: a tool safe to call
from an agent step but not from a pipeline tool step. Collapsing them into one set would forbid that
structurally, which is the opposite of the reasoning that put the cancel hook onto `_PendingChain`
as a single field — there, one fact lived in two places and any divergence was a bug.

The discriminator is **one fact, or two rules that agree** — not "are there two containers".

What the pair does need is a check, because the agreement is currently asserted only in prose:

- `set(_PIPELINE_STEP_DENY_TOOLS) == set(_DELEGATION_DENY_TOOLS)`
- plus a non-empty assertion on both — `set() == set()` is green, so an emptied set would pass the
  equality check while denying nothing
- and a docstring line saying that diverging them means editing this test deliberately

That converts silent drift into a red test without forbidding a deliberate divergence.

⚠️ The drift is **asymmetric**, and the safe direction is the one P6/P7 will actually take. Failing
to *remove* a retired name over-denies and is harmless. Failing to *add* a future launch verb to one
of the two leaves that surface under-denied — a real hole, and the direction nothing is watching.

Put the check on whichever of P6 / P7 touches both files first.

### Adding or retiring an LLM-visible tool re-records the same fixtures

Every step of this arc that changes the tool surface has paid the same bill, and each one
discovered it at CI rather than budgeting for it:

| step | change | what went red |
|---|---|---|
| P5 (#4101) | `send_to_session` added | `fp0063_arc_witness` turn1/turn2, `test_router_tools` |
| P7 (#4115) | three `run_pipeline_*` names retired | the same `fp0063_arc_witness` pair |
| P4d (#4117) | `run_prompt` added | the same pair, plus two `router_tools` goldens |

The cause is that the recorded LLM payloads embed the whole tool catalog, so a tool appearing
or disappearing changes a hash in fixtures that have nothing to do with the feature. **P6 will
hit this hardest** — retiring `delegate_to_agent` removes a tool that has been in every recorded
catalog since those fixtures were made.

Budget the re-record as part of any surface-changing step, and re-record rather than hand-edit:
the fixtures are captured payloads, and an edited one no longer witnesses what it claims to.

### `input_required` is spelled three ways

Grepping `src/` for `input_required` returns nothing, which reads as "D3 is unimplemented." It is
implemented; the underscore is the wrong needle.

| where | spelling | whose convention |
|---|---|---|
| ADR-0040 §D3 prose, and its MCP-Tasks quote | `input_required` | MCP's |
| the `RunStatus` member name (`runtime/task_types.py`) | `INPUT_REQUIRED` | Python's |
| that member's **value**, and `_A2A_TASK_STATES` (`a2a_task_view.py`) | `"input-required"` | A2A's — this is what reaches the wire |

Write `RunStatus.INPUT_REQUIRED`, never the string. This cost a reviewer a false "D3 is
unimplemented, that's a separate problem" reading on 2026-08-10, from a grep that was correct in
every way except the needle.

### P6's scope: what the 129 files are, and which are not work

The earlier estimate here read `~19 src / ~45 tests / ~51 docs`, which is 115 and names three
directories. Naming three directories tells the reader those are the population, and they are not
— `scripts/` (6), `website/` (2) and `CHANGELOG.md` (1) were never counted. The count also drifted
upward inside the three that were. The note that used to sit here warned about the drift, which is
the smaller of the two problems: "re-measure before starting" reads as *recount these three*, so it
cannot catch a missing surface. Take the population as **the whole repo**, not a directory list.

Two of the nine are **not work**:

| | why |
|---|---|
| `scripts/flat_tests_arc_population.json` | records a test population *as it was*; holding paths that no longer exist is the file's job |
| `scripts/flat_tests_disposition.json` | same |

Editing those to remove a retired name rewrites a record of what was true then — the same line
`decisions/README.md` draws for accepted ADRs and this repo draws for `journal/`. Leave them.
`CHANGELOG.md` and `website/` are undecided rather than excluded: if the mention describes a past
release, it is history too. Nobody has read them; decide at P6.

### Reading ADR-0040 on what a cancel leaves behind

The ADR's `cancel`-verb entry says "a cancelled task's record persists with
`status="cancelled"`", which against D4 (immediate deletion, no retention) reads as registry
retention for one status. It is not. Two different things are named, and D4 §"records it; that is
observation, not retention" already draws the same line:

| | at settle |
|---|---|
| the **handle** (`task_id` → the pending entry) | **deleted** — D4, uniformly for every settle, cancelled included |
| the **audit-event record** (P6 `.reyn/events`) | **persists** — this is "the record" |

Two implementation questions were being decided from that sentence on 2026-08-10, which is why it
is written out here rather than left to the reader:

- **Does a cancel settle?** Yes — `status=cancelled`, delivered on the same `on_settle` path.
  A cancel that did not settle would leave a requester who asked for delivery with nothing, and
  would give teardown a second route (the settle path's own acceptance condition is one function).
- **What is `describe_task`'s status domain?** Since every settle deletes the handle, a
  `describe_task` reply can only ever carry a **non-terminal** status: `running` or
  `input-required`. `completed` / `failed` / `cancelled` are real, and reach the audit trail and
  the requester's delivery — but never a `describe_task` reply.

⚠️ This is a **reading** of an accepted ADR, recorded on the mutable surface because ADRs are
immutable once accepted (`decisions/README.md:14`). It adds no decision the owner did not accept;
where it draws a consequence, the consequence is marked as such above.

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
