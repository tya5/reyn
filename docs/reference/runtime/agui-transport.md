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
  the current message type is `{"type": "user_message", "text": "..."}` (submit
  a turn).

Both are gated by the server's authentication context: a connection presents its
token as `?token=` or an `Authorization: Bearer <token>` header (same-machine
UDS connections are identified by OS peer credentials instead). An
unauthenticated connection is refused with `401` before any session is attached.

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
| `agent`           | `TEXT_MESSAGE_CONTENT` | the assistant reply text                  |
| `status`          | `TEXT_MESSAGE_CONTENT` | transient status line (`role: status`)    |
| `error`           | `RUN_ERROR`        | error text                                   |
| `trace`           | `CUSTOM`           | reyn tool/step trace line                    |
| `intervention`    | `CUSTOM`           | a prompt is displayed (answer round-trip is a later phase) |
| `presentation`    | `CUSTOM`           | a `present` op's render-node model (see *present-on-wire*) |
| control sentinels | `CUSTOM`           | `__end__` and client-local control kinds     |

Any display kind not in this table still round-trips losslessly (it falls back to
`CUSTOM` and is reconstructed from `_reyn`) — a new renderer kind can never
silently vanish on the wire.

### Working-indicator path (turn lifecycle + tool axis)

| reyn chat-event               | AG-UI event      |
|-------------------------------|------------------|
| `turn_started`                | `RUN_STARTED`    |
| `turn_settled` / `turn_completed` / `turn_cancelled` | `RUN_FINISHED` |
| `tool_called`                 | `TOOL_CALL_START`|
| `tool_returned` / `tool_failed` | `TOOL_CALL_END`|
| `user_answered_intervention`  | `CUSTOM`         |

These eight are the exact set the renderer's working / running / waiting-for-you
indicator consumes; the transport forwards precisely this set.

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

## Reconnect

On connect (or reconnect) the server replays, before any live event:

1. `MESSAGES_SNAPSHOT` — the display backlog (the messages already produced), so
   a reconnecting client rebuilds its scrollback; then
2. `STATE_SNAPSHOT` — the status read-model above.

Live events (and `STATE_DELTA`s) follow.

## Local ≡ remote

The server serializes the SAME unified frame stream the local in-process
transport produces (display outbox + the renderer-relevant chat-event subset).
The AG-UI transport adds only wire framing, never new render semantics — so the
remote renderer's display bytes and working-indicator transitions are identical
to the local ones.

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
| Tool       | 5                | 2 (→3 planned) | **next-phase** for `TOOL_CALL_RESULT` (lands with a later phase's HITL frontend-tool answer round-trip); the `TOOL_CALL_ARGS`/`_CHUNK` pair is **intentional-scope** (a tool call is already complete by the time reyn emits it — there is no in-flight args stream to chunk) |
| Text       | 4                | 1           | **intentional-scope** — reyn's outbox delivers whole messages, not token deltas, so a single `TEXT_MESSAGE_CONTENT` per message is the honest mapping; there is no `_START`/`_END`/streaming-chunk phase to map |
| Special    | 2                | 1           | **intentional-scope** — reyn-private payloads are always structured (`CUSTOM`); the standard `RAW` passthrough event has no reyn use case |
| Activity   | 2                | 0           | **intentional-scope** — reyn has no direct analog; the same information is already carried by the frame stream + `STATE_*` |
| Reasoning  | 7                | 0           | **future-candidate** — the highest-value gap (see below) |

**Totals**: reyn natively emits **9 of the 28** active-roster standard events
(10/28 counting the `CUSTOM` catch-all itself as one). The 28-event roster is
Lifecycle (5) + Text (4) + Tool (5) + State (3) + Activity (2) + Reasoning (7)
+ Special (2), tallied from the canonical AG-UI event reference
(<https://docs.ag-ui.com/concepts/events>). That reference self-reports up to
~34 event names in total when meta/deprecated/draft entries outside the
active roster are counted — the exact figure is spec-version dependent, so
this page tracks the 28-event active roster, not the larger number.

### Why the gaps are dispositioned the way they are

- **Reasoning (future-candidate, highest value).** reyn already treats
  reasoning as a first-class concept; today a reasoning trace rides the
  `trace` display kind → `CUSTOM`, invisible to a generic client. Mapping it
  to the standard `Reasoning*` events would let a generic AG-UI client render
  it directly. The gate that must be respected before shipping this: reyn's
  **reasoning-display toggle** — when the operator has reasoning display
  turned off, nothing should be emitted on the wire either, so a mapping must
  not become a chain-of-thought exposure path that bypasses that toggle.
- **Tool result fidelity (non-blocking, low cost).** A generic client cannot
  currently distinguish `tool_failed` from `tool_returned` — both collapse to
  the standard `TOOL_CALL_END` event, with the failure fact recoverable only
  from `_reyn` (which a generic client skips). reyn-client fidelity is
  unaffected; a future pass could surface an error/status field on the
  standard `TOOL_CALL_END` payload itself for generic-client visibility, at
  low implementation cost.
- **Everything marked intentional-scope** reflects a real architectural
  difference (reyn's whole-message outbox, structured-only private payloads,
  no in-flight tool-args phase, no direct "activity" concept) rather than an
  oversight — closing these gaps would mean inventing streaming/chunking
  machinery reyn's design deliberately does not have, not fixing a bug.
