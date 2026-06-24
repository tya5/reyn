---
type: concept
topic: runtime
audience: [human, agent]
---

# Agent lifecycle hooks

Hooks are a thin operator- and skill-scoped layer that lets you inject context,
trigger self-continuation, or run a sandboxed side-effect at any of the eight
lifecycle points in a reyn session.

They are built on two mechanisms that already exist: the **unified inbox** (the
channel that feeds messages into a turn) and the **P6 lifecycle** (the event
stream). No new OS machinery — a new skill that uses hooks does not require any
OS change (P7).

## Lifecycle points

Hooks fire at eight points, one for each combination of scope and direction:

| Scope   | `_start` | `_end` |
|---------|----------|--------|
| session | `session_start` | `session_end` |
| turn    | `turn_start` | `turn_end` |
| skill   | `skill_start` | `skill_end` |
| task    | `task_start` | `task_end` |

Every point is an **awaited dispatch**: the hook completes (shell exits, push is
queued) before the lifecycle point continues. This is what gives shell hooks
synchronous access to the moment — a session_start shell hook finishes before
the first turn begins.

Implementation anchors:

- `turn_end` fires at the terminal `stop_reason`
- `skill_start` / `skill_end` fire at `SkillRegistry.start()` / `.complete()`
- `task_start` fires at the `_create` Control IR op; `task_end` fires at
  `_update_status` (status → completed) AND at `_abort` (status → aborted) —
  every task that starts is guaranteed a matching `task_end` regardless of how it terminates
- `skill_end` currently fires only on clean completion — interrupt and error are
  deferred to [#2068](https://github.com/tya5/reyn/issues/2068)

## Three config schemes

Each entry carries **exactly one** of three mutually-exclusive schemes:

- **`template_push`** — a push directive built from config Jinja2 templates.
- **`shell_exec`** — a sandboxed command run as a pure side-effect (output ignored).
- **`shell_push`** — a sandboxed command whose **stdout is a JSON push-directive**,
  pushed via the same path as `template_push` (the only difference is the
  directive's source: captured stdout vs a Jinja2 render).

## Three capabilities

Those schemes deliver three behavioral capabilities, uniformly:

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
the lifecycle point always proceeds. `session` is parsed and carried for
forward-compatibility, but cross-session routing is not yet wired (a no-op
today; the dispatcher pushes to the current session).

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
unexpected skill behavior would otherwise leave open.

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
`shell_exec`, and a `turn_end` `shell_push` whose stdout decides the push:

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
```

The `wake: true` on the first hook triggers a new turn after each `turn_end`,
with the message injected as the system context. The `shell_exec` on
`session_start` appends a log line; its output is discarded. The `shell_push`
runs its command, parses stdout, and pushes only if the directive says so.

## Deferred

The following capabilities are designed but not yet implemented:

- **Cross-session push** — a push directive's `session` field is parsed and
  carried (both `template_push` and `shell_push`) but routing it to *another*
  session's inbox is not yet wired; today a push always lands in the current
  session.
- **Agent-level and phase-level hooks** — fine-grained points inside a turn
  (rare use cases; session/turn/skill/task covers the common ones).
- **`skill_end` on interrupt or error** — `skill_end` currently fires on clean
  completion only. Error and interrupt paths are tracked in
  [#2068](https://github.com/tya5/reyn/issues/2068).

## See also

- [Principles](../architecture/principles.md) — P5 (workspace), P6 (events), P7 (no skill strings in OS)
- [Workspace](workspace.md) — the single source of truth that hook push messages land in
- [Events](events.md) — the P6 audit trail that records hook dispatch
- [Permission model](permission-model.md) — the consent flow for shell hooks
- [Sandbox](sandbox.md) — the backend-agnostic execution environment for shell hooks
- [reyn-yaml § hooks](../../reference/config/reyn-yaml.md#hooks-block) — full config reference
