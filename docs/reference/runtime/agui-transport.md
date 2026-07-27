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
  - `{"type": "user_message", "text": "..."}` — submit a turn. The response
    body is `{"status": "ok", "msg_id": "..."}` (#3287) — the same correlation
    id the broadcast `reyn.event.user_submitted` carries, letting the
    submitting client recognise its own echo BY ID (see "Local ≡ remote holds
    for INPUT too" below).
  - `{"type": "TOOL_CALL_RESULT", "toolCallId": "<intervention-id>", "text": "..."}`
    or `{..., "choiceId": "<id>"}` — answer a pending intervention (the HITL
    round-trip; see "Human-in-the-loop answering" below).
  - `{"type": "cancel_inflight"}` — cooperatively cancel the in-flight turn (the
    Ctrl-C seam).
  - `{"type": "cancel_queued", "msg_id": "..."}` — cancel-by-id an UNDISPATCHED
    (queued, not-yet-running) inbox message (#3300 P3). A DIFFERENT intent from
    `cancel_inflight` above: this targets one specific queued item that has not
    started a turn yet, never the currently-running turn. Server-side semantics
    (`Session.cancel_queued`): queued → removed (WAL `inbox_cancel` tombstone +
    synchronous snapshot-prune, then an `inbox_cancel` chat-event delta, see
    "STATE_* — the status read-model" and `reyn.event.inbox_cancel` below);
    already dispatched → a no-op (never escalated to `cancel_inflight`);
    idempotent (a second cancel of the same id is a no-op, safe for an
    at-most-once retry).
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

#### The session-switch barrier (`reyn.event.session_attached`, #3310 N1/N2)

`/attach <name>` and `/session switch <sid>` both flip which session's frames
reach a client — but historically nothing told the client THAT a switch had
happened (the old Textual TUI's header re-post was deleted as dead code; see
the control-sentinel dispositions above). `AgentRegistry.attach`/
`attach_session` now emit a `session_attached` chat-event carrying
`{agent, session_id}` — the identity a client keys its display/reset cache on
— as an `EventFrame` put DIRECTLY on `repl_outbox` (`registry.py`, the
`_announce_session_attached` helper), never routed through the just-swapped
session's own chat-events.

The barrier property is the point: the `self._attached = key` flip and the
`repl_outbox.put_nowait(...)` happen with NO `await` in between (both are
plain synchronous statements). A single event loop can never interleave
inside that synchronous region, so on the `repl_outbox` FIFO "before this
frame = old session's frames, after = new session's frames" holds BY
CONSTRUCTION, not merely by convention — mirroring the no-await
critical-section idiom `Session.cancel_queued` uses (#3300 Y-server /
#3306). `InProcessTransport._pump_outbox` passes an `EventFrame` already on
`repl_outbox` through unchanged (never re-wraps it as a `DisplayFrame`).

This event is NOT an `OutboxMessage` display kind — the owner-ratified reason
is the same as #3288 ③b's `agent_delta`: a state transition rides an `EventFrame`
(opt-in draw — a surface with no handler drops it silently, never a garbage
row), where registering a closed-vocabulary display kind for it would be a
category error.

**N2 (client-side reset + hydrate, local — `textual_chat/app.py`).**
`TextualChatApp._handle_session_attached_event` consumes this event as the
reset barrier: on receipt it clears every per-session client-side state
(the retained `FlowModel`, running-tool tracking, pending-intervention
tabs, the sent-queue view/widget + its item-meta side table, in-flight
streamed-reply tracking) and rehydrates the retained model from the NEW
session's `history.jsonl` — **not** from a retained cache. A cached
`FlowView` cannot be the source of truth here: while a session is
detached, the registry forwarder *drops* its frames entirely (see the
control-sentinel dispositions above), so a cache would be missing
everything that happened meanwhile and would hold tool rows stuck
RUNNING. `ChatReadModel.conversation_history` (`interfaces/repl/
read_model.py`) is generalized to accept an optional `(agent,
session_id)` target — `None`/`None` (the pre-N2 shape) still hydrates
whichever session is currently attached; a target hydrates that specific
session instead, resolved via `AgentRegistry.get_session` (never a
duplicated `history.jsonl` path literal). Pending interventions are
*forgotten*, not re-fetched — the registry's `attach`/`attach_session`
already re-announce every pending intervention on attach, so the client
only needs to stop tracking the old session's entries.

**Remote parity (#3310 N3).** `registry.repl_outbox` (above) is a LOCAL-only
bus — the AG-UI/SSE `_SessionFrameSource` never drains it; it reads a
session's own `outbox_hub`/`chat_events` directly, bound to ONE session
object for the SSE connection's lifetime. A remote client that switched
sessions therefore had NO way to obtain the new session's scrollback at all:
the remote read-model's `conversation_history` is deliberately empty
(frame-sufficiency, `read_model.py`) and the emitter's `MESSAGES_SNAPSHOT`
backlog was otherwise fixed at connect time. N3 closes this by treating a
switch as a **logical reconnect**, entirely within the remote transport
(the registry's own `attach`/`attach_session` + `repl_outbox` barrier are
untouched and not involved):

- `_SessionFrameSource` (`endpoint.py`) independently observes the SAME
  `__session_switch_request__` sentinel the registry forwarder consumes for
  the local REPL (both are subscribers of the same session's `outbox_hub`
  fan-out — see *the control-sentinel dispositions* above). On seeing it, it
  **enqueues the `session_attached` `EventFrame` onto this connection's own
  queue BEFORE re-pointing itself at the target session** (`registry.get_session`
  + subscribing to the new session's chat-events) — the SAME vocabulary N1
  defined, an independent per-connection equivalent since `repl_outbox` never
  reaches a remote surface. This ordering (announce, then subscribe) is
  load-bearing, not incidental: the new session's chat-event subscriber does
  not exist yet when the announce is enqueued, so a chat-event the new
  session emits cannot possibly reach this connection's queue ahead of the
  barrier — true BY CONSTRUCTION regardless of whether an `await` is ever
  later introduced between the two steps (witnessed by
  `test_switch_announce_precedes_any_new_session_chat_event`, an adversary
  that floods the target session's own chat-event stream the instant the
  switch is triggered). It never calls `registry.attach_session` itself, so
  it cannot race or double-apply that side effect — it only re-points THIS
  connection's own view. A registry-less construction (every pre-N3 unit test)
  degrades byte-identically: the sentinel falls through to the generic
  `DisplayFrame` path, where `CONTROL_FILTER_KINDS` already drops it silently.
- `AgUiEmitter`, on observing a `session_attached` `EventFrame` flow through
  `stream()`, re-fires the SAME reconnect protocol it uses at connect
  (`_reconnect_snapshot_chunks`: `MESSAGES_SNAPSHOT` then `STATE_SNAPSHOT`)
  — STRICTLY after the barrier event is forwarded, never before, so a client
  that resets its view on the barrier never sees the reset race the state
  the re-fire is about to deliver. The new backlog is resolved via a
  caller-supplied `backlog_provider(agent, session_id)`; the endpoint wires
  `session_backlog_frames`, which projects the target session's in-memory
  `history` (`ChatMessage` list) through the SAME `project_restored_frames`
  SSoT local restore-on-restart uses (#3273 P5) — read fresh at switch time,
  never cached, so content that accrued in the other session while this
  connection was elsewhere is included (no staleness). The per-connection
  `TextStreamTracker` and `waiting_on` label are reset at the same point.
- No per-client "which frames have I already seen" bookkeeping was
  introduced: the mechanism is re-subscription (which session a connection
  currently reads from) plus a fresh history read at switch time — never a
  set of previously-delivered frame/message ids consulted before forwarding.
- An ordinary connection that never switches sessions is unaffected — the
  re-fire is dormant with no `session_attached` event ever flowing through.

Both consumers are now landed: **N1** provides the barrier event, **N2**
consumes it on the LOCAL path (`InProcessTransport` → `TextualChatApp`,
via the client-pull `ChatReadModel.conversation_history` seam), **N3**
consumes it on the REMOTE path (`_SessionFrameSource`/`AgUiEmitter`,
above, via a SERVER-push `backlog_provider` that re-derives the target
session's backlog directly through `project_restored_frames` — a
structurally separate mechanism from N2's client-pull, not a shared
call site, since a remote connection has no local `ChatReadModel` to
pull through). The net effect is the same for both surfaces: a switch
resets the client's view and repopulates it from that session's
authoritative history, never a retained cache.

#### Text lifecycle (the conforming triplet, plain and streamed)

The AG-UI spec mandates the text lifecycle **`TEXT_MESSAGE_START` → one or more
`TEXT_MESSAGE_CONTENT` → `TEXT_MESSAGE_END`, all correlated by a `messageId`**; a
bare `TEXT_MESSAGE_CONTENT` is invalid (a strict generic client drops it).

Streaming applies to the **narrative reply call path only**. `RouterLoop` has
three production call sites for `call_llm_tools`, but only the primary reply
(`run_loop`) passes `on_content_delta`; `_run_structured_answer_turn` (a
schema-constrained turn whose output is parsed with `json.loads` — partial
JSON is unparseable, so streaming it would be meaningless) and
`_force_close_call` (a terminal wrap-up, not body content) both omit it
intentionally.

**A message that never streamed** (no provider capability, ADR-0039 P3a/③a) rides
the wire as the plain whole-message triplet, with a generated per-message id
(reyn's outbox has no stable message id) and the single CONTENT's `delta`
carrying the full message text. Only the **CONTENT** event carries the `_reyn`
reconstruction block; START/END are generic scaffold the reyn client decodes to
`None` and ignores — the reconstruction invariant is **one frame ⇄ one
`_reyn`-bearing event**.

**A message that DID stream** (#3288 ③b emits `reyn.event.agent_delta` for each
raw LLM content-delta as it arrives; #3288 ③d maps it onto the STANDARD text
surface) instead gets a REAL multi-CONTENT sequence: `TEXT_MESSAGE_START` at the
first delta, one genuine `TEXT_MESSAGE_CONTENT` per delta (each carrying its OWN
`_reyn`, reconstructing that exact `agent_delta` chat-event — so a reyn client's
in-flight rendering, if any, is identical whether the frames arrived in-process
or over this wire), then `TEXT_MESSAGE_END` at completion. ★The completion is
mapped to **END ONLY — never a second CONTENT re-sending the full text** (a
client that rendered the deltas live would double-render the body). The END
instead carries its OWN `_reyn`, holding the completion's FULL persisted text —
the **sole reconstruction authority** for a streamed message; a reyn client never
reconstructs by concatenating deltas (they are non-persistent, derived,
live-only narration — the reconnect `MESSAGES_SNAPSHOT` backlog, built only from
persisted `OutboxMessage`s, never reads one either). This is also what closes the
**late-joiner window**: a connection's per-connection `TextStreamTracker` state
(`interfaces/transport/agui/emitter.py`) reflects only what THAT connection
personally observed — a connection that witnessed zero deltas for a chain (never
connected during the stream, or connected right as it finished) instead gets the
unchanged plain whole-message triplet for the SAME completion frame (full text
on CONTENT), so either way the client ends up with the complete, persisted text.
No per-client "which deltas did you receive" bookkeeping is kept (rejected in the
issue #3288 ③d design thread — it would add state for no benefit over reading the
authoritative completion).

So the reconstruction invariant is **re-decided for a streamed message
specifically**: N `agent_delta` CONTENT events each carry their OWN `_reyn` (one
per delta), and the terminal END carries a further, DISTINCT `_reyn` (the
completion). N+1 `_reyn`-bearing events, not 1 — but the client's reconstruction
authority is always the LAST one. A message that never streamed is unaffected by
any of this (the plain triplet above, unchanged).

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
  `ctx_used`, `ctx_window`, `waiting_on`, `queue`, `turn_active`,
  `halted_reason`.
- `STATE_DELTA` — emitted **on change**, carrying only the changed keys. An idle
  stream emits no deltas.

`halted_reason` (#2280) is `Session.halted_reason` — `None` while running, or
the fail-stop reason (e.g. `"durability_failure"`) once the session has
halted on a persistent durability failure (#2259). Riding this same
snapshot+delta channel gives a remote client the SAME proactive surface the
local TUI status line and plain `--cui` bottom toolbar show — the halt is
already enforced synchronously elsewhere (`DurabilityHaltError`); this field
is observability only, never load-bearing for the halt itself.

`queue` and `turn_active` (#3300 P2a) publish the server-authoritative
**sent-queue state**: `queue` is the current undispatched inbox queue (each
item `{msg_id, chain_id, text}` — `Session.queued_user_messages()`), and
`turn_active` is whether a turn is currently dispatched
(`Session.turn_active`). Riding the same snapshot+delta channel makes a client
**late-joiner-safe**: connecting mid-turn (having missed the `turn_started`
chat-event that dispatched the in-flight item) still gets the correct queue +
turn-active state from the snapshot, not a partial event-derived guess. P2a
publishes this state only — rendering it as a sent-queue widget is P2b.

An item leaves `queue` via one of two mutually-exclusive granular chat-event
deltas on the same snapshot+delta channel — `turn_started` (dispatched; see
"Working-indicator path" below) or `inbox_cancel` (cancelled by id via the
`cancel_queued` client message above, #3300 P3): the server's own atomic
queued/dispatched judgement guarantees exactly one of the two ever fires for a
given item, never both. `inbox_cancel` carries `msg_id` + `seq` (the same
order-race-gate token `user_submitted`/`turn_started` carry — see
`reyn.event.inbox_cancel` below); a client merging the granular deltas removes
the item by `msg_id` (unlike `turn_started`, which matches by `chain_id`).

The client seeds its status view from the snapshot and merges each delta, so the
remote status panel always reflects the server's values.

These are exactly the **main status-line values** the interactive TUI renders, so a
remote client on an interactive TTY draws the same status line as a local one
(`agent` · `model` · `cost` · `ctx%`, plus the working indicator). The **drawer
panes** behind that line (the cost breakdown and ctx/compaction detail, the
`/model` class picker, the agent/session tree, the tool/mcp/skill/hook visibility
and applicability toggles, the pipeline and cron listings) and the interactive
intervention / `/rewind` **pickers** are session-local state, not on the wire — a
remote client shows the streamed status values and degrades those to empty/`—`/0.
Adding any other field is an additive `STATE_*` key, not a client change.

## Reconnect

On connect (or reconnect) the server replays, before any live event:

1. `MESSAGES_SNAPSHOT` — the display backlog (the messages already produced), so
   a reconnecting client rebuilds its scrollback; then
2. `STATE_SNAPSHOT` — the status read-model above.

Live events (and `STATE_DELTA`s) follow.

**Session switch = the same protocol, mid-stream (#3310 N3).** A session
switch on an already-connected AG-UI stream re-fires this EXACT pair
(`AgUiEmitter._reconnect_snapshot_chunks`), strictly after the
`reyn.event.session_attached` barrier is forwarded — see *the session-switch
barrier* above. Connect-time and switch-time are one code path, not two
byte-identical-by-hand copies.

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
| `reyn.display.user`               | a user-authored line — a submitted turn OR a resolved intervention answer, RENDERED locally by a surface off `reyn.event.user_submitted` / `reyn.event.intervention_answer_submitted` (below), never PUT onto `session.outbox` by any producer as of #3300 (the last such write — the intervention-answer echo — was event-ified; kept as a valid `OutboxMessage` kind for surfaces' own local construction — e.g. a persisted-transcript restore — and as a fail-safe profile entry, not a live outbox-fanout wire kind); `meta` optionally carries `auth_user_id` / `auth_connection_id` attribution for a multi-client render (backlog user turns ride the standard `messages` array instead) |
| `reyn.display.system`             | a reyn chrome line — a persisted lifecycle/status marker (compaction / budget / cost-warn) |
| `reyn.display.__copy_last_reply__` | the `/copy` sentinel — forwarded (client-side clipboard copy); see *control sentinels* |
| `reyn.display.__rewind_list__`    | the `/rewind` sentinel — forwarded (client-side rewind picker); see *control sentinels* |
| `reyn.display.__attach_request__` | the attach-request sentinel — a fail-safe profile entry (upstream-consumed); see *control sentinels* |
| `reyn.display.tool_call_started`  | a tool-call start trace line                           |
| `reyn.display.tool_call_completed`| a tool-call completion trace line                     |
| `reyn.display.tool_call_failed`   | a tool-call failure trace line                        |

### `reyn.event.<etype>`

A reyn chat-event with no standard AG-UI analog. `value` is the event's data
object. Most members are the working-indicator axis (turn-lifecycle /
tool-call / user-submitted / cancel); `agent_delta` (#3288 ③b) is a SEPARATE
streaming-notification axis — see its row below and the `_STREAMING_EVENTS`
comment in `frames.py` for why it was forwarded ahead of any consumer. The
plain/repl renderer still has no `agent_delta` branch (and may never); the
Textual TUI (`interfaces/inline/textual_chat`) is the one surface that
consumes it, as of #3288 ③c — see *Textual TUI streamed-reply rendering*
below.
★This `CUSTOM` mapping is what `encode_frame`/`encode_frame_wire` (the plain,
non-streaming codec path) produce for an `agent_delta` `EventFrame` — the AG-UI
emitter's actual production call site (`emitter.py`) instead runs every frame
through `encode_frame_wire_streaming`, which maps `agent_delta` onto the
STANDARD `TEXT_MESSAGE_CONTENT` surface (#3288 ③d — see *Text lifecycle* above),
never this `CUSTOM` name, on the wire a real client receives. This row documents
what the plain codec functions do in isolation (still exercised directly by
`tests/test_agent_delta_chat_event_3288.py`), not what ships on the connected
wire.

| Custom `name`                        | Meaning                                          |
|--------------------------------------|--------------------------------------------------|
| `reyn.event.user_answered_intervention` | the user answered an intervention (working-indicator axis only — carries NO display text; see `reyn.event.intervention_answer_submitted` below for the echo) |
| `reyn.event.session_attached`        | a session/agent switch just happened (#3310 N1) — carries `{agent, session_id}`, the identity a client keys its display/reset cache on. Locally, emitted at the registry attach seam (`AgentRegistry.attach`/`attach_session`), put directly on `repl_outbox` as a stream BARRIER — see *the session-switch barrier* above; consumed by `TextualChatApp` (N2), which resets every per-session client state and rehydrates from `history.jsonl` on receipt. Remotely (#3310 N3), `_SessionFrameSource` synthesizes an independent per-connection equivalent when it observes a `/session switch` on the session backing THIS connection, and `AgUiEmitter` re-fires `MESSAGES_SNAPSHOT`/`STATE_SNAPSHOT` for the new session right after forwarding it — see *Remote parity* above |
| `reyn.event.user_submitted`          | a user turn was submitted (#3300 P1 C) — RAW text + chain_id + msg_id + seq + meta; each surface neutralizes at its render boundary. `msg_id`/`seq` are the #3300 P2a sent-queue correlation id + order-race-gate token |
| `reyn.event.intervention_answer_submitted` | an intervention answer was resolved (#3300, event-ifying the LAST outbox `kind="user"` broadcast site — `InterventionHandler.deliver_answer_to`) — RAW text (the raw answer, or the matched choice's label) + `intervention_id` + meta; each surface neutralizes at its render boundary, following the `user_submitted` precedent exactly. Unlike `user_submitted`, this has no sent-queue staging step — an intervention answer was never a queued inbox item, so it renders straight to the flow |
| `reyn.event.inbox_cancel`            | an UNDISPATCHED queued user message was cancelled by id (#3300 P3, via the `cancel_queued` client message) — carries `msg_id` + `seq`; the server-authoritative sent-queue removal signal (never a client-local "cancel succeeded" response), exclusive with `turn_started` for the same `msg_id` |
| `reyn.event.agent_delta`             | one streamed LLM content-delta chunk (#3288 ③b) — carries `text` (the raw per-chunk delta) + `chain_id`. The plain codec's `CUSTOM` mapping (see the note above); on the actual AG-UI wire (`encode_frame_wire_streaming`, #3288 ③d) this rides `TEXT_MESSAGE_CONTENT` instead — see *Text lifecycle* above for the full streamed-message contract (END-only completion, reconstruction authority, late-joiner closure) |

### Textual TUI streamed-reply rendering (#3288 ③c, #3283 ③)

The Textual TUI (`interfaces/inline/textual_chat/app.py`) is the L7 consumer
`agent_delta` was forwarded ahead of (see the note above): `TextualChatApp.
_handle_agent_delta_event` coalesces N deltas for one reply into exactly ONE
`FlowView` entry, keyed by `chain_id` — the SAME authoritative correlation id
`RouterLoop._emit_agent_delta` stamps on every delta and the terminal
`kind="agent"` `OutboxMessage` carries in its own `meta`. The first delta for
a `chain_id` appends a new entry; every later delta for that same `chain_id`
updates that SAME entry in place (`Entry.set_item`) rather than appending a
second row. The terminal completion (a DISPLAY frame, not an event) then
FINALIZES that same entry with the completion's authoritative full text
(never the deltas — L9 whole-persist's source of truth) and stops tracking
the `chain_id`, rather than appending a second entry of its own — this is
what keeps a mid-stream-joining client (one that only ever received the TAIL
of a reply's deltas, see *Text lifecycle*'s late-joiner closure above) to
exactly one final entry instead of a duplicate. The plain/repl renderer has
no equivalent branch and is unaffected — `agent_delta` there is still
consumed-but-dropped (opt-in draw, no visible-garbage window), exactly as
before ③c.

**Visibility-gated live updates (#3283 ③).** The in-place update above is
gated on whether the row is on screen, so a long conversation whose streaming
reply has been scrolled away costs O(1) model→view updates instead of
O(deltas). The append registers a `FlowView.track_visibility` tracker for the
entry; while the row is visible each delta issues its `Entry.set_item` as
before, and while it is NOT visible the delta **still accumulates** onto the
tracked reply text but issues no `set_item`. The tracker's `on_show` replays
the whole accumulated text in ONE update when the row scrolls back — so
scrolling away and back shows the COMPLETE reply, never a truncated one.

Two properties are load-bearing here:

- **The gate is an optimisation, not a correctness mechanism.** Text
  accumulation is unconditional; only the render is deferred. Removing the
  deferral leaves every reply byte-identical, just updated once per delta.
  Removing the `on_show` replay does NOT — that is what puts deferred text on
  screen.
- **It is a distinct gate from flowview's own.** flowview already skips the
  *present + reflow* for an off-screen update (`FlowView.on_flow_update`), but
  the `set_item` itself — a new item object, a revision bump, a strip-cache
  eviction, a model→view notification — happens regardless. #3283 ③ gates that
  *update feed*; flowview gates the *render*. Neither replaces the other.

The tracker is released when the terminal completion settles the row — the
load-bearing release, since nothing else would ever unregister a settled row's
observer and they would otherwise accumulate for the whole session — and, belt
and braces, for every still-in-flight reply on a session switch
(`session_attached`, #3310 N2), where clearing the model already drops every
observer via `FlowView.on_flow_clear`. The completion's own final write is NOT
visibility-gated: the authoritative full text lands whether or not the row is
on screen.

### Textual TUI gutters — state (left) + elapsed time (right) (#3283 ①②④)

The Textual TUI's `FlowView` (`interfaces/inline/textual_chat`) paints TWO
fixed-width columns per row, both driven by flowview's `FlowDecorator`
protocol (`decorate(entry, width, height) -> RenderableType`), never a
second hand-rolled column:

- **LEFT gutter — `ReynGutter`** (`gutter.py`): the #3273 state contract. A
  kind-driven glyph (`❯` user, `●` assistant/tool-header, `⎿` tool-result)
  whose COLOUR is driven by the entry's `EntryState` — RUNNING amber,
  SUCCESS green, ERROR coral, CANCELLED dim, DEFAULT the kind's own colour.
  A RUNNING entry's glyph BLINKS (`●`/`○`), picked from a monotonic clock
  (`int(clock() / frame_period)`) that flowview's own
  `FlowView(animation_fps=N)` re-invokes on each animation tick — no
  app-side timer (#3283 ①, native-blink equivalence).
- **RIGHT gutter — `ReynTimingGutter`** (`gutter.py`, #3283 ④): a per-entry
  ELAPSED-TIME label (`Ns`/`Nm`/`Nh`), wired via flowview's additive
  `right_decorator`/`right_gutter_width` params. **Content set — elapsed
  time only**, decided against what data actually exists (owner-adjudicated
  on #3283, issue thread): turn cost/tokens are CUMULATIVE-ONLY in
  `BudgetTracker` (no per-turn/per-entry source — showing one would be a
  fabricated per-entry number) and a dedicated state chip would duplicate
  the left gutter's existing `EntryState` encoding. Only a tool-call entry
  that actually has timing data shows a label — LIVE while RUNNING (read off
  the same start marker the ② live-spinner body uses), or the FINAL captured
  duration once SETTLED (stashed at settle time, before the live marker is
  stripped). Every other entry — user/agent lines, interventions, and ANY
  RESTORED row — renders an empty right-gutter cell: no placeholder, no
  `"0s"`. **Restore is live-session-only by decision**: a persisted
  `ChatMessage` carries no timing field at all, and widening that persisted
  shape was judged out of proportion to a TUI gutter decoration — a restored
  tool row's right gutter is always blank, never a reconstructed value.

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
interactive-surface choice (Claude Code-style TUI on an interactive TTY, plain
console for `--cui` / non-TTY / piped) is one shared seam
(`renderer.uses_app_input() and is_tty`, the same predicate `make_renderer` uses
behind `_inline_interactive`), and both `reyn chat` and `reyn chat --connect` hand
a `ClientTransport` + a `ChatReadModel` to the SAME driver
(`client_driver.run_chat_client`). On an interactive TTY that driver routes to the
Textual conversation-pane app (`reyn.interfaces.inline.textual_chat`, the #3273
TUI rebuild), which owns both input and output and drains the SAME
`transport.frames()` stream — so an interactive remote attach renders the TUI, not
a plain fallback, from the identical frame stream a local attach consumes. The
client reads its status bar / intervention region / task poll through the
read-model: a `RegistryReadModel` off the local session, or a `RemoteReadModel`
off the `STATE_*` view above.

**Local ≡ remote holds for INPUT too, symmetric with output.** A resolved
intervention answer (`InterventionHandler.deliver_answer_to` — the one funnel
every answer path shares: TUI free-text, the Textual TUI's grouped
intervention panel (`reyn.interfaces.inline.textual_chat.intervention_panel`,
#3299 P1/P2, tab-ified #3308 P5 — one tab per PENDING intervention, each a
closed-set `RadioSet` or free-text `Input`, between the conversation and the
input row, replacing the earlier in-flow chip surface; answering a tab
delivers targeted at THAT intervention's id — R1 by-id delivery — and marks
it ✓/inert without removing it, so several simultaneously-outstanding
interventions are each independently answerable, in any order, without one
displacing another), an A2A peer, and the AG-UI HITL round-trip above) emits
an `intervention_answer_submitted` chat-event (#3300 — event-ifying the LAST
outbox `kind="user"` broadcast site, following the `user_submitted` precedent
below exactly). A submitted turn (`Session.submit_user_text`) emits the
sibling `user_submitted` chat-event (#3300 P1 C — replacing an earlier
outbox-echo write, a category error: an INPUT written into the display/OUTPUT
channel). Both ride the SAME unified frame stream as an `EventFrame`
(`_TURN_AND_ANSWER_EVENTS`, `transport/frames.py`) — the encode/decode is
generic (`transport/agui/protocol.py`), so no wire changes were needed for
either event type. Every attached surface's event→display handler
(`ConsoleChatRenderer.on_chat_event` / `InlineChatRenderer.on_chat_event` /
`TextualChatApp._pump_frames`) renders the line, neutralizing at that render
boundary (`renderer.user_submitted_display_message` /
`renderer.intervention_answer_display_message` —
`TextualChatApp._handle_intervention_answer_event` for the Textual surface) —
**except the one client whose own terminal already showed it**, for
`user_submitted` only (an intervention answer has no client-local echo to
de-duplicate against — the panel/composer never prints the answer itself, so
every attached surface, including the answering one, renders off THIS event
with no suppression logic). On the plain PromptSession
loop (`--cui` / `chat.render_mode: plain` / non-TTY, `stream_client.py`), an
interactive TTY's `prompt_session.prompt_async` leaves the typed line on
screen the instant Enter is pressed — that already IS the echo. Re-rendering
it again from the broadcast `user_submitted` event printed every LLM-round-
trip turn's own line twice (#3287; a local `/quit` never reaches
`submit_user_text`, so it never doubled — the asymmetry the bug report
noticed). The fix is ownership, not suppression-by-default, and uses TWO
DIFFERENT correlation mechanisms — one per transport shape, neither by text:

- **Local (`InProcessTransport`)**: `route_input_line` records the `msg_id`
  its `transport.submit_user_text` call RETURNS (the SAME correlation id
  `user_submitted`'s `msg_id` field carries, #3300 P2a) in a small set
  (`own_submissions`, owned per client-loop-pair, never shared across
  clients); `run_output_loop` skips re-rendering a `user_submitted` event
  only when its `msg_id` matches an entry in THIS client's own set.
  `ClientTransport.submit_user_text` returns the assigned `msg_id`
  (previously `None`) — `InProcessTransport` returns `Session.submit_user_text`'s
  own return value directly, same-task and race-free (nothing yields between
  the chat-event emit and the id reaching the caller).
- **Remote (`AgUiTransport`)**: matches the broadcast event's
  `meta.auth_connection_id` against the client's OWN `connection_id` instead
  (`remote_client.py` mints it client-side with `uuid.uuid4()` BEFORE any
  submit and stamps it on every POST; the AG-UI endpoint's `user_message`
  handler already attributes every submit with it — #3300's existing
  multi-client display plumbing, `endpoint.py` → `session.py`'s `meta` →
  the broadcast event, unchanged wire shape). Known up-front, with **no
  dependency on any other channel** — see "closing the race" below.

Either mechanism: every other attached client's turns (and this client's own
turns when non-interactive, where nothing else echoes the line) still render
normally. With 2+ clients attached, everyone still sees every OTHER client's
turn and every answer, not only the agent's replies to them; each client
just stops duplicating its own.

Correlation is by **identity, never by text** — an earlier revision matched
by text and was caught in review (co-vet finding F1 on #3309): two attached
clients submitting the identical short line (e.g. both answering "yes")
would cross-match, simultaneously swallowing the OTHER client's turn and
leaving THIS client's own turn to double-print later, reintroducing the bug
through a different door. `msg_id` (#3300 P2a) and `auth_connection_id`
(#3300, multi-client attribution) were both added SPECIFICALLY as identity
fields — never form-sniffed from content — so two different submissions
never collide even with identical text.

**Closing the race, not just narrowing it (co-vet finding F2 on #3309)**: an
earlier revision used `msg_id` for the remote path too, reading it from the
POST response body — but that id only becomes visible once the POST
returns, and the server may already have pushed the SSE broadcast for the
same submission over the INDEPENDENT events connection in the interim, a
network-ordering race between the two channels. The reviewer pointed out
this is unnecessary: `meta.auth_connection_id` is the client's own identity,
known BEFORE the submit even happens — matching on it needs no second
channel to resolve at all, closing the race structurally rather than
documenting it as an accepted residual. `msg_id` remains load-bearing for a
different reason on the remote path: #3300 Y-client (cancel-by-id) needs the
client to learn its own message id regardless of transport, so
`AgUiTransport.submit_user_text` still returns it — it is simply no longer
what remote echo-suppression correlates on.

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
| Text       | 4                | 3           | **conforming triplet, plain and streamed** — a whole message rides `TEXT_MESSAGE_START` → `TEXT_MESSAGE_CONTENT` → `TEXT_MESSAGE_END`, correlated by `messageId`; a message that streamed (#3288 ③a/③b/③d) rides the SAME triplet with a REAL per-delta `TEXT_MESSAGE_CONTENT` for each chunk (see *Text lifecycle* above) — only the condensed single-event `TEXT_MESSAGE_CHUNK` variant is unmapped (**intentional-scope** — the triplet form already covers streaming; reyn has no use for the alternate condensed encoding) |
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
