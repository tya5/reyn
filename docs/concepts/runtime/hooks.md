---
type: concept
topic: runtime
audience: [human, agent]
---

# Agent lifecycle hooks

Hooks are a thin operator-scoped layer that lets you inject context,
trigger self-continuation, run a sandboxed side-effect, or launch a pipeline
at any of the six **lifecycle points** in a reyn session — or, at an
**external-event point** fired by something outside the session's own
run-loop (a subscribed MCP resource changing, a watched file changing, a
cron job firing, or an inbound webhook).

They are built on two mechanisms that already exist: the **unified inbox** (the
channel that feeds messages into a turn) and the **P6 lifecycle** (the event
stream). No new OS machinery — a new workflow that uses hooks does not require any
OS change (P7).

## Syntax quick reference

Everything below is detailed further on; this section is a self-contained
lookup for authoring a `hooks:` (and, if needed, `composers:`) block without
reading the rest of the page.

### `on:` values — what a hook's `on:` accepts vs. what a Composer's `inputs[].kind` accepts

**These are two different, non-interchangeable vocabularies** — a value
valid in one is not necessarily valid in the other:

| Bare form | Namespaced form (also accepted) | Fires | Valid as a hook's `on:`? | Valid as a Composer `inputs[].kind`? |
|---|---|---|:---:|:---:|
| `session_start` | `builtin:lifecycle:session_start` | session opens | ✅ | ✅ |
| `session_end` | `builtin:lifecycle:session_end` | session closes | ✅ | ✅ |
| `turn_start` | `builtin:lifecycle:turn_start` | a turn begins | ✅ | ✅ |
| `turn_end` | `builtin:lifecycle:turn_end` | a turn's terminal `stop_reason` | ✅ | ✅ |
| `task_start` | `builtin:lifecycle:task_start` | a dynamic task is created | ✅ | ✅ |
| `task_end` | `builtin:lifecycle:task_end` | a dynamic task completes or aborts | ✅ | ✅ |
| `mcp_resource_updated` | `builtin:external:mcp_resource_updated` | a subscribed MCP resource pushes an update | ✅ | ✅ |
| `file_changed` | `builtin:external:file_changed` | a watched path changes ([`fs_watch`](../../reference/config/reyn-yaml.md#fs_watch-block) required) | ✅ | ✅ |
| `cron_fired` | `builtin:external:cron_fired` | a message-based `cron:` job delivers | ✅ | ✅ |
| `webhook_received` | `builtin:external:webhook_received` | an inbound webhook resolves to this session | ✅ | ✅ |
| — (open) | `composed:<name>` | a Composer (see below) publishes its correlated output | ✅ | ✅ (chaining — another Composer's output) |
| — (open) | `llm:<session_id>:<event_name>` | the LLM itself emits one via `emit_hook_event` (always its own session) | ❌ **rejected at load** (`HookConfigError`) | ✅ |

**`llm:*` can never be a hook's `on:` value — only a Composer input.** A
`hooks:` entry only accepts the 10 builtin bare/namespaced forms above or a
`composed:<name>` prefix; anything else (including a well-formed
`llm:<session_id>:<event_name>`) is a load-time `HookConfigError`. To react
to an LLM-emitted event, correlate it through a `composers:` entry into a
`composed:<name>` event, then put your `hooks:` entry's `on:` on THAT
composed kind — see the [worked example](#llm-authored-hook-events-emit_hook_event)
below. The bare/namespaced-form duality only applies within the 10 builtin
points — it does not extend `on:`'s acceptance to `llm:*`.

### The 4 config schemes — every field

A hook entry sets exactly one scheme. Every scheme accepts the two
top-level fields `on` (required, see above) and `name` (optional string,
defaults to the `on` value — becomes the `[hook:<name>]` attribution prefix)
plus the optional `matcher` (see [below](#matcher-narrowing-which-events-fire-a-hook)).

**`template_push`** — a Jinja2-templated inbox push:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `message` | str (Jinja2) | _required_ | Rendered text of the pushed `[hook:name]` message. |
| `wake` | bool \| str (Jinja2 → bool) | `true` | `true` starts a new turn (self-continuation, **E**); `false` rides passively into the next turn (context-inject, **C**). |
| `push_when` | str (Jinja2 → bool) | `"true"` | `false` skips the push entirely (conditional push). |
| `session` | str \| None | `None` (current session) | Routes the push to a *different* session's inbox — [cross-session push](#cross-session-push). |

**`shell_exec`** — a sandboxed side-effect command:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `shell_exec` | str | _required_ | The command line. stdout/stderr are ignored; the event is written to the command's stdin as JSON. |

**`shell_push`** — a sandboxed command whose stdout IS a push directive:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `shell_push` | str | _required_ | The command line. stdout must be pure JSON: `{"push_when": bool, "wake": bool, "message": str, "session"?: str}` (first three required). Any failure (non-zero exit, invalid JSON, missing/wrong-typed field) skips the push, fail-safe. |

**`pipeline_launch`** — launch a registered pipeline, async/detached:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | str | _required_ | The pipeline's registered name, resolved at dispatch time. Unregistered → warning + skip, lifecycle point still completes. |
| `input_template` | dict \| str \| None | `None` | `dict`: every string leaf (recursively) is Jinja2-rendered. `str`: rendered once, output parsed as a JSON object. `None`: launches with no input. |

### `matcher` grammar

`matcher: {field: pattern, ...}` — evaluated against the firing event's
template vars before the hook's action runs.

- Match rule by field **name**: `uri` and `path` use a shell-style glob
  (`fnmatch`); every other field name is exact string equality.
- Absent or empty `matcher` → the hook always fires.
- For the 10 builtin points, a matcher field outside that point's payload
  (a typo, or a field the point never carries) is a **load-time
  `HookConfigError`** — rejected before the hook can ever run.
- For a hook's `matcher` on a `composed:*` `on:` target, or a Composer
  input's `match` on an `llm:*` `inputs[].kind` (neither has a builtin
  schema entry — the open set), a field the event doesn't carry never
  matches at runtime (nothing to validate against at load time).

### `composers:` block — every field

A Composer correlates multiple Bus events into one derived `composed:<name>`
event, independent of the `hooks:` block (see
[Async Bus and Composer](#async-bus-and-composer-event-correlation) below
for the full model):

```yaml
composers:
  - name: deploy_approved          # required, unique
    op: all                        # required: all | any | seq | window | debounce | correlate_by | count
    inputs:                        # required, non-empty list
      - kind: builtin:external:mcp_resource_updated   # required
        match: {server: "github"}                     # optional — same field->pattern grammar as `matcher`
      - kind: mcp:approval-server:approved
    emit:
      kind: composed:deploy_approved   # required, MUST start with `composed:`
    policy:                         # optional
      capacity: 10                  # int, default 10
      overflow: reject               # drop_oldest (default) | drop_newest | reject
      ttl: 5m                        # duration: plain seconds, or "<N>s"/"<N>m"/"<N>h" — default 5m
    correlate_by: request_id        # required IFF op: correlate_by — the payload field to key on
    count: 3                        # required IFF op: count — the threshold
```

- `inputs[].kind` names a builtin namespaced kind (`builtin:lifecycle:*` /
  `builtin:external:*`), an `llm:<session_id>:<event_name>` kind an LLM emits
  via `emit_hook_event` in that same session, a `composed:*` kind from
  another Composer (chaining — cycles are rejected at load time), or an
  external-plugin kind (`mcp:<server>:<event>`, etc.). The LLM can only ever
  *produce* an `llm:*` event (via `emit_hook_event`); it cannot author a
  `composers:` block itself (that's an operator-owned config), only supply
  the events one correlates on.
- `inputs[].source`, if present, must be omitted or `"builtin"` — anything
  else is a load-time error (the Bus only ever carries `source="builtin"`;
  correlate on a payload field instead).
- The 7 `op` values: `all` (every input arrived), `any` (first arrival,
  stateless), `seq` (inputs' kinds arrive in the declared order), `window`
  (fire after `ttl` from the FIRST match, with everything buffered),
  `debounce` (fire `ttl` after the LAST match with no newer one), `correlate_by`
  (like `all`, keyed by a payload field), `count` (N matching events per key).
- **`inputs` arity**: `seq` requires **at least 2** inputs (a load-time
  error otherwise) — every other op accepts a **single** input, including
  `all`/`any` (both fire immediately on that one input's first arrival) and
  `count` (fires once `count` matching events of that one kind have arrived).
  A single-input Composer is the common shape for reacting to just one
  `llm:*` signal:
  ```yaml
  composers:
    - name: deploy_ready
      op: any                       # any/all are equivalent with 1 input
      inputs:
        - kind: llm:main:deploy_ready
      emit: { kind: composed:deploy_ready }
  ```
- **What the composed event carries.** `_emit_composed` builds the emitted
  event's payload as `{"inputs": [<matched event's payload dict>, ...],
  "correlation_key": <key or "__default__">}` — so a downstream hook's
  template vars are `{{ inputs }}` (a list) and `{{ correlation_key }}`, NOT
  the original event's fields flattened at the top level. **Order caveat**:
  `inputs[]` is in ARRIVAL order, not declared-config order, for every op
  except `seq` (whose whole point is enforcing the declared order) — and
  each entry is the bare payload dict with no `kind` tag to identify which
  declared input it came from. So `{{ inputs[0].<field> }}` is only reliably
  "the first *declared* input's field" when the Composer uses `op: seq`, or
  has exactly one input (nothing else can arrive first).
- A `composed:<name>` event becomes a normal Sync `on:` target — add a
  `hooks:` entry with `on: composed:deploy_approved` to react to it (see the
  worked example [below](#async-bus-and-composer-event-correlation)).

### `emit_hook_event` — LLM-authored hook-events

The LLM's own tool for putting an event onto its session's Bus — see
[LLM-authored hook-events](#llm-authored-hook-events-emit_hook_event) below
for the full syntax, autonomy boundary, and a worked example.

## Lifecycle points

Hooks fire at six lifecycle points, one for each combination of scope and direction:

| Scope   | `_start` | `_end` |
|---------|----------|--------|
| session | `session_start` | `session_end` |
| turn    | `turn_start` | `turn_end` |
| task    | `task_start` | `task_end` |

Every point is an **awaited dispatch**: the hook completes (shell exits, push is
queued) before the lifecycle point continues. This is what gives shell hooks
synchronous access to the moment — a session_start shell hook finishes before
the first turn begins.

Implementation anchors:

- `turn_end` fires at the terminal `stop_reason`
- `task_start` fires at the `_create` Control IR op; `task_end` fires at
  `_update_status` (status → completed) AND at `_abort` (status → aborted) —
  every task that starts is guaranteed a matching `task_end` regardless of how it terminates

## External-event points

Unlike the six lifecycle points above — fired from the session's own
turn/task run-loop — an **external-event point** is fired by something
outside that loop: today, a subscribed MCP resource changing.

### `mcp_resource_updated`

Fires when a server pushes a `resources/updated` notification for a resource
this session subscribed to via `subscribe_mcp_resource` (see
[Resource subscriptions](../tools-integrations/mcp.md#resource-subscriptions-the-async-push-event-source)).
Delivered from the MCP receive-loop task through a bounded queue drained on
the session's own event loop — not from the agent's own turn/task machinery
— so it can fire between turns, not only at a turn/task boundary.

Template vars available to `template_push` / `pipeline_launch` rendering:

| Var | Meaning |
|-----|---------|
| `server` | The MCP server name the resource belongs to. |
| `uri` | The updated resource's URI. |
| `resync` | `true` if this firing is a reconnect resync (see below), `false` for a real server push. |

**Resync on reconnect.** Reyn keeps no resource-content cache, so a dropped
connection could silently miss updates that happened while disconnected.
After a transport-death reconnect re-establishes every previously-tracked
subscription, this hook-point fires once per re-subscribed URI with
`resync: true` — a conservative "this may have changed while you were
disconnected, re-read if you care" signal, using the exact same hook-point
and template-var shape as a real push. It never fires on a session's very
first connection (there is nothing to resync).

### `file_changed`

Fires when a file under an operator-declared watch path is created, modified,
or deleted. Requires the `watchdog` extra (`pip install reyn[fs-watch]`) and
at least one path under `fs_watch.paths` in `reyn.yaml`:

```yaml
fs_watch:
  paths:
    - /repo/src
    - /repo/docs
  debounce_seconds: 0.2   # coalesce a write-burst on one path into ONE fire
```

Full field reference: [reyn-yaml § `fs_watch` block](../../reference/config/reyn-yaml.md#fs_watch-block).
Without either the extra or a configured path, the feature is off (a clear
warning is logged once if paths are configured but the extra is missing; no
config at all is silently byte-identical to a build with no watcher).

Template vars:

| Var | Meaning |
|-----|---------|
| `path` | The changed file's path. |
| `event_type` | `created`, `modified`, or `deleted`. |

Watched paths are declared once, at startup, in the OUT-set (`reyn.yaml` /
`reyn.local.yaml`) — there is no op or tool verb that lets an agent register
or widen a watch; a filesystem-wide change feed is treated as the same class
of concern as sandbox policy. Bursts of events for one logical change (an
editor's temp-file dance, a create-then-modify) are debounced per path — one
burst fires the hook once, not once per underlying filesystem event.

### `cron_fired`

Fires when a message-based `cron:` job delivers to its own session.

Template vars:

| Var | Meaning |
|-----|---------|
| `job_name` | The fired job's configured name. |
| `to` | The target agent name. |

### `webhook_received`

Fires when an inbound webhook (Slack, LINE, a generic plugin) resolves to a
session.

Template vars:

| Var | Meaning |
|-----|---------|
| `transport` | The logical transport (`slack`, `line`, `webhook`, ...). |
| `sender` | The full routing sender string (`"<transport>:<external_id>"`). |

The template context deliberately carries only this routing metadata —
**never the raw inbound request body**, which may carry tokens or PII the
operator never intended a hook action to see. Contrast `cron_fired`'s
`job_name`/`to`, which are operator-authored config, never end-user-supplied.

Both `cron_fired` and `webhook_received` are **non-blocking relative to their
ingress**: the cron job's own inbox delivery and the webhook's HTTP response
never wait on a hook action — dispatch is scheduled as a fire-and-forget
background task, so a slow hook (e.g. a multi-second `shell_exec`) can never
stall the ingress that triggered it.

## Matcher: narrowing which events fire a hook

A hook may set `matcher`, a `dict[str, str]` of field → pattern, evaluated
against the firing event's template vars **before** the hook's action runs:

```yaml
hooks:
  - on: mcp_resource_updated
    matcher: {server: "github", uri: "file:///repo/**"}
    template_push:
      message: "{{ uri }} changed on {{ server }}."
```

- Every named field must match: **exact string equality**, except `uri` and
  `path`, which match via a shell-style glob (`fnmatch`) — so
  `file:///repo/**` matches any URI under that prefix, and `/repo/src/**`
  matches any watched path under that directory.
- For the 10 **builtin** hook points (6 lifecycle + `mcp_resource_updated` /
  `file_changed` / `cron_fired` / `webhook_received`), a matcher field must be
  one the point's builtin schema actually carries — a typo'd or nonexistent
  field name (e.g. a lifecycle point's matcher naming `server`/`uri`, or
  `payload.srever`) is a **load-time `HookConfigError`**, rejected before the
  hook can ever run (a schema-external matcher would otherwise never fire —
  fail-loud replaces that silent footgun).
- For a **future or custom point with no builtin schema entry** (the
  schema-driven open set), a field the firing event doesn't carry still
  **never matches at runtime** — the pre-schema behavior, since there is
  nothing to validate against at load time.
- **Absent or empty matcher → the hook always fires** — the default, and the
  behavior every pre-`matcher` hook keeps unchanged.

The rule is keyed off the field *name* (`uri`/`path` glob, everything else is
exact), not the hook-point — so a future external-event source that also
emits a `uri`- or `path`-shaped field gets glob matching for free.

## Four config schemes

Each entry carries **exactly one** of four mutually-exclusive schemes:

- **`template_push`** — a push directive built from config Jinja2 templates.
- **`shell_exec`** — a sandboxed command run as a pure side-effect (output ignored).
- **`shell_push`** — a sandboxed command whose **stdout is a JSON push-directive**,
  pushed via the same path as `template_push` (the only difference is the
  directive's source: captured stdout vs a Jinja2 render).
- **`pipeline_launch`** — launch a registered [pipeline](pipelines.md) with
  input rendered from the event's template vars. See
  [Pipeline launch](#pipeline-launch-pipeline_launch) below.

## Four capabilities

Those schemes deliver four behavioral capabilities, uniformly:

### C — context inject (a push with `wake: false`)

A passive `[hook:name]` system message is queued into the unified inbox. It
rides along with the **next** turn — no extra turn is triggered. Use it to
append read-only context (metrics, timestamps, retrieved facts) that the LLM
sees in the conversation without being asked to act on it immediately. Produced
by a `template_push` or a `shell_push` whose directive sets `wake: false`.

### E — self-continuation (a push with `wake: true`)

Same as C, but the `wake: true` flag signals the run-loop to open a new turn
immediately. This is the differentiating capability: a `turn_end` hook can
restart the agent without any human input. Bounded by the [loop valve](#loop-valve).
Produced by a `template_push` or a `shell_push` with `wake: true`.

### F — external side-effect (`shell_exec`)

A sandboxed command is executed. Reyn writes a JSON event to the command's
stdin; its stdout and stderr are **ignored**. Use it to update external
state — write a log entry, emit a metric, post to a webhook. See
[Sandbox](#sandbox) for the safety model.

### Computed push (`shell_push`)

A sandboxed command whose **stdout** is a single JSON object
`{"push_when": bool, "wake": bool, "message": str, "session"?: str}` (first
three required). stdout is parsed into the same push directive a `template_push`
produces, then dispatched via the identical C/E path — so the command *decides
at runtime* whether to push (`push_when`), how (`wake`), and what (`message`).
stdout must be pure JSON (logs go to stderr). Any failure — non-zero exit,
invalid JSON, or a missing / wrong-typed field — **skips the push** (fail-safe);
the lifecycle point always proceeds. `session` names the target session for
**cross-session push** (see below) — omitted, it defaults to the current
session.

### Pipeline launch (`pipeline_launch`)

Launches a registered [pipeline](pipelines.md) by name, with an `input`
built from the firing event's template vars:

```yaml
hooks:
  - on: mcp_resource_updated
    matcher: {uri: "file:///repo/docs/**"}
    pipeline_launch:
      name: reindex_docs
      input_template: {uri: "{{ uri }}"}
```

- `name` — the pipeline's registered name, resolved at dispatch time. If it
  isn't registered, the hook logs a warning and skips the launch — the
  lifecycle/external-event point still completes normally, exactly like any
  other hook failure.
- `input_template` — optional. A `dict`'s string leaves (recursively) are
  each Jinja2-rendered against the template vars; a plain string is rendered
  once and its output parsed as a JSON object (mirroring `shell_push`'s
  "stdout is JSON" contract); omitted, the pipeline launches with no input.
- **Async/detached**, works from any hook-point (lifecycle or
  `mcp_resource_updated`): the launch is the same
  [`run_pipeline_async`](../../reference/runtime/pipeline-dsl.md#registered-launch)
  path — the hook fires-and-continues, the pipeline runs in its own
  crash-recoverable driver-session, and the result arrives later on this
  session's own inbox as a `pipeline_result` message.

### Cross-session push

A `template_push` or `shell_push` directive's `session` field routes the
push to a *different* session's inbox instead of the current one — the
target session processes it exactly as it would its own hook push (`wake`
rides along: `true` triggers a turn there, `false` rides passively into its
next turn). Naming the current session, omitting `session` entirely, or
running in a context with no cross-session routing capability all fall back
to the local (current-session) push.

## wake flag and the run-loop

`wake` (default `true`) is what splits C from E. The run-loop drains the inbox
after each turn:

1. Collect all queued hook messages.
2. Any `wake: false` messages are included as context in the upcoming turn (or
   held for the next human-driven turn if no `wake: true` is present).
3. The loop fires **one** new turn if at least one `wake: true` is present —
   all `wake: false` messages from the same batch ride along as context in that
   same turn.

If no hooks are configured or none match the current lifecycle point, the loop
is byte-identical to a hooks-free session. Zero overhead on the happy path.

## Fidelity

Pushes are **new** attributed `[hook:name]` system messages added to the
conversation. They do not mutate existing history — object identity is preserved
on every existing message. This is tested at the object-identity level, not just
content equality.

Shell output is intentionally ignored. Reyn does not support transform-hooks
(hooks that rewrite the context or the artifact stream). Real redaction,
truncation, and content fencing stay at the OS layer where they are visible,
evented, and auditable (see
[secret-handling](secret-handling.md) and
[content-layer defense](../../reference/config/reyn-yaml.md#content-layer)).

## Awaited-dispatch architecture

Hooks are dispatched by `HookDispatcher`, a first-class synchronous awaited call
at each lifecycle point. This is **not** an EventLog subscriber:

| Mechanism | Timing | Use |
|-----------|--------|-----|
| `HookDispatcher` | awaited first-class | hooks — must complete before the lifecycle point continues |
| EventLog subscriber | sync-inline, no await | real-time console render, analytics |
| WAL | append-only durable log | crash recovery |
| P6 audit event | async-tolerant | audit trail, replay, eval |

Subscribers are sync-inline and cannot `await` — they are fire-and-forget at
emit time. A shell hook that needs to wait for a process to exit cannot be
implemented as a subscriber. `HookDispatcher` solves this.

Each hook is wrapped in its own `try/except` block. A hook failure is logged and
attributed to the hook by name; it does not abort the lifecycle point or
propagate to the LLM output.

## Loop valve

`E` (self-continuation) is bounded to prevent runaway hook-driven sessions:

- **Counter**: `safety.loop.max_hook_driven_turns` (default `25`) counts
  hook-driven turns since the last human user turn.
- **Reset**: the counter resets to zero on every human turn.
- **On cap**: the configured `safety.on_limit` action fires —
  `warn` → `ask_user` → `abort`. All three leave the session alive (no silent
  kill).
- **Unlimited**: set `max_hook_driven_turns: 0` to disable the cap entirely.

The valve is a backstop, not an obstruction. A well-designed self-continuation
hook will finish before the cap; the cap catches runaway loops that a bug or
unexpected workflow behavior would otherwise leave open.

## Sandbox

Shell hooks run inside the same backend-agnostic sandbox abstraction as
Control IR `shell_exec` ops: Seatbelt (macOS), Landlock/seccomp (Linux), Noop
(unsupported platforms), or a container backend. Safe defaults apply:

- `network: false` — outbound network blocked
- No subprocess spawning
- Consent fail-closed: if the sandbox backend cannot be confirmed, the shell
  hook is refused rather than run unsandboxed

### Consent and allowlist

Shell-hook commands require operator consent before they run. The consent flow depends on whether a live intervention listener is attached:

- **Interactive chat session** (inline CUI) — consent routes through the unified intervention bus and renders as a closed-set intervention in the above-input region: "Shell hook `<name>` wants to run a command" (the hook's configured `name:` field, or a generic message if unnamed). Three choices:
  - **[A]lways** — allow and persist to the allowlist (`~/.reyn/shell-hooks-allowlist.json`, override via `REYN_SHELL_HOOKS_ALLOWLIST`). Future runs of the same command are auto-approved.
  - **[y]es** — allow this run only.
  - **[n]o** — skip (fail-closed).
- **Non-interactive** (`reyn run`, `mcp-serve`, headless) — falls back to the pre-bus behavior: TTY stdin prompt when available, or refused when stdin is not a TTY.
- **Allowlist hit** — any command already in the allowlist runs silently without a prompt (auto-approved on all surfaces).

Consent is fail-closed throughout: if the sandbox backend cannot be confirmed, the hook is refused rather than run unsandboxed.

See [sandbox](sandbox.md) for the full backend model and [permission model](permission-model.md) for the broader consent architecture.

### P6 event: `hook_shell_executed`

Every shell hook run — including silently auto-approved runs — emits a `hook_shell_executed` P6 event (under the "tool" group), recording:

```
shell_exec: <command> [rc=N]
```

(`shell_push:` prefix for push-mode hooks.) The return code suffix is omitted when the command exits 0. This gives the operator a complete audit trail of shell-hook activity regardless of consent path.

## Configuration

Hooks are declared under the `hooks:` key in `reyn.yaml`. See the
[reyn-yaml reference § hooks block](../../reference/config/reyn-yaml.md#hooks-block)
for the full schema.

Brief example — a `turn_end` self-continuation `template_push`, a `session_start`
`shell_exec`, a `turn_end` `shell_push` whose stdout decides the push, and a
matcher-narrowed `mcp_resource_updated` `pipeline_launch`:

```yaml
hooks:
  - on: turn_end
    template_push:
      message: "Run complete. Check for pending tasks."
      wake: true

  - on: session_start
    shell_exec: "echo session-started >> /tmp/reyn-hooks.log"

  - name: dynamic
    on: turn_end
    shell_push: "scripts/decide-next.sh"   # emits {"push_when":true,"wake":true,"message":"..."}

  - on: mcp_resource_updated
    matcher: {server: "github", uri: "file:///repo/docs/**"}
    pipeline_launch:
      name: reindex_docs
      input_template: {uri: "{{ uri }}"}
```

The `wake: true` on the first hook triggers a new turn after each `turn_end`,
with the message injected as the system context. The `shell_exec` on
`session_start` appends a log line; its output is discarded. The `shell_push`
runs its command, parses stdout, and pushes only if the directive says so.
The last hook only fires for a `github`-server resource under
`docs/` — and, when it does, launches the `reindex_docs` pipeline
asynchronously with the changed URI as input.

## Async Bus and Composer — event correlation

Everything above is the **Sync** path: an awaited, per-hook dispatch at each
lifecycle/external point. reyn also has a per-Session **Async Bus** —
independent pub/sub broadcast of the same events, with no consume semantics
(every subscriber observes the same broadcast simultaneously) — and, built on
top of it, a **Composer** that correlates multiple events into one derived
"composed" event.

A **Composer** watches the Bus, buffers matching events per its configured
op, and — once the op's condition is met — publishes ONE new event with
`kind = "composed:<name>"` back to the same Bus. Seven ops:

| Op | Fires when |
|---|---|
| `all` | every one of N distinct inputs has arrived (per correlation key) |
| `any` | the first matching input arrives (stateless) |
| `seq` | the inputs' kinds arrive in the CONFIGURED order (an out-of-order arrival resets progress) |
| `window` | `ttl` seconds have elapsed since the FIRST matching event — fires with everything buffered |
| `debounce` | `ttl` seconds have elapsed since the LAST matching event with no newer one in between |
| `correlate_by` | like `all`, but keyed by a payload field (e.g. a request id) instead of one global bucket |
| `count` | `threshold` matching events have arrived (per key) |

```yaml
composers:
  - name: deploy_approved
    op: all
    inputs:
      - { kind: builtin:external:mcp_resource_updated, match: { server: "github" } }
      - { kind: mcp:approval-server:approved }
    policy: { capacity: 10, overflow: reject, ttl: 5m }
    emit: { kind: composed:deploy_approved }
```

**Correlate on payload, never on `source`.** Every event on the Bus carries
`source="builtin"` — `kind` already encodes the source TYPE
(`mcp_resource_updated` vs `file_changed` vs ...) and `payload` already
carries the source INSTANCE (`payload.server` / `payload.path` /
`payload.job_name` / `payload.transport`). A Composer input naming a
`source` other than `"builtin"` can never match anything the Bus carries and
is rejected at config-load time (the same typo-resistance posture the
matcher's schema validation already has for payload fields).

**Reliability posture — best-effort, not a recovery feature.** A Composer's
in-flight correlation state is held in memory only and is lost on a process
crash (a partially-matched `all`/`seq`/`correlate_by` simply never fires).
This is a deliberate v1 scope decision, not an oversight: the Bus itself is
already lossy under backpressure (a slow subscriber drops the oldest
unread event), so a Composer built on top of it cannot promise more
reliability than its input. Overflow of a Composer's own pending state
follows one of three policies — `drop_oldest` / `drop_newest` / `reject`
(no publisher-blocking backpressure) — and every drop, whether from
overflow or a `ttl`-aged incomplete correlation, is surfaced as a
`composer_dropped` P6 event (metadata only: composer name + correlation key
+ reason, never the buffered payload content) so a composition that quietly
never fires is never silent. A successful fire is `composer_fired`
(same metadata-only shape).

**Composed events reach Sync via a dedicated bridge, never `HookDispatcher.
dispatch()` itself.** A `composed:<name>` event is published ONLY to the
Bus by the Composer (invariant #5 above stays true — a Composer never calls
`HookDispatcher.dispatch()`/`hooks_for()` directly). Hook-Event Redesign
Phase 5 part 1 ([#2881](https://github.com/tya5/reyn/issues/2881)) opened
`composed:<name>` as a subscribable Sync `on:` target — a
`reyn.hooks.composed_consumer.ComposedEventConsumer` subscribes to the same
session `HookBus` and, for every observed `composed:*` event, runs any
Sync-registered hook whose `on:` names that kind
(`HookDispatcher.dispatch_bus_event`) — WITHOUT re-publishing to the bus
(re-broadcasting an already-bus-delivered event would double-deliver it to
any sibling Composer correlating on the same kind). A composer config that
would create a composition cycle (composer A depends on composer B's output
which depends on A's) is still rejected at config-load time, never
discovered at runtime — that DAG check is independent of, and unaffected
by, this Sync consumer.

```yaml
hooks:
  - on: composed:deploy_approved      # a composed event as a Sync on: target
    shell_exec: "reyn deploy.sh"

composers:
  - name: deploy_approved
    op: all
    inputs:
      - { kind: builtin:external:mcp_resource_updated, match: { server: "github" } }
      - { kind: mcp:approval-server:approved }
    policy: { capacity: 10, overflow: reject, ttl: 5m }
    emit: { kind: composed:deploy_approved }
```

A Session reads `composers:` from the SAME 4-layer additive combine as
`hooks:` (`reyn.yaml` startup ∪ `.reyn/config/hooks.yaml` runtime ∪
per-agent ∪ per-session) and starts every configured Composer automatically
(`start_composers`, called from `run()` alongside the filesystem watcher's
own start) — no manual wiring required. Composers are **startup-only**: a
config change takes effect on the next session start, not via the hooks
hot-reload seam (a live Composer's in-flight `PendingStore` correlation
state has no reload-time reconciliation yet).

**The composed→wake loop-valve bound.** A `composed:<name>` hook's
wake=true push lands in the inbox via the exact same `kind="hook"` E-path
every other hook-driven wake uses, so a self-stimulating composed→wake
chain (a composer counting a lifecycle point its own consumer hook's next
turn re-triggers — e.g. `turn_end`) is bounded by the session's existing
`max_hook_driven_turns` loop-valve with **zero new bounding logic**: every
wake path, composed→wake included, is counted by the same
`_hook_driven_turns` cap check. This is the architect-ratified "structural
non-reentry → valve-metered allow" transition (proposal
[0059](../../deep-dives/proposals/0059-hook-event-redesign.md) §9 item 3) —
pinned by a flip-witness Tier-2 test that drives a chain whose natural turn
count is unbounded and asserts the force-close fires at the cap.

## LLM-authored hook-events (`emit_hook_event`)

Everything above is fired by the OS (a lifecycle point, an external-event
source) or by a Composer's correlation logic. `emit_hook_event` is the ONE
Control-IR op that lets the LLM itself put an event onto its own session's
Bus — the first LLM-reachable producer in an otherwise OS-internal pipeline.

```json
{"kind": "emit_hook_event", "event_name": "deploy_ready", "payload": {"artifact": "build-42"}}
```

- `event_name` (str, required) — the emitted kind is ALWAYS
  `llm:<session_id>:<event_name>`; the session component comes solely from
  the caller's own session at execution time — there is no field the LLM
  can set to target a different session.
- `payload` (dict, optional, default `{}`) — carried on the event for a
  `matcher` / Composer to inspect. Never itself rendered into a hook
  message template by this op.

**The autonomy boundary — a static ALLOW-list, not a deny-list.** Only this
session's own `llm:<session_id>:*` namespace may ever be emitted:

- `builtin:*` is rejected — an LLM cannot spoof Reyn's own
  lifecycle/external-event kinds.
- `composed:*` is rejected — an LLM cannot spoof a Composer's *correlated*
  output; forging one would fire a `composed:*`-gated hook (e.g. an
  approval-gated deploy) without the Composer's actual correlation logic
  ever running.
- `webhook:*` / `mcp:*` (or any other session's `llm:*`) are rejected — an
  LLM cannot spoof external ingress or another session's identity.

**An emitted `llm:*` event only reaches a `hooks:` entry through a
Composer** — there is no direct Sync path from a raw `llm:*` Bus event to a
`hooks:` `on:` entry (only `composed:*` events are bridged to Sync dispatch;
see [Async Bus and Composer](#async-bus-and-composer-event-correlation)).
So `emit_hook_event`'s output is always consumed as a Composer
`inputs[].kind`, never directly as a hook's `on:`. **The `inputs[].kind`
value must name the actual session id**, not just the event name — the
default session's id is `main` (see [Sessions](../multi-agent/sessions.md)
for named/multi-session setups). Worked example — the LLM signals it
finished preparing a deploy (with the artifact id in `payload`); a Composer
waits for that alongside an external approval, then a Sync hook reacts to
the correlated result and reads the artifact id back out. Using `op: seq`
here (not `all`) is deliberate — it's the one op that GUARANTEES `inputs[]`
lands in the declared order, so `inputs[0]` is reliably the `llm:*` event's
payload, not whichever of the two happened to arrive first:

```yaml
composers:
  - name: deploy_approved
    op: seq                              # order-guaranteed — inputs[0] is always the llm:* event below
    inputs:
      - kind: llm:main:deploy_ready       # the default session's id is "main"
      - kind: mcp:approval-server:approved
    emit: { kind: composed:deploy_approved }

hooks:
  - on: composed:deploy_approved
    template_push:
      message: "Deploy artifact {{ inputs[0].artifact }} approved — proceeding."
      wake: false
```

## Deferred

The following capabilities are designed but not yet implemented:

- **Agent-level and phase-level hooks** — fine-grained points inside a turn
  (rare use cases; session/turn/task covers the common ones).
- **`WalBackedPendingStore`** — a crash-durable swap for a Composer's
  `PendingStore` seam (recovery-feature-gated: the moment it lands, CLAUDE.md's
  truncate-falsify PR gate applies). Composer pending state stays
  best-effort/crash-non-durable until then (by design, not an oversight —
  see the reliability posture above).
- **valve-persist** — `_hook_driven_turns` (the loop-valve counter) is
  in-memory-only (resets on crash); a separate, recovery-gated follow-up
  would make it snapshot-backed. Flagged as more load-bearing now that the
  Composer/Bus redesign adds new hook-driven-turn-generating paths.

## See also

- [Workspace](workspace.md) — the single source of truth that hook push messages land in
- [Events](events.md) — the P6 audit trail that records hook dispatch
- [Permission model](permission-model.md) — the consent flow for shell hooks
- [Sandbox](sandbox.md) — the backend-agnostic execution environment for shell hooks
- [reyn-yaml § hooks](../../reference/config/reyn-yaml.md#hooks-block) — full config reference
- [MCP § Resource subscriptions](../tools-integrations/mcp.md#resource-subscriptions-the-async-push-event-source) — the source of the `mcp_resource_updated` external-event point
- [Pipelines](pipelines.md) — what a `pipeline_launch` hook launches
