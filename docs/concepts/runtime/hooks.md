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
at least one path under `fs_watch.paths` in `reyn.yaml` — see
[reyn-yaml § `fs_watch` block](../../reference/config/reyn-yaml.md#fs_watch-block).
Without either, the feature is off (a clear warning is logged once if paths
are configured but the extra is missing; no config at all is silently
byte-identical to a build with no watcher).

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
- A field named in the matcher that the firing event doesn't carry (e.g. a
  lifecycle point's vars have no `server`/`uri`) **never matches** — a
  matcher can only narrow an event source, never invent a signal that was
  never fired.
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

- **TUI session** — consent routes through the unified intervention bus and surfaces as a **Pending-tab modal**: "Shell hook `<name>` wants to run a command" (the hook's configured `name:` field, or a generic message if unnamed). Three choices:
  - **[A]lways** — allow and persist to the allowlist (`~/.reyn/shell-hooks-allowlist.json`, override via `REYN_SHELL_HOOKS_ALLOWLIST`). Future runs of the same command are auto-approved.
  - **[y]es** — allow this run only.
  - **[n]o** — skip (fail-closed).
- **Non-TUI** (`reyn run`, `mcp-serve`, headless) — falls back to the pre-bus behavior: TTY stdin prompt when available, or refused when stdin is not a TTY.
- **Allowlist hit** — any command already in the allowlist runs silently without a prompt (auto-approved on all surfaces).

Consent is fail-closed throughout: if the sandbox backend cannot be confirmed, the hook is refused rather than run unsandboxed.

See [sandbox](sandbox.md) for the full backend model and [permission model](permission-model.md) for the broader consent architecture.

### P6 event: `hook_shell_executed`

Every shell hook run — including silently auto-approved runs — emits a `hook_shell_executed` P6 event. This event surfaces in the TUI **Events tab** (under the "tool" group) as:

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

## Deferred

The following capabilities are designed but not yet implemented:

- **Agent-level and phase-level hooks** — fine-grained points inside a turn
  (rare use cases; session/turn/task covers the common ones).

## See also

- [Workspace](workspace.md) — the single source of truth that hook push messages land in
- [Events](events.md) — the P6 audit trail that records hook dispatch
- [Permission model](permission-model.md) — the consent flow for shell hooks
- [Sandbox](sandbox.md) — the backend-agnostic execution environment for shell hooks
- [reyn-yaml § hooks](../../reference/config/reyn-yaml.md#hooks-block) — full config reference
- [MCP § Resource subscriptions](../tools-integrations/mcp.md#resource-subscriptions-the-async-push-event-source) — the source of the `mcp_resource_updated` external-event point
- [Pipelines](pipelines.md) — what a `pipeline_launch` hook launches
