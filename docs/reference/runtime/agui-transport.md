# AG-UI transport — the thin-client wire protocol

Reyn's chat client is a stream-consuming UI: it draws a session's output and
routes user input, and it touches the session ONLY through a transport seam.
There are two transports behind that one seam — a local in-process transport,
and this **AG-UI transport** over HTTP + Server-Sent Events (SSE). Both feed the
identical renderer, so a remote client draws byte-for-byte what a local one does.

This page is the wire contract: the SSE endpoint, the reyn-frame ⇄ AG-UI-event
mapping, and the `STATE_*` status read-model.

## Surfaces

The transport speaks **AG-UI only** — it is a UI, not an agent. (Agent↔agent is
A2A; tools are MCP; observability export is OTEL. Those are separate surfaces.)

- `GET /agui/chat/{agent}/events` — the server→client SSE stream. Each SSE block
  is `event: <TYPE>\ndata: <json>\n\n`.
- `POST /agui/chat/{agent}` — the client→server channel. Body is a JSON object;
  the supported message types are:
  - `{"type": "user_message", "text": "..."}` — submit a turn.
  - `{"type": "TOOL_CALL_RESULT", "toolCallId": "<intervention-id>", "text": "..."}`
    or `{..., "choiceId": "<id>"}` — answer a pending intervention (the HITL
    round-trip; see "Human-in-the-loop answering" below).
  - `{"type": "cancel_inflight"}` — cooperatively cancel the in-flight turn (the
    Ctrl-C seam).
  - `{"type": "heartbeat"}` — a liveness keepalive.

  An input type the server does not model is a **graceful no-op** (a `200` ack),
  never a `500` — the server half of ignore-unknown.
- `POST /agui/chat/{agent}/seize` — take the active-driver token (see "Active
  driver and seize").

A client can never shut the server down — there is no shutdown message; a
client's `/quit` is a local disconnect only. The server is the sole writer.

A connection identifies itself with a `connection_id` query param (or an
`X-Reyn-Connection` header), stable across its SSE stream and its POSTs.

Both are gated by the server's authentication context: a connection presents its
token as `?token=` or an `Authorization: Bearer <token>` header (same-machine
UDS connections are identified by OS peer credentials instead). An
unauthenticated connection is refused with `401` before any session is attached.
The operator-facing command that opens this transport is `reyn chat --connect
<url>` (`--token <secret>` for the bearer token, falling back to the
`REYN_WEB_AUTH_TOKEN` environment variable).

## Standard envelope, reyn-private richness

Every event carries **both**:

- a **standard AG-UI field shape**, so a generic AG-UI client renders the
  interoperable core (text / tool / run / error / state); and
- a reyn-private `_reyn` reconstruction block, from which the reyn client rebuilds
  the exact render frame.

A generic client ignores what it does not understand: an event with no `_reyn`
block (or a reyn `CUSTOM` event a generic client does not model) is **skipped,
not fatal** — reyn owns this ignore-unknown contract.

## Event mapping

The client consumes one ordered SSE stream and dispatches each event back to one
of the renderer's two entry points (display vs working-indicator). The mapping:

### Display path (agent output → the scrollback)

| reyn display kind | AG-UI event        | Notes                                        |
|-------------------|--------------------|----------------------------------------------|
| `agent`           | text triplet       | the assistant reply text (see *text lifecycle*) |
| `status`          | text triplet       | transient status line (`role: status`)       |
| `reasoning`       | reasoning triplet  | the model's reasoning text (see *reasoning lifecycle*); emitted only when reasoning display is on |
| `error`           | `RUN_ERROR`        | error text                                   |
| `intervention`    | `CUSTOM`           | a prompt is displayed; the reyn client draws it natively and answers it by id (see "Human-in-the-loop answering") |
| `presentation`    | `CUSTOM`           | a `present` op's render-node model (see *present-on-wire*) |
| `__copy_last_reply__` / `__rewind_list__` | `CUSTOM` | client-consumed sentinels — forwarded (see *control sentinels*) |
| `__attach_request__` | `CUSTOM`        | fail-safe profile entry; upstream-consumed (see *control sentinels*) |
| `__end__` / `__session_switch_request__` | *(filtered)* | NOT forwarded (see *control sentinels*) |

Any other display kind still round-trips losslessly (it falls back to `CUSTOM` and
is reconstructed from `_reyn`) — a new display kind can never silently vanish on
the wire. The completeness gate that guarantees this enumerates the **authoritative
producer domain** — every `OutboxMessage(kind=...)` literal across the source
(direct constructions plus the call sites of kind-forwarder helpers), NOT a
renderer-file proxy — and asserts each producer kind is *standard-mapped*,
*profiled*, or *control-filtered*; anything else fails CI.

#### Control sentinels (forwarded vs filtered)

A few `__…__` display kinds get a **per-entry disposition**, decided by *where the
sentinel is consumed* (never by negating a forward-set, which would wrongly drop
renderable display kinds):

- **Client-consumed → forwarded** (profiled `CUSTOM`, `_reyn`-lossless):
  - `__copy_last_reply__` — `/copy`: the **client** does a real client-side
    clipboard copy over the transport stream.
  - `__rewind_list__` — `/rewind`: the **client** renders the rewind region picker.

  In the thin-client model the transport *is* the AG-UI wire, so filtering these
  would make remote `/copy` and `/rewind` silent no-ops — they must reach the wire.
- **Filtered** (`CONTROL_FILTER_KINDS`, an explicit allowlist — the emitter emits
  no wire event):
  - `__end__` — the stream terminator (the emitter returns on it; the client's
    loop also ends when the stream closes).
  - `__session_switch_request__` — already swallowed upstream (`registry.py:3061`),
    so it never reaches the AG-UI tap; filtering is a fail-safe.
- **Upstream-consumed → fail-safe profile**: `__attach_request__` is swallowed
  upstream (`registry.py:3052`) and never reaches the tap; its profile entry is a
  fail-safe for a future tap-point change, not a live wire kind. (Remote
  attach-label sync is designed separately, not via this legacy sentinel.)

#### Text lifecycle (the conforming triplet)

The AG-UI spec mandates the text lifecycle **`TEXT_MESSAGE_START` → one or more
`TEXT_MESSAGE_CONTENT` → `TEXT_MESSAGE_END`, all correlated by a `messageId`**; a
bare `TEXT_MESSAGE_CONTENT` is invalid (a strict generic client drops it). A whole
reyn text message therefore rides the wire as that triplet, with a generated
per-message id (reyn's outbox has no stable message id) and the CONTENT `delta`
carrying the full message text (reyn is whole-message; token-streaming is out of
scope).

Only the **CONTENT** event carries the `_reyn` reconstruction block; the START and
END events are generic scaffold that the reyn client decodes to `None` and
ignores. So the reconstruction invariant stays **one frame ⇄ one `_reyn`-bearing
event**, and the reyn client rebuilds exactly one display frame per message.

#### Reasoning lifecycle (the conforming triplet)

reyn's model reasoning rides the AG-UI **Reasoning** message lifecycle so a
generic client renders it as reasoning rather than as an opaque `CUSTOM` payload.
The canonical Reasoning category has seven events; reyn is whole-message (no token
streaming), so it maps the content-bearing inner triplet **`REASONING_MESSAGE_START`
→ `REASONING_MESSAGE_CONTENT` → `REASONING_MESSAGE_END`, correlated by a shared
`messageId`** with `role: "reasoning"` and the CONTENT `delta` carrying the whole
reasoning text. This mirrors the text triplet exactly: only the CONTENT event
carries the `_reyn` block (START/END decode to `None`), so the reyn client rebuilds
exactly one reasoning display frame and its render is byte-unchanged.

Two boundaries hold this signal in place:

- **Display-gate by construction.** A reasoning display frame only exists when
  the operator's reasoning-display toggle is on — reyn emits the frame at a single
  chokepoint gated on that toggle. Display off ⇒ no reasoning frame ⇒ zero
  `REASONING_*` events on the wire. The mapping adds no new gate and cannot become
  a chain-of-thought exposure path that bypasses the toggle.
- **Reasoning is a display signal, not observability.** The AG-UI display surface
  is an operator's connected client, where display-on is intent-to-see. Reasoning
  content is a transport-frame concern and is never routed to the observability
  export — the OTLP exporter keeps its content-off default and receives no
  reasoning chain-of-thought.

### Working-indicator path (turn lifecycle + tool axis)

| reyn chat-event               | AG-UI event      |
|-------------------------------|------------------|
| `turn_started`                | `RUN_STARTED`    |
| `turn_settled` / `turn_completed` / `turn_cancelled` | `RUN_FINISHED` |
| `tool_called`                 | `TOOL_CALL_START`|
| `tool_returned` / `tool_failed` | `TOOL_CALL_END` (with `status`) |
| `user_answered_intervention`  | `CUSTOM`         |

These eight are the exact set the renderer's working / running / waiting-for-you
indicator consumes; the transport forwards precisely this set.

`TOOL_CALL_END` carries a standard `status` field (`"ok"` / `"error"`) derived
from the etype — `tool_failed` → `"error"`, `tool_returned` → `"ok"` — so a
generic client sees a tool failure. The reyn client still exact-recovers the
precise etype from `_reyn`.

### Intervention frontend-tool

Alongside the display frame, the server emits a companion `TOOL_CALL_START`
**frontend-tool** whose `toolName` is `reyn.intervention.<kind>` and whose
`toolCallId` is the intervention id. A generic AG-UI client can render and
answer it as an ordinary tool call; the reyn client uses it only to know which
intervention is pending — it draws the prompt itself from the display frame,
so there is no double render. When the intervention resolves (answered or
denied) the server emits a terminal `TOOL_CALL_RESULT`, so a pending
frontend-tool never dangles.

## Human-in-the-loop answering

Answering an intervention IS a permission grant, so every answer is
authenticated AND authorized at delivery time. The client is untrusted: the
server re-authorizes the identity and validates the answer against its OWN
copy of the intervention (the id, and any choice id) — the client's echoed
prompt / choices are not trusted.

Answers are delivered **by id**: the `toolCallId` in a `TOOL_CALL_RESULT`
names the exact intervention the operator was shown, so a grant lands on that
prompt and never on a different queued one. An unknown or already-answered id
is rejected (the client falls back to an ordinary turn); there is no
answer-the-oldest fallback.

An authenticated human operator's answer is unfenced (treated as trusted
operator input). An answer arriving from an external agent peer over the
internal agent-to-agent path stays fenced (a different, untrusted trust
class).

Attribution: each answered grant is recorded on the audit trail with the
authenticated user id and the connection it came from; attach / seize / detach
are also audited.

## Active driver and seize

Multiple terminals may attach to one session and all see the same output.
Exactly one connection at a time holds the **active-driver token** — the
authority to answer / drive. This is a UX coordination token, not a security
control.

Any authorized connection may **seize** the token
(`POST /agui/chat/{agent}/seize`) with no handshake — the intended case is one
operator across a laptop and a desktop. The previous holder becomes a
non-holding equal peer and may seize back.

A seize is refused for an unauthenticated / unauthorized connection, or one
with no attached surface. A deposed holder's in-flight answer is rejected at
delivery (it is no longer the active driver).

## Fail-close and the grace window

A pending intervention must never hang forever waiting on an operator who has
gone. When the last answerable operator surface for an intervention is lost —
an in-process detach OR a network break / heartbeat timeout — the
intervention is resolved with a typed refusal (a fail-closed answer the run
continues from), never left parked.

This only happens after a **grace window**: a brief disconnect and reconnect
within the window keeps the intervention pending and resumes normally. Only a
full grace window with zero surfaces triggers the refusal.

A liveness signal (a periodic heartbeat) means a half-open connection cannot
hide a dead surface: a surface that stops heart-beating past the liveness
timeout is detected as lost.

The heartbeat POST is a **half-open backstop only** — a normal disconnect (the
client closes cleanly) is caught immediately by the SSE handler's own
`finally: manager.detach(...)`, not the heartbeat. The dedicated ping only
matters for a client that hangs without ever sending a TCP FIN. The remote thin
client (`reyn chat --connect`) sends a heartbeat every 25s
(`REYN_AGUI_HEARTBEAT_INTERVAL_S` overrides it), skipping the dedicated ping
whenever a real client→server POST (a turn, an answer, a cancel) already
landed within that window — piggybacking on real traffic instead of adding
redundant load. The server's liveness timeout is 60s
(`REYN_AGUI_LIVENESS_TIMEOUT_S` overrides it) — comfortably above the client
interval (the idiomatic ratio: Socket.IO 25s/60s, Phoenix 30s, SignalR
15s+2×timeout) so a live, idle client is never false-swept as dead. The client
interval MUST stay below the server timeout, which in turn stays below
timeout+grace, so the half-open backstop and the grace window together always
cover detection.

The refusal is scoped **per intervention**: an intervention still answerable
by another live surface (for example one an external agent peer is answering)
is left pending even when the operator terminals are all gone.

## present-on-wire

A `present` op's render model is a `list[dict]` of render nodes, **neutralized at
construction** (every leaf string stripped of terminal control / ESC sequences),
so it is inert before it reaches any wire. It rides a `CUSTOM` event under the
`presentation` display kind, carried in `meta.nodes`.

The AG-UI client additionally re-runs the surface neutralizer over every node
leaf **at the transport edge**, per connection — idempotent for a leaf the
construction seam already neutralized, but load-bearing defense-in-depth for a
heterogeneous-surface client whose upstream did not neutralize (or neutralized
for a different surface).

## STATE_* — the status read-model

The status bar (attached agent, model, cost, tokens, context usage, and the
current WaitingOn label) is a **read-model**, not a file mirror: it is derived
from the session's live cost / token / context accessors and the working-indicator
state, and only the render-relevant subset is streamed.

- `STATE_SNAPSHOT` — emitted **on connect**, the full read-model. Fields:
  `attached_name`, `model`, `cost_agent`, `cost_total`, `agent_tokens`,
  `ctx_used`, `ctx_window`, `waiting_on`.
- `STATE_DELTA` — emitted **on change**, carrying only the changed keys. An idle
  stream emits no deltas.

The client seeds its status view from the snapshot and merges each delta, so the
remote status panel always reflects the server's values.

These are exactly the **main status-bar chip values** the inline CUI renders, so a
remote client on an interactive TTY draws the same status bar as a local one
(`agent` · `model` · `cost` · `ctx%`, plus the working indicator). The **dropdown
expansions** (cost/ctx detail, the `/model` class picker, the agent/session tree,
the task tree, the `…` overflow toggle counts), the interactive intervention /
`/rewind` **pickers**, and the **`task` chip count** are session-local state, not
on the wire — a remote client shows the streamed chip values and degrades those to
empty/`—`/0. (The `task` chip is degraded rather than streamed because the task
system is a deprecation candidate — deliberately no per-connection poll; adding any
other field is an additive `STATE_*` key, not a client change.)

## Reconnect

On connect (or reconnect) the server replays, before any live event:

1. `MESSAGES_SNAPSHOT` — the display backlog (the messages already produced), so
   a reconnecting client rebuilds its scrollback; then
2. `STATE_SNAPSHOT` — the status read-model above.

Live events (and `STATE_DELTA`s) follow.

The `MESSAGES_SNAPSHOT` `messages` field is a **standard `[{role, content}]`
array of conversation turns only** — `agent` → `assistant`, `user` → `user` — the
shape a generic client expects. reyn chrome (status / error / present /
intervention / trace) is not a conversation turn and is excluded from the standard
array; the reyn client rebuilds the full backlog (chrome included) from the
`_reyn` block, so its scrollback is unchanged.

## The reyn extension profile

Beyond the interoperable core, reyn names its own vocabulary under a reyn-owned
namespace — the `CUSTOM`-event `name` for chrome with no standard analog, and the
frontend-tool `toolName` for interventions. This namespace is a **documented,
tested extension profile**: every `reyn.*` name reyn emits has a registry entry. A
completeness gate enumerates the **authoritative producer domain** — every
`OutboxMessage(kind=...)` literal across the source (direct constructions plus the
call sites of kind-forwarder helpers), plus the intervention frontend-tool encoder
— and asserts each producer kind is *standard-mapped*, *profiled*, or
*control-filtered*, so the profile cannot silently drift from what the codec puts
on the wire.

Three namespaces:

### `reyn.display.<kind>`

A reyn display frame with no standard AG-UI analog. `value` is `{"text": <string>}`
— the display line text.

| Custom `name`                     | Meaning                                              |
|-----------------------------------|------------------------------------------------------|
| `reyn.display.intervention`       | an intervention prompt is displayed                   |
| `reyn.display.presentation`       | a `present` op's text; the render-node model rides the `_reyn` block's `meta.nodes` (inert on the wire — see *present-on-wire*) |
| `reyn.display.user`               | a user-authored line — a submitted turn OR a resolved intervention answer — broadcast (via the outbox fan-out, same as agent output) to EVERY attached client, not only the one that produced it; `meta` optionally carries `auth_user_id` / `auth_connection_id` attribution for a multi-client render (backlog user turns ride the standard `messages` array instead) |
| `reyn.display.system`             | a reyn chrome line — a persisted lifecycle/status marker (compaction / budget / cost-warn) |
| `reyn.display.__copy_last_reply__` | the `/copy` sentinel — forwarded (client-side clipboard copy); see *control sentinels* |
| `reyn.display.__rewind_list__`    | the `/rewind` sentinel — forwarded (client-side rewind picker); see *control sentinels* |
| `reyn.display.__attach_request__` | the attach-request sentinel — a fail-safe profile entry (upstream-consumed); see *control sentinels* |
| `reyn.display.tool_call_started`  | a tool-call start trace line                           |
| `reyn.display.tool_call_completed`| a tool-call completion trace line                     |
| `reyn.display.tool_call_failed`   | a tool-call failure trace line                        |

### `reyn.event.<etype>`

A reyn chat-event (working-indicator axis) with no standard AG-UI analog. `value`
is the event's data object.

| Custom `name`                        | Meaning                                          |
|--------------------------------------|--------------------------------------------------|
| `reyn.event.user_answered_intervention` | the user answered an intervention             |

### `reyn.intervention.<kind>`

An **open namespace** carried differently from the two above: it is the `toolName`
of the HITL **frontend-tool** `TOOL_CALL_START` (a standard event, not a `CUSTOM`
one — see *Intervention frontend-tool*), so a generic client can render and answer
an intervention as an ordinary tool call. `<kind>` is the intervention kind
(`ask_user`, `permission.*`, …) — caller-supplied, so this is profiled at the
**namespace** level (fixed value schema), not as a closed member set.

- **`toolCallId`** — the intervention id (the answer-correlation anchor a client
  echoes back verbatim in a `TOOL_CALL_RESULT`).
- **`args`** — `{prompt, detail, choices, suggestions}`, what a generic client
  renders to pose the question.

The `reyn.display.*` and `reyn.event.*` namespaces above are `CUSTOM`-event names a
generic client ignores (skipped, not fatal); the reyn client reconstructs the exact
frame from the `_reyn` block. An unknown `reyn.*` name a client predates is likewise
skipped, not fatal.

## Local ≡ remote

The server serializes the SAME unified frame stream the local in-process
transport produces (display outbox + the renderer-relevant chat-event subset).
The AG-UI transport adds only wire framing, never new render semantics — so the
remote renderer's display bytes and working-indicator transitions are identical
to the local ones.

Local ≡ remote holds at the **renderer/loop layer**, not just the transport. The
renderer choice (Claude Code-style inline CUI on an interactive TTY, plain console
for `--cui` / non-TTY / piped) is one shared seam (`logger_factory.make_renderer`
behind the `_inline_interactive` predicate), and both `reyn chat` and `reyn chat
--connect` hand a `ClientTransport` + a `ChatReadModel` to the SAME driver
(`client_driver.run_chat_client`). The client reads its status bar / intervention
region / task poll through the read-model: a `RegistryReadModel` off the local
session, or a `RemoteReadModel` off the `STATE_*` view above — so an interactive
remote attach renders the inline CUI, not a plain fallback.

**Local ≡ remote holds for INPUT too, symmetric with output.** A submitted turn
(`Session.submit_user_text`) and a resolved intervention answer
(`InterventionHandler.deliver_answer_to` — the one funnel every answer path
shares: TUI free-text, TUI choice-region, an A2A peer, and the AG-UI HITL
round-trip above) each put a `kind="user"` frame on the SAME `session.outbox`
the agent's reply rides, so it fans out through the identical `OutboxHub`
broadcast to every attached surface. The submitting client renders its own
line from that broadcast frame too (no separate local echo) — with 2+ clients
attached, everyone sees every turn and every answer, not only the agent's
replies to them.

## AG-UI event coverage — reading the numbers honestly

**Frame loss is zero and reyn-client fidelity is 100%, regardless of the
numbers below.** Every event carries the reyn-private `_reyn` reconstruction
block (see *Standard envelope, reyn-private richness* above); the reyn client
always recovers the exact original frame from it. The coverage figures in this
section describe something different: **how much of the AG-UI *standard*
event vocabulary** — the signal a *generic*, non-reyn AG-UI client can render
without any reyn-specific knowledge — reyn currently emits natively, as
opposed to folding into a `CUSTOM` event a generic client has to skip. A low
number here is a statement about generic-client richness, not about data
loss.

| Category   | Standard events | reyn-mapped | Disposition |
|------------|-----------------|-------------|--------------|
| State      | 3                | 3           | **complete** |
| Lifecycle  | 5                | 3           | **intentional-scope** — the 2 Step events fold into the `STATE_*` read-model's `waiting_on` field instead of a separate standard event (see *STATE_\* — the status read-model* above) |
| Tool       | 5                | 3           | **complete for the HITL round-trip** — `TOOL_CALL_START` + `TOOL_CALL_END` (with a standard `status` field) + `TOOL_CALL_RESULT` (the intervention frontend-tool answer round-trip); the `TOOL_CALL_ARGS`/`_CHUNK` pair is **intentional-scope** (a tool call is already complete by the time reyn emits it — there is no in-flight args stream to chunk) |
| Text       | 4                | 3           | **conforming triplet** — a whole message rides `TEXT_MESSAGE_START` → `TEXT_MESSAGE_CONTENT` → `TEXT_MESSAGE_END`, correlated by `messageId`; only the streaming `TEXT_MESSAGE_CHUNK` is unmapped (**intentional-scope** — reyn's outbox delivers whole messages, not token deltas) |
| Special    | 2                | 1           | **intentional-scope** — reyn-private payloads are always structured (`CUSTOM`); the standard `RAW` passthrough event has no reyn use case |
| Activity   | 2                | 0           | **intentional-scope** — reyn has no direct analog; the same information is already carried by the frame stream + `STATE_*` |
| Reasoning  | 7                | 3           | **standard-mapped** — a whole reasoning message rides `REASONING_MESSAGE_START` → `REASONING_MESSAGE_CONTENT` → `REASONING_MESSAGE_END`, correlated by `messageId`; the outer `REASONING_START`/`REASONING_END` context wrapper and the streaming `REASONING_MESSAGE_CHUNK`/`REASONING_ENCRYPTED_VALUE` variants are **intentional-scope** (reyn is whole-message; no encrypted CoT) |

**Totals**: reyn natively emits **15 of the 28** active-roster standard events
(16/28 counting the `CUSTOM` catch-all itself as one). The 28-event roster is
Lifecycle (5) + Text (4) + Tool (5) + State (3) + Activity (2) + Reasoning (7)
+ Special (2), tallied from the canonical AG-UI event reference
(<https://docs.ag-ui.com/concepts/events>). That reference self-reports up to
~34 event names in total when meta/deprecated/draft entries outside the
active roster are counted — the exact figure is spec-version dependent, so
this page tracks the 28-event active roster, not the larger number.

### Why the gaps are dispositioned the way they are

- **Reasoning (standard-mapped).** reyn treats reasoning as a first-class
  concept, and a reasoning display frame now maps to the standard reasoning
  message triplet (`REASONING_MESSAGE_START` → `REASONING_MESSAGE_CONTENT` →
  `REASONING_MESSAGE_END`), so a generic AG-UI client renders it directly
  instead of skipping a `CUSTOM` payload. Two boundaries are respected (see
  *reasoning lifecycle*): the **reasoning-display toggle** is honored by
  construction — a reasoning frame only exists when display is on, so display
  off ⇒ zero `REASONING_*` events, and the mapping adds no new gate — and the
  reasoning chain-of-thought stays a display signal only, never routed to the
  observability export (the OTLP content-off default is unaffected). The outer
  `REASONING_START`/`REASONING_END` wrapper and the streaming chunk/encrypted
  variants are intentional-scope (reyn is whole-message).
- **Everything marked intentional-scope** reflects a real architectural
  difference (reyn's whole-message outbox, structured-only private payloads,
  no in-flight tool-args phase, no direct "activity" concept) rather than an
  oversight — closing these gaps would mean inventing streaming/chunking
  machinery reyn's design deliberately does not have, not fixing a bug.
