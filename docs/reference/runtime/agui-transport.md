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
    synchronous snapshot-prune, then an `inbox_cancel` audit-event delta, see
    "STATE_* — the status read-model" and `reyn.event.inbox_cancel` below);
    already dispatched → a no-op (never escalated to `cancel_inflight`);
    idempotent (a second cancel of the same id is a no-op, safe for an
    at-most-once retry).
  - `{"type": "slash_command", "name": "model", "args": "strong"}` — run a
    registered slash command (#3595 S5). The response body is
    `{"status": "ok", "ran": true|false}`; `ran: false` means this server's
    registry has no such command (a client on a different build), never a
    crash. ★ The client has ALREADY interpreted the operator's `/…` line and
    resolved the NAME against its own registry — nothing on the wire or on the
    server tests a leading `/`, which is the whole point of #3595: `Session`
    interprets no string, and a client maps typed text onto published
    operations. A remote client sends this rather than running the command
    itself because it holds no `Session`, and the commands that still read
    session state can only run where that session is. Permission-gated by the
    same `authorize_write` check `user_message` passes, and the command's reply
    rides the ordinary display stream.
  - `{"type": "heartbeat"}` — a liveness keepalive.

  An input type the server does not model is a **graceful no-op** (a `200` ack),
  never a `500` — the server half of ignore-unknown.
- `POST /agui/chat/{agent}/seize` — take the active-driver token (see "Active
  driver and seize").

`{agent}` on `POST /agui/chat/{agent}` and `POST /agui/chat/{agent}/seize`
(#5129) is a **fallback, not the destination**: both routes resolve the real
target from this connection's own `connection_id` (whichever agent it is
currently attached to); `{agent}` is only consulted when the connection has
no recorded attachment. A `--connect`ed client's own cross-agent `/attach`
therefore redirects its NEXT `submit`/`seize` too, without the client having
to change the URL it POSTs to.

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
| `__end__` | *(filtered)* | NOT forwarded (see *control sentinels*) |

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
  - `__open_artifact__` — local-only by construction (launches an OS app on the
    machine the client is running on; see *#4482*).
- **Retired** (#4534 PR-2 / PR-2b): `__attach_request__` and
  `__session_switch_request__` no longer exist. `/agent new`, `/attach`, and
  `/session switch` all go through `ClientTransport.request_attach` /
  `request_session_switch` — named operations, not display-channel sentinels
  (#3595 S5's own principle: the client interprets, the server executes a
  named operation, the same shape `run_slash_command` already applied).
  Earlier revisions of this doc described `__attach_request__` as "forwarded,
  and genuinely live" on the wire, and `__session_switch_request__` as
  tap-consumed-and-filtered — both accurate before their respective PRs
  landed. Session-switch follow (below) no longer consumes a sentinel off
  the outbox either; it subscribes to `registry.add_attach_listener` directly.

#### The session-switch barrier (`reyn.event.session_attached`, #3310 N1/N2)

`/attach <name>` and `/session switch <sid>` both flip which session's frames
reach a client — but historically nothing told the client THAT a switch had
happened (the old Textual TUI's header re-post was deleted as dead code; see
the control-sentinel dispositions above). `AgentRegistry.attach`/
`attach_session` now emit a `session_attached` `EventFrame` carrying
`{agent, session_id}` — the identity a client keys its display/reset cache on
— put DIRECTLY on `repl_outbox` (`registry.py`, the
`_announce_session_attached` helper), never routed through the just-swapped
session's own audit-events.

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
session's own `outbox_hub`/`audit_events` directly, bound to ONE session
object for the SSE connection's lifetime. A remote client that switched
sessions therefore had NO way to obtain the new session's scrollback at all:
the remote read-model's `conversation_history` is deliberately empty
(frame-sufficiency, `read_model.py`) and the emitter's `MESSAGES_SNAPSHOT`
backlog was otherwise fixed at connect time. N3 closes this by treating a
switch as a **logical reconnect**, entirely within the remote transport
(the registry's own `attach`/`attach_session` + `repl_outbox` barrier are
untouched and not involved):

- `_SessionFrameSource` (`endpoint.py`) subscribes to
  `registry.add_attach_listener(agent_name, ...)` (#4534 PR-2b — ported off the
  retired `__session_switch_request__` sentinel, whose in-band arrival on
  `session.outbox_hub` this source used to observe directly; `attach_session`
  now flips focus out-of-band, so the source instead registers a synchronous
  callback the registry fires from `_announce_session_attached`'s own no-await
  critical section). The callback hands the target sid to a per-connection
  `asyncio.Queue`, which `_drain_one_session`'s drain loop dual-waits
  alongside `sub.get()` (`asyncio.wait(..., return_when=FIRST_COMPLETED)`) —
  the second wait source an in-flight blocked `await sub.get()` needs to be
  interrupted by an out-of-band signal. On seeing the sid, it **enqueues the
  `session_attached` `EventFrame` onto this connection's own queue BEFORE
  re-pointing itself at the target session** (`registry.get_session` +
  subscribing to the new session's audit-events) — the SAME vocabulary N1
  defined, an independent per-connection equivalent since `repl_outbox` never
  reaches a remote surface. This ordering (announce, then subscribe) is
  load-bearing, not incidental: the new session's audit-event subscriber does
  not exist yet when the announce is enqueued, so an audit-event the new
  session emits cannot possibly reach this connection's queue ahead of the
  barrier — true BY CONSTRUCTION regardless of whether an `await` is ever
  later introduced between the two steps (witnessed by
  `test_switch_announce_precedes_any_new_session_audit_event`, an adversary
  that floods the target session's own audit-event stream the instant the
  switch is triggered). It never calls `registry.attach_session` itself, so
  it cannot race or double-apply that side effect — it only re-points THIS
  connection's own view. A registry-less / agent_name-less construction
  (most existing unit tests) registers no listener at all, so no switch-follow
  ever happens for that connection.
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
`_reyn`, reconstructing that exact `agent_delta` audit-event — so a reyn client's
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

| reyn audit-event              | AG-UI event      |
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
  `attached_name`, `model`, `agent_names`, `session_tree`,
  `all_sessions_status` (#5729), `model_active_class`, `model_classes`
  (#5094), `visibility_items`, `mcp_subscriptions` (#5185), `cost_agent`,
  `cost_total`, `agent_tokens`, `ctx_used`, `ctx_window`, `waiting_on`,
  `pending_intervention_head` (#5050), `queue`, `turn_active`, `queue_seq`
  (#3300 P2a), `halted_reason`. `agui/state.py`'s own `project_status` is
  the sole declaration of this list (#5098) — read it directly rather than
  trusting this doc's transcription to stay current.

`all_sessions_status` (#5729) is `AgentRegistry.all_sessions_status()` —
per-session `{agent, sid, turn_active, iv_waiting}` for every LOADED
session in this process (never a sibling process, #5694/#5714). Unlike
`turn_active` above (scoped to the one attached session), this covers
every session so the agent tab can show them all — including ones a
remote client has not attached. `turn_active`/`iv_waiting` are carried as
2 INDEPENDENT booleans, never collapsed into a status enum: a turn can be
dispatched and ALSO waiting on an intervention answer at once, and that
combination is the one an operator most needs to see.
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
audit-event that dispatched the in-flight item) still gets the correct queue +
turn-active state from the snapshot, not a partial event-derived guess. P2a
publishes this state only — rendering it as a sent-queue widget is P2b.

An item leaves `queue` via one of two mutually-exclusive granular audit-event
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

These are the **main status-line values** the interactive TUI renders, so a
remote client on an interactive TTY draws the same status line as a local one
(`agent` · `model` · `cost` · `ctx%`, plus the working indicator) — PLUS, as of
#5094/#5185, a growing set of **drawer panes** that also reflect real
server-side state: the `/model` class picker and agent/session tree (#5094),
and the tool/mcp/skill visibility toggles (#5185, `visibility_items`) together
with the mcp pane's own subscription rows (`mcp_subscriptions`) — see
`ChatReadModelCapabilities`'s own docstring
(`reyn.interfaces.repl.read_model`) for the full, authoritative list of which
reads are genuinely wired vs. graceful-degrade; a hand-transcribed list here
would drift the moment a new key is added, the same risk #5098 already named
for the field list above. The REMAINING drawer panes (the cost breakdown and
ctx/compaction detail, the hook applicability toggles, the pipeline and cron
listings) and the interactive intervention / `/rewind` **pickers** are still
session-local state, not on the wire — a remote client degrades those to
empty/`—`/0/`"not reported on this connection"`, never a fabricated value.
Adding any other field is an additive `STATE_*` key, not a client change.

## Reconnect

On connect (or reconnect) the server replays, before any live event:

1. `MESSAGES_SNAPSHOT` — the display backlog, so a reconnecting client
   rebuilds its scrollback; then
2. `STATE_SNAPSHOT` — the status read-model above.

Live events (and `STATE_DELTA`s) follow.

**Session switch = the same protocol, mid-stream (#3310 N3).** A session
switch on an already-connected AG-UI stream re-fires this EXACT pair
(`AgUiEmitter._reconnect_snapshot_chunks`), strictly after the
`reyn.event.session_attached` barrier is forwarded — see *the session-switch
barrier* above. Connect-time and switch-time are one code path, not two
byte-identical-by-hand copies.

**The backlog is ONE bounded page, not the full history (#5139 C).** The
server sends at most `HYDRATE_PAGE_FRAMES` (200 — the same bound local
restore's own lazy scrollback paging already uses) frames per
`MESSAGES_SNAPSHOT`, cut only at a turn boundary (a `chain_id`-correlated
run — never inside a tool call/result pair, which would silently break
its own correlation). The `_reyn` block carries two more keys alongside
`messages`: `has_more` (whether an older turn still exists beyond this
page) and `next_cursor` (that older turn's own `chain_id`, `None` exactly
when `has_more` is `False`). A client that scrolls past this page's own
top end (`FlowView.ReachedTop`) POSTs `{"type":
"load_older_backlog_request", "session_id", "before_root_id":
<next_cursor>}`; the response reuses this SAME `MESSAGES_SNAPSHOT`
encoding (one more bounded page, its own `has_more`/`next_cursor`) rather
than a second wire vocabulary. Continuation is always CLIENT-driven pull
— the server never pushes a second page unprompted, the same "server
owns the per-request bound, never a second unbounded send" discipline
the rest of this doc's typed requests (`session_list_request` etc.)
already follow.

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
| `reyn.display.tool_call_started`  | a tool-call start trace line                           |
| `reyn.display.tool_call_completed`| a tool-call completion trace line                     |
| `reyn.display.tool_call_failed`   | a tool-call failure trace line                        |

### `reyn.event.<etype>`

A reyn audit-event with no standard AG-UI analog. `value` is the event's data
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
`tests/interfaces/test_agent_delta_audit_event_3288.py`), not what ships on the connected
wire.

| Custom `name`                        | Meaning                                          |
|--------------------------------------|--------------------------------------------------|
| `reyn.event.user_answered_intervention` | the user answered an intervention (working-indicator axis only — carries NO display text; see `reyn.event.intervention_answer_submitted` below for the echo) |
| `reyn.event.session_attached`        | a session/agent switch just happened (#3310 N1) — carries `{agent, session_id}`, the identity a client keys its display/reset cache on. Locally, emitted at the registry attach seam (`AgentRegistry.attach`/`attach_session`), put directly on `repl_outbox` as a stream BARRIER — see *the session-switch barrier* above; consumed by `TextualChatApp` (N2), which resets every per-session client state and rehydrates from `history.jsonl` on receipt. Remotely (#3310 N3), `_SessionFrameSource` synthesizes an independent per-connection equivalent when it observes a `/session switch` on the session backing THIS connection, and `AgUiEmitter` re-fires `MESSAGES_SNAPSHOT`/`STATE_SNAPSHOT` for the new session right after forwarding it — see *Remote parity* above |
| `reyn.event.user_submitted`          | a user turn was submitted (#3300 P1 C) — RAW text + chain_id + msg_id + seq + meta; each surface neutralizes at its render boundary. `msg_id`/`seq` are the #3300 P2a sent-queue correlation id + order-race-gate token |
| `reyn.event.intervention_answer_submitted` | an intervention answer was resolved (#3300, event-ifying the LAST outbox `kind="user"` broadcast site — `InterventionHandler.deliver_answer_to`) — RAW text (the raw answer, or the matched choice's label) + `intervention_id` + meta; each surface neutralizes at its render boundary, following the `user_submitted` precedent exactly. Unlike `user_submitted`, this has no sent-queue staging step — an intervention answer was never a queued inbox item, so it renders straight to the flow |
| `reyn.event.inbox_cancel`            | an UNDISPATCHED queued user message was cancelled by id (#3300 P3, via the `cancel_queued` client message) — carries `msg_id` + `seq`; the server-authoritative sent-queue removal signal (never a client-local "cancel succeeded" response), exclusive with `turn_started` for the same `msg_id` |
| `reyn.event.agent_delta`             | one streamed LLM content-delta chunk (#3288 ③b) — carries `text` (the raw per-chunk delta), `chain_id`, and `round_index` (which LLM round of the turn produced it, #3656). A turn that calls a tool emits more than one assistant message, and `chain_id` alone cannot tell them apart; the producer runs inside the round, so the index is a fact it holds rather than one a consumer reconstructs from frame order. The plain codec's `CUSTOM` mapping (see the note above); on the actual AG-UI wire (`encode_frame_wire_streaming`, #3288 ③d) this rides `TEXT_MESSAGE_CONTENT` instead — see *Text lifecycle* above for the full streamed-message contract (END-only completion, reconstruction authority, late-joiner closure) |

### Textual TUI streamed-reply rendering (#3288 ③c, #3283 ③)

The Textual TUI (`interfaces/inline/textual_chat/app.py`) is the L7 consumer
`agent_delta` was forwarded ahead of (see the note above): `TextualChatApp.
_handle_agent_delta_event` coalesces N deltas into ONE `FlowView` entry **per
LLM ROUND**, keyed by `(chain_id, round_index)` (#3656). `chain_id` is the
turn; `round_index` is which round within it, and a turn that calls a tool has
more than one — 140 deltas, three tool calls, then 300 deltas was the measured
case, and its two texts are two separate assistant messages in history. Keyed
by `chain_id` alone, the second round's deltas flowed into the entry created
BEFORE the tool row, so what the model wrote after reading a tool result
appeared above the call that produced it.

The first delta for a `(chain_id, round_index)` appends a new entry; every
later delta for the same pair updates that SAME entry in place
(`Entry.set_item`) rather than appending a second row. A delta from a LATER
round closes the previous round's record — the entry keeps its text and simply
stops being a target, since the terminal frame arrives once per TURN, not once
per round. A delta with no `round_index` (an older producer, a replayed frame)
reads 0 and therefore coalesces exactly as before.

The terminal completion (a DISPLAY frame, not an event) then FINALIZES the
LAST round's entry with the completion's authoritative full text (never the
deltas — L9 whole-persist's source of truth) and releases any earlier round,
rather than appending a second entry of its own — this is
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

### Textual TUI gutters — state (left) + elapsed time/turn tokens (right) (#3283 ①②④)

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
  The **ADDRESSED-ROW RAIL** (#3490) is drawn in the RIGHT gutter, described
  below — this column carried it until #3526 moved it on the owner's
  instruction. **There is exactly ONE addressed position** — the keyboard
  cursor (#3476 ⑥), which is also what `ctrl+n` search moves (#3493) rather
  than keeping a second selection of its own, so two different rows can never
  both be marked *by construction* instead of by a gating rule that has to
  stay correct. **flowview 0.11.0 (#3624) merged keyboard highlight and mouse
  selection into that one `selectable=` cursor** — previously `highlight=`
  (keyboard-only) and `selectable=` (mouse-only) were independent flags, and
  reyn left `selectable=` off specifically to keep a click from moving the
  addressed position; that separation no longer exists upstream, so
  `FlowView(selectable=True)` now enables the cursor for BOTH inputs and a
  click both moves and commits it, same as Enter/Space. reyn keeps the
  addressed-row rail's *single-position* invariant regardless (a click just
  becomes a second way to move it, same as an arrow key) — see "Textual TUI
  keyboard cursor" below for the copy-on-commit hazard this merge introduced
  and how it is contained. The state glyph keeps its own `EntryState` colour —
  being addressed is a POSITION, not an outcome, so the mark must not repaint
  the state vocabulary. The rail's colour is `_CC_TEXT` (`"default"`), so it
  forces no colour of its own and follows the theme's foreground. A named ANSI
  colour was tried first, to have the TERMINAL's own palette resolve it: rich
  does keep such a colour palette-relative (the strip carries
  `ColorType.STANDARD`), but **Textual downconverts it to truecolor at output**
  (measured in a real terminal — `"blue"` arrived as
  `\x1b[38;2;157;101;255]`, its theme's purple). The only true passthrough is
  the app-wide `App.ansi_color`, which would drop the whole `_CC_*` palette to
  16 colours, so it is deliberately not set. Since #3526 the bar is a thin `▏`
  (U+258F) in the RIGHT gutter's LEADING cell — the edge facing the body, so it
  stays as near the text as that side allows — spanning the body's whole
  post-wrap `height` so one entry reads as one marked block. Both the old and
  new positions cost no body column (each gutter is a fixed-width band) and both
  double as a divider; what differs is DISTANCE, since the right margin is a
  place most lines stop short of, unlike the line start the left rail met. The
  app supplies
  `ReynRightGutter(is_marked=…)`, which reads `FlowView.current` live on every gutter
  repaint, and re-derives the affected rows' gutters via
  `FlowView.refresh_gutter` on each `Highlighted` and on focus changes (the
  gutter cache is keyed on a decor revision that neither a cursor move nor a
  focus change bumps, so without that invalidation the rail would strand on
  the row it was first painted on). The rail shows only while the pane is
  actually being addressed — FlowView focused, or the search bar open; the
  position persists either way. **Why the rail is gutter CONTENT
  and not a `flowview--highlight` component style**: flowview applies
  a component style as `Segment.apply_style(segments, style)` ==
  `style + segment.style` — a BASE *beneath* each segment's own attributes,
  with no `post_style` — so a background there is swallowed on exactly the rows
  carrying the ROW TINT described next. `text-style: reverse` does survive that
  merge (it is what #3476 ⑤/⑥ originally shipped) but inverts fg/bg into a
  near-white block over the palette, so surviving the merge is necessary and
  not sufficient.
  That class (`flowview--highlight` — `flowview--cursor` before 0.7.0,
  `flowview--selected` a synonym from 0.11.0 until #3624 / flowview 0.12.0
  dropped the alias) is therefore left **undeclared**, and
  flowview 0.6.1 onward honours
  that: an undeclared component class paints nothing, because the row overlay
  uses the *partial* component style (only the rules an app actually declared).
  Under 0.6.0 it did not — Textual resolves an undeclared component class to a
  *concrete* style synthesised from inherited values
  (`get_component_rich_style("flowview--highlight")` returned
  `Style(color=#e0e0e0, bgcolor=#121212)`), flowview painted it, and the
  addressed row came out near-black; because the cursor auto-arms on the newest
  entry, the BOTTOM row wore it permanently (#3496, reported upstream as
  textual-flowview#5 and fixed in 0.6.1, which let reyn delete the subclass
  that had been suppressing the accessor). 0.6.1 also made a *declared*
  component background win over a row's own `Presentation.background`
  (textual-flowview#6) — so a component style is now a viable way to mark a
  row, and reyn still does not use one: the gutter rail leaves the
  conversation's own colours completely alone, which is the owner-directed
  design, not a workaround.
  What keeps this honest is `test_the_addressed_row_keeps_its_own_background`,
  which compares each row's painted background before and after it becomes
  addressed — it fails whichever side disturbs the row (it is RED on 0.6.0
  without the subclass, verified).
- **ROW TINT — `Presentation.background`** (`presenter.py`): a user row and a
  FAILURE row (a `tool_call_failed` / `error` frame, or a `tool_call_completed`
  whose summary is a `✗`) carry a whole-row background that flowview paints
  edge to edge across gutter + body + padding (`_view._compose_line`). Every
  tint is a `_CC_*_BG` constant — a faint DARK block (`_CC_USER_BG`,
  `_CC_ERR_BG`) that the row's normal foreground stays legible against; a
  saturated `_CC_*` foreground colour is never reused as a background. The two
  vocabularies are kept disjoint deliberately: foreground and background are
  chosen on independent code paths, so overlapping them is how they collide.
  #3367 was exactly that collision — every failure leg paired `style=_CC_ERR`
  with `background=_CC_ERR`, painting the row's text (and, because the tint
  spans the gutter column, the gutter's coral `⎿`/`✗` glyph) in its own
  background colour, so a failed tool call rendered as an unreadable solid
  band. `tests/interfaces/test_textual_chat_row_contrast_3367.py` gates the invariant over
  the (kind, state) cross-product enumerated from `DISPLAY_KINDS` +
  `EntryState`.
- **RIGHT gutter — `ReynRightGutter`** (`gutter.py`, #3283 ④): one column, two
  label families, wired via flowview's additive
  `right_decorator`/`right_gutter_width` params. flowview takes a single right
  decorator, so this class composes two single-purpose halves and joins
  whatever is non-empty:
  - **`ReynTimingGutter` — per-entry ELAPSED time** (`Ns`/`Nm`/`Nh`). Only a
    tool-call entry that actually has timing data shows a label — LIVE while
    RUNNING (read off the same start marker the ② live-spinner body uses), or
    the FINAL captured duration once SETTLED (stashed at settle time, before
    the live marker is stripped).
  - **`ReynTurnUsageGutter` — TWO different figures, two different anchor
    rows** (`↑12k ↓1.8k` — `↑` prompt, `↓` completion). Earlier revisions
    anchored both a per-call figure AND the turn total to the same
    `kind="agent"` row, falling back to the turn total whenever a specific
    agent row didn't carry its own `prompt_tokens` — ambiguous, since the
    same visual slot answered two different questions depending on a hidden
    fact about that one frame. Split into two anchors instead (#4691 arc
    item ④, owner ruling):
    - `kind="agent"` rows (`TURN_ANCHOR_KIND`) show ONLY their own per-call
      absolute figure, straight from `entry.item.meta` — never a turn-total
      fallback. A row with no per-call figure (a restored/legacy frame, or
      an agent-kind emit site that never threaded one through) renders an
      EMPTY cell, never a silently-substituted number.
    - `kind="user"` rows (`TURN_TOTAL_ANCHOR_KIND`) — the line that OPENS a
      turn — show the turn TOTAL via a keyed lookup over `BudgetTracker`'s
      bounded per-turn buckets (`BudgetTracker.turn_usage` via
      `Session.turn_usage`, reached from the status snapshot's
      `turn_usage_fn`) — the per-turn attribution #3339 captured at the
      source, with the prompt/completion split accumulated alongside the
      total at the call. **Never derived by differencing cumulative
      counters.** Anchoring to the opening line rather than a settled reply
      also means the figure never repeats across a turn's multiple `agent`
      rows the way the shared-anchor design risked. A row that NAMES a turn
      (`meta["chain_id"]`) whose figure the runtime does not hold renders
      `—`, never `0`: a turn that made no LLM call, a turn EVICTED from the
      bounded buckets, an unknown chain_id, and a REMOTE client (per-turn
      buckets are session-local and not projected onto the wire —
      `turn_usage_fn` is `None` there, the same frame-sufficiency boundary
      as the past-turn log). A turn that recorded **0** tokens renders
      `↑0 ↓0` — a measured fact, kept distinct from `—`. A row naming NO
      turn at all (every RESTORED row — `chain_id` is not carried onto a
      re-projected persisted frame, and the per-turn buckets are in-memory
      live-session state a restart does not rehydrate) renders an EMPTY
      cell — nothing unknown to report on a row with no turn to report.

    The turn's **USD cost is deliberately not drawn here** even though the
    lookup returns it: tokens answer the question this column exists for, and
    the narrower column leaves the conversation body room (a real-TTY read at
    80 columns found a wider gutter left long tables and code cramped).
    `/cost` and the status line remain the spend surfaces.

  A dedicated state chip, the umbrella issue's third candidate, stays dropped:
  it would duplicate the left gutter's existing `EntryState` encoding.
  A row with no data in either family renders an empty cell — no placeholder,
  no `"0s"`, no `0` tokens. **Both families are live-session-only by
  decision**: a persisted `ChatMessage` carries no timing field,
  `project_restored_frames` does not carry `chain_id` onto a restored frame,
  and the per-turn buckets are in-memory state a restart does not rehydrate —
  so a restored row's right gutter is blank, never a reconstructed value.

  Both labels are painted with `_CC_AMBIENT` (`"dim"`, i.e. SGR 2) rather than
  a colour, so the TERMINAL's theme decides their shade (#3536). They had used
  the fixed mid-grey `_CC_DIM` (`#6b7280`), which on a transparent terminal
  background left them unreadable — its contrast is whatever shows through.
  This applies HERE and not to `_CC_DIM` generally: a terminal-chosen
  foreground is only safe over a terminal-chosen background, and these labels
  ride solely on rows the presenter does not tint (`agent`,
  `tool_call_started`). On a tinted row (`_CC_USER_BG` / `_CC_ERR_BG`, fixed
  dark hex) the same substitution would be dark-on-dark for a light-terminal
  user, and the row-contrast gate would stop measuring the pairing entirely —
  it only inspects segments whose foreground is concrete.

  The column is a fixed width derived in terminal **cells**
  (`rich.cells.cell_len`, the measure Textual's own compositor applies) from
  the widest label each family can emit — not from a character count. The two
  direction markers are East Asian *Ambiguous* width; rich resolves them to one
  cell, so the derivation and the renderer agree by construction.

#### Hiding a gutter (#3352)

Both columns cost their width on **every** row, so either can be switched off
and its whole column handed back to the conversation body:

| Key | Effect |
|-----|--------|
| `ctrl+g` | Show/hide the LEFT (state-marker) gutter — 2 columns |
| `ctrl+t` | Show/hide the RIGHT (elapsed/tokens) gutter — 12 columns |

Both keys appear in the TUI's **Help** pane (sourced from the app's own
binding table). Note that `ctrl+r` is **not** available for new bindings: it is
reserved for voice input (see `RESERVED_KEYS` in `textual_chat/chrome.py`), and
it is reverse-history-search in most shells. The two sides are
**independent** — flowview exposes
`left_gutter_visible` / `right_gutter_visible` as two flags and reyn follows
that granularity rather than offering a single combined switch.

Hiding is a real width recovery, not a blank column: flowview counts a hidden
gutter as width 0 (`left_gutter_effective_width` / `right_gutter_effective_width`),
grows `FlowView.body_width` by exactly that amount and re-presents the body at
the new width. Measured on an 80×24 terminal, the body goes 66 → 78 columns
with the right gutter hidden and → 80 with both hidden. (`FlowView.region.width`
stays the full terminal width in all cases — it does not respond to gutter
configuration and is not the plane to read.)

The **start** state is config-backed (`chat.gutters.left` / `chat.gutters.right`,
both `true` by default); a keypress is **session-scoped** and never writes back
to `reyn.yaml`.

### Textual TUI empty state — the fresh-session hint (#3476)

A session with no history used to open onto a blank void above the composer
(owner design review). The conversation pane now paints a centred hint while
the model holds **no entries**:

```
reyn

Type a message to start
/ commands · : skills · Help tab for keys
```

This is flowview's `empty=` / `empty_align="middle"` — an EMPTY STATE the
library itself clears the instant the first entry lands, not an app-managed
banner. That distinction is the whole point of using it: a reyn-side
show/hide would be a second piece of state to keep in step with the model,
and it would drift the first time an entry arrived through a path that
forgot to hide it. The hint is a `rich.Text` built by `empty_state_hint()`
(`textual_chat/app.py`) in the palette's dim tone, so it reads as ambient
guidance rather than content.

The same change made restore **hydration** a single `FlowModel.extend` call
instead of one `append` per frame: `extend` reflows the view once for the
whole page (flowview 0.6.0), where the per-entry loop reflowed once per
entry. The per-entry `set_state` calls that follow only repaint gutters, so
they add no reflow.

### Textual TUI lazy history paging (#3476)

Restore materialises only the **newest** `_HYDRATE_PAGE_FRAMES` (200) frames
of the projected history. The older prefix is held aside
(`_older_frames`, oldest-first) and paged in a slice at a time as the user
scrolls toward it:

- `FlowView.ReachedTop` (edge-triggered, armed `reach_threshold=3` rows early
  so the page is in place before the user arrives, and re-armed when the view
  retreats from the edge) → `FlowModel.insert_many(0, page)`.
- `insert_many` reflows **once** for the whole prepended slice and flowview
  preserves the scroll position across it, so the row being read does not
  move.
- With nothing left to page in, the handler is a no-op. Live frames are
  unaffected — they append at the bottom through the frame pump and never
  reach this path.
- `_older_frames` is reset by every hydrate call (initial mount AND session
  switch), so a switch can never page in the previous session's leftovers.

A restored tool frame's terminal `EntryState` is applied by ONE shared
transition (`_apply_restored_state`) that runs on **both** the hydrate and
the page-in path — a row that pages in lazily settles exactly as it would
have on first paint.

**This is not a performance fix, and the doc should not imply it is.** The
view-side cost of hydrating everything at once was measured small (heights
are lazily estimated, so a 40 000-frame `extend` costs ≈ 41 ms); paging was
adopted as deliberate forward infrastructure for histories far beyond that,
with the measurement recorded on the issue. What the tests pin is therefore
the **correctness** of the paging, never a timing claim.

`/copy`'s reply ring is seeded from the FULL restored history, not from the
materialised page — what `/copy N` addresses is the history, and a reply in
the not-yet-paged-in prefix stays reachable. The seeding walks the frames
oldest-first with `appendleft`, the same direction the live pump uses: the
ring is a `deque(maxlen=COPY_BUFFER_MAX)` whose index 0 must be the newest
reply, and a `reversed()` + `append` seeding expressed that correctly only
while the reply count stayed under the cap — past it, `append` evicts from
the NEWEST side and silently inverts the "1 = newest" contract (#3486).

### Textual TUI in-conversation search (#3476, #3692 PR-B ③)

`ctrl+n` opens a one-line search bar docked directly above the composer (the
last chrome region before the input row; collapsed by default). Originally
`ctrl+f` — moved by #3692 PR-B once flowview 0.13 gave that key its own
meaning (`cursor_scroll_page_down`, one member of a `ctrl+b/d/e/f/u/y`
vim-scroll set): reyn's search is entry-granular over the FULL conversation
model (forcing lazily-paged-in older history to materialise first) while
flowview's own `*`/`n`/`N` is a row/character-granular cursor jump limited to
whatever is already materialised — measurably different features, so per the
issue's own decision rule the search moved rather than displacing one
vim-scroll key out of its set. `ctrl+p` was the first candidate and a real
trap: free by every enumeration below, yet still claimed — by Textual's own
`App.COMMAND_PALETTE_BINDING`, a class attribute outside the declarative
`BINDINGS` list this enumeration otherwise walks (measured: pressing it in a
real pilot opened the command palette, not the search bar). `ctrl+n` was
re-verified free against the full enumeration: `TextArea`'s own
ctrl-bindings, flowview's owned set, `RESERVED_KEYS` (`ctrl+r`/`F2`), the
(separate, unbuilt) plain-CLI redesign proposal's own key table, and
Textual's `App`/`Screen` class attributes beyond `BINDINGS` — and pressed,
not just declared, in `test_search_bar_3476.py`.

| Key | Effect |
|-----|--------|
| `ctrl+n` | Open (or refocus) the bar |
| `Enter` / `↑` | Step to an OLDER match |
| `Shift+Enter` / `↓` | Step to a NEWER match |
| `Esc` | Close, clear the mark, return focus to the composer |

Matching is incremental: every keystroke recomputes the match set, selects the
**newest** hit and centres it (`scroll_to_entry(align="center", animate=True)`),
and the bar shows the hit's model-order position as `n/M`. Stepping uses
`FlowView.find_previous` / `find_next` (model order, wrapping). The arrows map
**spatially** — `↑` walks toward older entries, the direction the viewport
moves — so the key pressed and the way the conversation scrolls always agree;
`Enter` = older follows the same reasoning, since a bottom-anchored
conversation is searched backward from now.

Two decisions worth stating because the obvious alternative is wrong:

- The predicate reads the **model** text (`entry.item.text`), never
  `FlowView.entry_text()`. The latter returns the *rendered* body and is `""`
  for an entry that has not been presented yet, which would silently exclude
  every never-scrolled-to row from the search domain. (#4171: flowview 0.17.0
  fixed this same gap in its OWN `*`/`n`/`N` search — see "Textual TUI text
  cursor" below — via an optional `search_text=` reyn now supplies; the two
  searches stay independent features, not a shared implementation, but
  neither is limited to the rendered band any more.)
- Opening the bar first materialises the **entire** lazily-held older prefix
  (one `insert_many`). A hit that exists in the restored history but not in
  the materialised page would otherwise read as "no results" — a lie, and the
  measured full-hydrate cost makes paying it all at once cheaper than teaching
  search a second, virtual domain.

Search moves the **keyboard cursor** (#3493) rather than holding a selection
of its own, so the hit is marked by the one addressed-row rail described under
*Textual TUI gutters* above — and closing the bar keeps the cursor on the hit,
so `Shift+Tab`/`Ctrl+O` back into the pane resumes navigating from what you found. Its
keys are registered in `SEARCHBAR_KEYS`
(`textual_chat/chrome.py`) so the Help pane sources them from where they are
defined.

### Textual TUI keyboard cursor (#3476, #3624)

The conversation pane carries an entry-level **cursor**
(`FlowView(selectable=True)`) — flowview 0.11.0 unified what used to be two
independent flags, keyboard-only `highlight=` and mouse-only `selectable=`
(0.7.0's name for what 0.6.x called `cursor=`), into ONE `current` entry driven
by *both* inputs; 0.12.0 (#3624) then removed the `highlight=` alias entirely,
so `selectable=True` is now the only spelling and it enables the mouse
alongside the keyboard whether reyn wants the mouse leg or not (see the hazard
below). The pane is reached via Textual's own `Shift+Tab` focus cycling, and
(#3692 PR-B ①) `Ctrl+O` — a direct jump reyn adds because flowview cannot bind
a key for "I don't have focus yet"; only the app that currently holds focus
can move it in. `Esc` returns to the composer (machine-verified by the
Esc-sufficiency gate, including the #3692 case where an active text-cursor
selection intercepts `Esc` one layer in instead — see "Textual TUI text
cursor" below). While FlowView does not hold focus these keys are unaffected;
the composer's own `PageUp`/`PageDown` scroll delegation calls actions on the
view directly and does not depend on the cursor at all.

| Key / input | Effect |
|-----|--------|
| `Ctrl+O` / `Shift+Tab` | Focus the pane, landing on the remembered `current` entry (or the newest, on first entry) |
| `↑` `↓` `PgUp` `PgDn` `Home` `End` | Move the cursor (flowview's own bindings) |
| `Enter` | Copy the cursor entry's text to the clipboard |
| `Space` | Fold/unfold the highlighted entry's tool detail (#4697); inside the text cursor below, falls through to the same copy as `Enter` instead |
| a click | Move the cursor to the clicked entry (flowview 0.11.0+; does **not** copy — see below) |
| `r` | Open `/rewind` |
| `Esc` | Back to the composer (or, with an active text-cursor selection, cancels the selection first) |

Arriving at the pane arms the cursor on the **newest** entry rather than
leaving it invisible until the first arrow press: flowview's `move_current`
starts from `current=None` and only lands on an entry once a direction key (or
click) moves it (`Textual`'s own, unrelated `TextArea.move_cursor` is a
same-named different API and not this one), which is a real gap for a feature
whose whole point is a visible position indicator. A remembered position is
kept across visits — leaving and re-entering resumes where you were.

**Copy (Enter/Space only) — a click must not trigger it (#3624).** flowview
0.11.0 made `Selected` fire on **every commit**: Enter, Space, *and a click*,
with nothing in the event that says which one it was (`Selected.__init__`
takes only `flow_view`/`entry`). reyn's pre-0.11.0 intent — Enter/Space on the
cursor entry copies it to the clipboard — cannot be read off `Selected`
directly any more: doing so would let one stray click silently overwrite
whatever the user had copied in a **different application**, possibly
credentials. reyn does not register `on_flow_view_selected` at all. Instead
`textual_chat/app.py` defines `_CursorFlowView`, a thin `FlowView` subclass
that overrides `action_activate` — the method flowview's own `BINDINGS` bind
Enter/Space to (`Binding("enter", "activate", …)`) — to additionally post a
private `_FlowViewKeyCommitted` message alongside the `super()` call. A click
never runs `action_activate`: `FlowView.on_click` calls `self.activate()`
directly, bypassing the action/binding system entirely, so the override sees
only the keyboard path. `TextualChatApp.on_flow_view_key_committed` is what
performs the clipboard write, keyed off that message rather than `Selected`.
This is a direct, ring-free path: `/copy N` addresses one of the last
`COPY_BUFFER_MAX` **agent replies** by ordinal, whereas the cursor points at
one exact, arbitrary entry of any kind (a user line, a tool result), so there
is no ordinal to resolve and no reason to go through the ring.

**#4697 further split Space itself**, on top of the Enter/Space-vs-click split
above: `_CursorFlowView` overrides Space's own `BINDINGS` entry to
`action_toggle_fold` instead of upstream's `action_activate`. Outside the text
cursor below, Space no longer reaches `action_activate`/the clipboard at all —
it posts `ToggleFoldRequested` to fold/unfold the highlighted entry's tool
detail (`#4691` §6 owner ruling: highlight movement stopped auto-expanding/
folding tool detail, so a dedicated open/close key was needed). Inside the
text cursor (`cursor_visible`), `action_toggle_fold` falls through to
`action_activate()` — the same clipboard-copy path Enter always takes — so an
in-progress text selection is never disrupted by a stray fold. Enter's own
binding and everything above about the Enter/Space-vs-click split is
unaffected; only Space's *outside-text-cursor* behavior moved.

**`r`** submits a bare `/rewind` through the ordinary submit seam — the same
path a composer-typed `/rewind` takes, so the checkpoint picker and rewind's
destructive-action path are untouched. It is a fast keyboard entry point,
**not** a jump to the checkpoint belonging to the cursor's entry: the
conversation log's `ChatMessage.seq` and the WAL seq that
`AgentRegistry.list_rewind_points` addresses are different sequence spaces
with no correlation wired anywhere, so a targeted jump would need new
plumbing rather than a new binding.

The cursor's position is marked by the addressed-row rail described under
*Textual TUI gutters* above. Its keys live in `CONVERSATION_CURSOR_KEYS`
(`textual_chat/chrome.py`) for the Help pane.

### Textual TUI conversation tree — Group nesting (#4691)

Three levels of `FlowView.Entry` nesting, one mechanism per level, built on
flowview's own `Entry.append_child`/`.children`/`.collapsed`/`.toggle_collapsed()`
primitives — reyn adds no tree data structure of its own:

- **Turn Group** — a `kind="user"` row (the line that OPENS a turn) is every
  completion Group of that turn's PARENT.
- **Completion Group** — a `kind="agent"` row that dispatched tool calls is
  its own tool rows' PARENT.
- **Tool rows** — leaves, nested under whichever completion Group's `call_id`
  they carry.

`_resolve_append_parent` (`textual_chat/app.py`) makes this recursive without
per-level code: it checks (1) a `_call_parents[call_id]` match first, (2) the
CURRENT turn's own open parent (`_current_turn_parent`) if no call-level match
and the frame isn't itself `kind="user"`, (3) flat top-level otherwise (a
legacy/restored row, an op-loop caller with no `call_id`, or no turn open).
A turn's first `kind="agent"` row hits (2) — it has no parent of its OWN
yet — then registers itself into `_call_parents` (`_register_call_parent`),
so every later row sharing that `call_id` finds it via (1). One `call_id`
lookup, checked at two different moments, produces both tree levels.

**Registration is provider-independent** (#4777, owner-reported): gated on
`dispatched_tool_calls`, a REYN-OBSERVED fact `router_loop.py` stamps from
the LLM result's own `tool_calls` list — never on the provider's own
self-reported `finish_reason` string, which some providers never emit as
`"tool_calls"` even when they did dispatch one, silently disabling Group
construction end-to-end on those providers if gated on it (found live, not
in a test suite written against a provider that reports it correctly). A
terminal reply that dispatches no tools still registers (harmless — nothing
ever looks up an unused `call_id`) but never spins, since there is nothing
for it to wait on. Both entry-creation call sites — the ordinary
non-streaming append AND `_handle_agent_delta_event`'s first-streamed-delta
creation — route through the same `_append_frame`/`_resolve_append_parent`
pair, closing an earlier streaming-bypass class where a streamed reply's own
entry-creation path never registered as a Group parent or nested under its
turn at all, no matter what the non-streaming path did.

**Defaults, opposite by design (owner ruling):**

- The **turn Group defaults OPEN**. Its `RUNNING` state is SET, not derived,
  at promotion (`_handle_turn_started_event`) — deriving it from zero
  children (there are none yet) would show no state at all, and deriving it
  incrementally as completion Groups settle mid-turn would flicker the row
  to `SUCCESS` between calls while the turn itself is still in flight.
  Settling happens exactly once, at the turn's own end
  (`_settle_turn_parent`, called from the `_TURN_END_EVENT_TYPES` leg of
  frame pumping).
- A **completion Group defaults COLLAPSED**, called at registration time
  (`entry.collapse()` inside `_register_call_parent`, guarded on
  `dispatched_tool_calls`) — even though the entry may still be a leaf with
  no child arrived yet. Before textual-flowview 0.22.0 this was a
  documented no-op on a leaf, worked around by re-asserting `.collapse()`
  at the entry's own `append_child` call site; 0.22.0's own fix (release
  notes: a child appended later "walks its ancestors and is born folded")
  made collapse state stick on any live entry, leaf included, so that
  workaround is gone.

**Open/close: `Space`, only on a row that HAS children.**
`on_flow_view_toggle_fold_requested` checks `entry.children` truthy FIRST —
an exclusive signal for a Group parent, since `append_child` is called from
exactly one place — and calls `entry.toggle_collapsed()` (flowview's own
fold/unfold primitive; flowview's own `za`/`zR`/`zM` z-prefix key family
already reaches the same primitive through a separate path — Space just
wires the SAME primitive to a second key, not a new one) before falling
through to the settled-tool-row detail-expand check described under
*Textual TUI keyboard cursor* above; a Group parent's own `meta` carries no
`_RESULT_KIND_KEY`, so without this check-order it would never reach a fold
path at all. A collapsed Group parent shows its child COUNT next to the
state glyph (`"(2 folded)"`, dim) rather than a bare, uninformative row —
deliberately minimal, no summary wording, no icon vocabulary.

**Parent state is DERIVED from children, recomputed on every child settle**
(`_recompute_parent_state`, called from each child's own settle path):
`RUNNING` wins if any child still is; among terminal states `ERROR` wins
over `SUCCESS` (one failed child taints the whole call); `CANCELLED` counts
as neither — an orphan is not a failure. The turn parent reuses this same
function one layer up at its own settle time, passing any one of its
children so the recompute walks across every completion Group of that turn.
A turn that ends with no completion Group having landed under it (cancelled
before the first call) settles `CANCELLED` — there is nothing to recompute
FROM, and nothing was observed to call a success.

**A Group parent's own line recedes while EXPANDED** (children visible) —
`palette.TOKENS["@recede@"]` (an SGR `dim` attribute, not a colour — CLAUDE.md's
TUI colour policy token, distinct from the plain REPL renderer's own hex
`_CC_DIM`), applied to the parent's body only; children are unchanged. Owner
ruling: the parent weakens, never the children strengthen — the reverse
would make a Group's children stand out MORE than an ordinary row, changing
how the whole conversation reads. Excluded when collapsed (that state
already recedes for a different reason — naming a hidden count, not sitting
as a structural peer beside visible children).

### Textual TUI text cursor (#3507, #3692)

flowview 0.13 replaced the earlier entry-gated cursor-entry step with an
always-on per-character text cursor: `c` (flowview's own key, `toggle_cursor`)
just shows or hides the cursor block, and visual mode (`v`/`V`…`y`) is the
one real mode — there is no separate mode to enter or leave any more. This is
what the entry-level highlight cannot do: the finest keyboard position used to
be a whole entry, so selecting *part* of a long reply had no keyboard route at
all.

Every one of these keys is **flowview's own keymap** (`hjkl w b e 0 $ ^ gg
G v V y zz zt zb Ctrl-E Ctrl-Y Ctrl-D Ctrl-U Ctrl-F Ctrl-B Esc`, always live,
plus `*` / `n` / `N` to search the selection). reyn declares **no key binding
of its own for any of them, including `c`** — deliberately, so the keymap
cannot drift from upstream's; a test asserts that absence. One consequence:
the half/full-page scroll bindings (`Ctrl-D`/`Ctrl-U`/`Ctrl-F`/`Ctrl-B`,
flowview 0.10.0) were adopted **automatically** by the same no-own-bindings
rule — a version bump that extends upstream's keymap needs no reyn-side
change (#3624: looked at, adopted implicitly rather than as a separate
feature decision). `c` reaching flowview reached the same way — the 0.13 pin
bump removed reyn's own `c` binding along with the whole entry/exit wiring
it drove, rather than rebinding `c` to something new.

The interaction that matters for this surface: `set_current` (flowview 0.13.1)
moves the text cursor onto the **currently addressed entry** without moving
the addressed-row rail itself, so `c` (show/hide the cursor) and cursor
motion do not drag the rail along with them — 0.13.0 regressed this (`c`
moved the addressed row), fixed in 0.13.1. `FlowView.row_count` /
`row_text(y)` / `entry_at_row(y)` are the row-level primitives the text
cursor is built on, available to any consumer that needs to map content rows
back to entries.

A selection — whether from `y` (yank) or a mouse drag — covers the **body
columns only** (flowview 0.9.0): the gutters are decoration, like a scrollbar,
so a yank carries the message text and never a state glyph, an elapsed label,
or a token figure. `row_text(y)` is body-only for the same reason. Reading a
*gutter* off `get_selection` therefore reports an empty gutter for a perfectly
painted one — the surface that answers "is the gutter on screen?" is
`render_line(y)`, Textual's own paint surface.

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
transport produces (display outbox + the renderer-relevant audit-event subset).
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
an `intervention_answer_submitted` audit-event (#3300 — event-ifying the LAST
outbox `kind="user"` broadcast site, following the `user_submitted` precedent
below exactly). A submitted turn (`Session.submit_user_text`) emits the
sibling `user_submitted` audit-event (#3300 P1 C — replacing an earlier
outbox-echo write, a category error: an INPUT written into the display/OUTPUT
channel). Both ride the SAME unified frame stream as an `EventFrame`
(`_TURN_AND_ANSWER_EVENTS`, `transport/frames.py`) — the encode/decode is
generic (`transport/agui/protocol.py`), so no wire changes were needed for
either event type. Every attached surface's event→display handler
(`ConsoleChatRenderer.on_audit_event` / `InlineChatRenderer.on_audit_event` /
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
  the audit-event emit and the id reaching the caller).
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
