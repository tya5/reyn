---
type: concept
topic: architecture
audience: [human, agent]
---

# Events

Every state change in reyn emits an audit-event. The audit-event log is the runtime's diary: a JSONL stream that records what happened, in order, with enough detail to replay the run.

## Why everything is an audit-event

There is no separate logger, tracer, or telemetry hook. The same channel powers:

- **Live debug output.** Console reporters subscribe to the audit-event stream and render each audit-event as it arrives.
- **Replay.** `reyn events <log_file>` re-renders a saved log to the console without re-invoking the LLM.
- **Eval analytics.** Eval reports aggregate audit-event data (token usage, validation errors) per case.
- **Crash recovery (implemented).** Crash recovery reconstructs agent state from the WAL (`.reyn/state/wal.jsonl`) plus seq-keyed snapshots as its substrate — not the audit-event log. User-facing rewind/resume (PITR + global rewind) is a separate design; see [Time-travel](time-travel.md).

If the OS is the only mutator (P3) and every mutation emits an audit-event, the audit-event log is sufficient. There's no "what else happened" to chase down.

## What gets recorded

A few of the larger buckets:

- **LLM and context** — `llm_called`.
- **Control IR** — one audit-event per op kind, for example:
  <!-- BEGIN control-ir-bucket-example-kinds -->
  ```text
  mcp_called
  sandboxed_exec_started
  semantic_search_embed_failed
  web_search_started
  ```
  <!-- END control-ir-bucket-example-kinds -->
  plus `permission_denied`. A `file` op like a read instead rides the shared `tool_executed` kind, naming the specific operation in its `op` field rather than as a kind of its own.
- **User interaction** — `user_message_received`, `user_intervention_received`, `chat_started`, `chat_stopped`, `turn_cancelled`.
- **Agent-to-agent messaging** — `agent_message_sent`, `agent_request_received`, `agent_response_received`, `agent_message_refused`, `chain_timeout`. Each carries `chain_id` so a single user request can be traced across hops.

The buckets above are examples. The `type` field is a **closed vocabulary** — every kind reyn emits, and nothing else, is enumerated in the [events reference](../../reference/runtime/events.md#kind-vocabulary), derived from one source of truth in `src/reyn/core/events/event_schema.py` and CI-checked against both the emitting code and the page. A consumer outside reyn can therefore enumerate the complete set of types it may receive.

### Task subscription events — historical WAL design, now removed

Task↔session binding changes (assignee/requester) were *designed* (#2187) to be recorded as **WAL** kinds (`task_subscribed`, `task_rebound` — StateLog, `.reyn/state/wal.jsonl`), not the P6 audit-event log — so if this mechanism were live, you would look for it there, not here. It never got a writer, and #3436 removed the two kinds from the WAL vocabulary entirely (dead declaration, no producer). Task↔session binding changes are not recorded anywhere today. The WAL is generally the crash-recovery and time-travel substrate; the audit-event log is the per-run trace — they are separate logs with different durability contracts (see [Time-travel](time-travel.md) — *WAL vs audit-event separation*).

## What an audit-event is

Every audit-event has a stable envelope:

```
type      — event type (see reference)
timestamp — ISO-8601 timestamp
data      — flat dict of payload fields specific to the type
```

Key fields present in most audit-events (in `data`):

```
run_id    — uuid for the run (present on most run-scoped audit-events)
```

Note: `run_id` is present on most run-scoped audit-events (`llm_called`,
`permission_denied`) but absent from some audit-events emitted outside a run
context (e.g. `chat_started`). Which kinds carry `run_id` is a payload question,
not a vocabulary one — the closed set above says which kinds exist, and does not
partition them by field. There is no enumeration of the `run_id`-carrying subset;
read the per-kind payloads in the reference.

### Audit-events with required fields (FP-0021)

A growing set of audit-event kinds are required to carry specific
audit fields in their `data` dict (the required fields vary per kind — e.g.
`llm_called` requires `model`; `permission_granted`/`permission_denied`
require `run_id`, `actor`, `phase`). The authoritative, current registry
lives in `src/reyn/core/events/event_schema.py`
(`EVENT_AUDIT_REQUIREMENTS`) — this list has grown over time and will keep
growing, so it is not duplicated here. Dedicated per-feature invariant tests
(e.g. `tests/core/test_session_lifecycle_events_1800.py`,
`tests/runtime/test_mcp_search_tool_invariants.py`,
`tests/dev/test_chat_turn_completed_inline.py`) each assert that their event
kinds are declared here with the correct required fields, on every CI run.

Enforcement is test-time only (not at `emit()` runtime) to keep
production overhead zero.

Stable shape makes the log machine-readable without a custom parser per consumer.

## Write-side backend (#4496)

Where an audit-event lands on disk (or doesn't) is a separate axis from
whether it fires and reaches subscribers. `EventLog.emit()` always: stamps
`agent_id`/`run_id`/`emitter`/`audit_seq`, hands the event off for
subscriber delivery, and returns — regardless of the write-side backend
`reyn.yaml`'s `audit_events.backend` selects (see [config
reference](../../reference/config/reyn-yaml.md#audit_events-block)):

- **`local`** (default) — writes to `.reyn/events`, unchanged from before
  this abstraction existed.
- **`discard`** (sink-null) — writes nothing. Subscriber delivery and
  `audit_seq` continuity are unaffected; only the disk write is skipped.
  `reyn events replay` / support-bundle / dogfood_trace have nothing to
  read for a `discard` run — this is a real trade-off (support-bundle in
  particular is the tool operators use to report bugs), not a free lunch.

The backend is called from inside `emit()`, before subscriber dispatch,
wrapped in its own try/except — never inserted as just another
subscriber. That ordering is what guarantees a backend failure (or a
future network backend's connection error) can never silence a
subscriber, and a raising subscriber can never stop the backend from
having already written. See `src/reyn/core/events/backend.py`'s module
docstring for the full mechanism.

**Dispatch timing is NOT uniform (#4966).** `emit()` branches on whether a
running event loop exists at call time: with one, the event is queued and
a background consumer task delivers it to every subscriber — `emit()` can
return BEFORE that delivery happens, not after. Without one (no running
loop — e.g. a synchronous CLI path), dispatch runs inline, synchronously,
before `emit()` returns. Both branches share the same per-subscriber
try/except isolation (#4963) — one subscriber's failure never blocks the
next, in either branch — but "emit() returned" is a delivery guarantee
only in the no-loop case. A caller that needs delivery to have actually
happened before proceeding awaits `EventLog.drain()` explicitly (see its
own docstring for the queue/consumer race it resolves).

### `agent_delta` durable-write coalescing (#4960)

One kind gets special treatment on the `local` backend's write side:
`agent_delta` (one audit-event per streamed reply chunk) is NOT written
to disk per-fragment. Live subscriber delivery is unaffected — every
fragment still dispatches to the TUI/AG-UI exactly as before. Measured
(2000-delta/60KB real streamed reply, `agent_delta_include_text=true`):
unthrottled, `agent_delta` writes were 99.4% of that run's audit file
bytes, so writing every fragment durably would dominate the log for the
duration of any streamed reply. That figure assumes `text` is being
written — with #4666's default (`agent_delta_include_text=false`, see
below), a coalesced record's bytes are smaller and this percentage does
not apply as measured.

Three mechanisms, each covering a gap the others leave open:

- **Fragment count** (`agent_delta_coalesce_fragments`, default 100) —
  one durable record per N fragments. Governs under normal bursty
  streaming (measured up to ~1000 fragments/s through a proxy).
- **Interval** (`agent_delta_coalesce_interval_ms`, default 2000ms) — one
  durable record per T milliseconds when N hasn't been reached. This is
  the ONLY guarantee for a process-level death (SIGKILL / OOM-kill / host
  crash) — a Python `finally` never runs in that case, so the terminal
  flush below cannot help.
- **Terminal flush** — when a streaming chain ends (success, a raised
  exception, or cancellation — `RouterLoop.run()`'s own `finally`), any
  fragments accumulated since the last durable write get one final
  record. Covers a SHORT interruption that ends before either threshold
  above is reached — architect's ruling identified this as the most
  likely interruption shape, and losing it would defeat the whole reason
  coalescing (rather than dropping `agent_delta` writes entirely) was
  chosen: **cost accountability** — if a call dies mid-stream before its
  `usage` record lands, the coalesced record is the only surviving
  evidence that a partial reply (and its token cost) existed at all.

A coalesced record carries `coalesced_fragment_count`; `reyn events
replay` therefore shows fewer `agent_delta` records than fragments
actually streamed — declared via `LocalEventBackend.declare_gaps()`
(contract 2), never a silent loss. See `src/reyn/core/events/backend.py`'s
module-level constants for the full measured rationale, and the
[config reference](../../reference/config/reyn-yaml.md#audit_events-block)
for the operator-facing knobs.

### `agent_delta`'s own content is a SEPARATE opt-in (#4666)

Coalescing (above) decides how OFTEN a durable record is written.
Whether that record carries the reply's own CONTENT — the `text` field —
is a different question, with its own knob:
`audit_events.agent_delta_include_text` (default `false`). Deliberately
NOT unified with the coalescing knobs (owner ruling: each content opt-in
gets its own toggle) — mirrors OpenTelemetry's GenAI convention that
"every attribute that can hold prompt or output content is opt-in,
default metadata-only" (the routinely-PII rationale).

Off (the default): the durable record still carries `chain_id` /
`round_index` / `coalesced_fragment_count` / `audit_seq` — enough to
prove "a partial reply of N fragments existed" (#4960's own reason for
coalescing at all: cost accountability for a call whose `usage` record
never lands) — but not the reply's own text. `LocalEventBackend.
declare_gaps()` names this gap ONLY while the flag is off, never
statically (contract 2's "declared vs never-existed" distinction, per
architect's own #4960 ruling): reading a durable log written while the
flag was on must never be told the text was "never retained" when it
was, and vice versa.

**⚠️ Default-behavior change (2026-08-21, #4666, owner ruling):** before
this config field existed, `agent_delta`'s reply content was ALWAYS
durably written, with no opt-in or opt-out. If your workflow depends on
`.reyn/events` carrying streamed-reply text, set
`agent_delta_include_text: true`.

**Unchanged either way:** live TUI/AG-UI delivery (every fragment, full
text, always — this flag governs the durable write only) and
`history.jsonl` (the completed reply's own persistence is a separate
mechanism entirely, untouched by this).

### The completed model→user text is ANOTHER, separate opt-in (#4666②)

`agent_delta_include_text` above gates the STREAMED fragments' content.
The COMPLETED text — what the user actually saw once a turn resolved —
is a different question with its own knob: `audit_events.
completed_response_include_text` (default `false`, owner ruling, same
"one toggle must never cover both" instruction as ①). It gates TWO
kinds:

- `agent_response_committed` (new kind) — emitted from `Session.
  _put_outbox`, the single measured choke point every model→user text
  commit funnels through: the terminal reply (organic or its
  `response_format` variant), mid-loop budget force-close, max_iterations
  wrap-up, `session.py`'s own router_cap wrap-up, and the tool_calls-round
  accompanying text (`persist=False`, not written to history, but the
  user sees it — still in scope per architect's ruling that ② asks "what
  was said", not "why"). Excludes cancellation (no result is ever
  appended for a cancelled turn) and canned/synthetic non-model text.
- `user_intervention_requested`'s `question`/`suggestions`/`options` — the
  model's own `ask_user` question. Covered by ②, not ③, because the
  question is text the MODEL directed at the user — same content type as
  the rest of ②'s scope (owner ruling: one knob per CONTENT TYPE). The
  answer (`user_intervention_received.answer`, item ③'s own row) is a
  DIFFERENT content type — text the USER directed at the model — so it
  is necessarily gated by ③'s own, separate knob, not this one.
  (Architect's own earlier framing here — "②と③ share one knob, since
  they're two ends of one exchange" — was corrected: the owner's ruling
  is one knob per content type, and question/answer are two different
  types by construction, so they were always going to be two different
  knobs. The conclusion, question ∈ ②, was right for a different reason
  than the one first given.)

Both events fire unconditionally, same shape as ①: off (the default),
`LocalEventBackend.write()` drops only the free-text field(s) —
`chain_id` / `intervention_id` are always kept, so "a response was
committed" / "a question was asked" remains provable without content.
`declare_gaps()` names this gap dynamically, same discipline as ①'s own.
Unchanged either way: live TUI/AG-UI delivery and any opt-in OTEL
subscriber. **Toggling ② without ③ (or vice versa) is a deliberate
per-operator choice, not an accident** — e.g. ② on / ③ off durably
records the model's questions but not the user's answers to them; that
asymmetry is the intended shape of "one knob per content type", not a
half-recorded exchange to be fixed.

**⚠️ ② closes the `ask_user` dispatcher-level leak, but not every
conversation-content leak in general.** The owner's "conversation body"
ruling is a property of CONTENT, not of which event `kind` carries it —
the SAME string (e.g. `ask_user`'s question) used to duplicate into
`tool_called.args` for the generic `dispatch_tool` path, entirely outside
the three owner-ruled knobs' reach. #4666 item ③b closed that specific
leak: a per-tool declaration (`reyn.core.dispatch.content_declarations`)
lets a tool mark one of its own fields as `"assistant"`-directed content,
consulted by `dispatch_tool` and redacted under the SAME ② knob. **Scope,
measured, not extended by guess:** only `ask_user` declares any field
today (`question` → `"assistant"`); `mcp_called.args` was measured and
excluded (an MCP tool's args are model-decided call parameters, not a
dedicated question/answer channel). A tool that shows the user free text
and forgets to declare it still leaks that text silently through this
path — the declaration bound only catches the declared set GROWING,
never catches a tool that should have declared and didn't.

### User input's own content is a THIRD, separate opt-in (#4666 item ③)

The two knobs above both govern the AGENT's own output. A user's own
typed/chosen text gets a third, independent knob:
`audit_events.user_input_include_text` (default `false`) — covering the
6 kinds where a user's own words reach an audit-event: `user_submitted`,
`user_message_received`, `intervention_answer_submitted`,
`user_answered_intervention`, `user_intervention_received`,
`router_retry_exhausted` (an AST census across the whole tree, lead-
coder, #4666 — an earlier pass found 3 and undercounted). See the
[events reference](../../reference/runtime/events.md#user-interaction)
for the exact field dropped per kind.

No coalescing involved here — unlike `agent_delta`, each of these 6
kinds is written individually; the knob only decides whether that one
write keeps its content field or drops it. Off (the default): every
other field on the kind (`chain_id`/`intervention_id`/`msg_id`/`seq`/
etc.) is still recorded, so "a submission/answer happened" remains
provable without the words themselves. `LocalEventBackend.declare_gaps()`
names this gap dynamically, same discipline as the two knobs above.

**⚠️ Default-behavior change (2026-08-21, #4666):** before this field
existed, these 6 kinds' content was always durably recorded,
unconditionally.

**Unchanged either way:** live subscriber delivery (TUI/AG-UI/peer
broadcast — every one of these kinds reaches its subscribers before this
backend-level knob is ever consulted) and `history.jsonl`.

**⚠️ `ask_user`'s dispatcher-level leak is now closed too (#4666 item
③b).** `ask_user`'s answer used to also reach the audit log
unconditionally via `tool_returned.result["answer"]` (`dispatch_tool`, a
wholly separate emit path from the 6 kinds above) — outside this knob's
reach entirely. A per-tool declaration
(`reyn.core.dispatch.content_declarations`) now lets `ask_user` mark
`answer` as `"user"`-directed content, consulted by `dispatch_tool` and
redacted under this SAME knob. **Scope, measured, not extended by
guess:** only `ask_user` declares any field today; a tool that shows the
user free text and forgets to declare it still leaks that text silently
through this path — the declaration bound only catches the declared set
GROWING, never catches a tool that should have declared and didn't. Do
not read the 6-kind list above as exhaustive coverage of "user input
reaches the audit log" — a future undeclared tool is a real gap this
knob cannot see.

## What audit-events are NOT

- **Not application logs.** A workflow author shouldn't emit free-form audit-events. The set is OS-defined.
- **Not memory.** Audit-events are the runtime's per-run record; memory is across-run knowledge. See [../data-retrieval/memory.md](../data-retrieval/memory.md).
- **Not the source of truth for artifacts.** Artifacts pass through the workspace channel; audit-events record that they passed.

## Reading audit-events as a debugging tool

When something looks wrong:

1. Find the run id from the run output's last line (`events saved → ...`).
2. `reyn events .reyn/events/<run_id>.jsonl --conversation` to see what each LLM call looked like and what it returned.
3. Or `--filter permission_denied` to jump straight to where the OS refused an op.

You don't need a debugger; the log already has the information.

## See also

- [Reference: events](../../reference/runtime/events.md) — the full audit-event taxonomy

Audit-events are the per-run trace, not the crash-recovery or time-travel
substrate — those are WAL-backed (see [Time-travel](time-travel.md)). For
payload-level trace inspection, see
[reference/dogfood-tracing.md](../../reference/dogfood-tracing.md).
