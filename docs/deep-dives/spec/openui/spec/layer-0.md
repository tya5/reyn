# OpenUI Layer 0 — Host Adapter Protocol

This document specifies the protocol contract between an OpenUI host and an
OpenUI design. Both sides reference exactly four `window` globals and two
functions; everything else is delegated to a Layer 1 schema.

This layer is **transport-agnostic**: it makes no statement about how the
host talks to its backend. WebSocket, SSE, fetch, IPC — all are valid host
implementation choices, hidden from the design.

This layer is **domain-agnostic**: it makes no statement about what the
application is for. The Layer 1 schema (referenced via `OPENUI_SCHEMA`)
defines the domain.

---

## 1. The four globals

A conforming host MUST set the following globals on `window` before
loading the design's scripts. A conforming design MUST read these globals
without writing to them.

### 1.1 `window.OPENUI_HOST`

```typescript
interface OpenUIHost {
  invoke(action: string, payload?: unknown): Promise<unknown>;
  listen(channel: string, handler: (event: unknown) => void): () => void;
}
```

The host adapter object. See § 2 for `invoke` and § 3 for `listen` semantics.

### 1.2 `window.OPENUI_DATA`

The initial data the design renders from. Synchronous, populated by the
host before the design loads. Its shape is defined by the active Layer 1
schema.

```typescript
window.OPENUI_DATA: unknown;  // shape determined by OPENUI_SCHEMA
```

Designs MUST treat this as read-only. To request a fresh snapshot at
runtime, designs invoke the reserved action `data.refetch` (§ 4.1).

### 1.3 `window.OPENUI_SCHEMA`

A string identifying the active Layer 1 schema, in the form
`<domain>/<version>`.

```typescript
window.OPENUI_SCHEMA: string;  // e.g. "reyn-ui/v1"
```

Designs MAY check this at startup and refuse to mount if the schema does
not match what they were authored for. Hosts MAY refuse to load designs
whose declared schema does not match.

### 1.4 `window.OPENUI_DESIGN_MODE`

A boolean. `true` means the design is being previewed standalone (no
real host), `false` means it is embedded in a host. The default for
standalone HTML files SHOULD be `true`; hosts SHOULD set it to `false`
explicitly before loading the design.

```typescript
window.OPENUI_DESIGN_MODE: boolean;
```

In design mode, the design MAY render designer-only chrome (e.g. a
theme-tweaks panel) that the host does not want shown. Designs MUST NOT
call `OPENUI_HOST.invoke` or `OPENUI_HOST.listen` while in design mode if
the host adapter is absent — they should fall back to mock behaviour.

---

## 2. `invoke` — request-response actions

```typescript
invoke(action: string, payload?: unknown): Promise<unknown>;
```

`invoke` is the design-to-host RPC channel. The design names an action
(a string, see § 4 for naming rules) and supplies a JSON-serialisable
payload. The host:

1. Routes the action to its implementation.
2. Returns a `Promise` resolving to the action's return value, or
   rejecting with an `Error` instance.

### 2.1 Promise resolution

- **Resolves** with the action's return value (any JSON-serialisable
  value, or `undefined` for void actions).
- **Rejects** with an `Error` whose `message` field is human-readable.
  Hosts SHOULD also set `cause` or a custom property for machine
  consumption when the schema defines an error vocabulary.

### 2.2 Action lookup failure

If the host receives an action it does not know how to dispatch, it MUST
reject with a recognisable error:

```js
throw new Error(`unknown action: ${action}`);
```

Designs SHOULD treat unknown-action errors as a programmer mistake (the
schema is misaligned) and surface them, not silently swallow.

### 2.3 Cancellation

Layer 0 does not specify a cancellation primitive. Schemas may define
cancellation actions (e.g. `agent.cancel`); Layer 0 itself does not.

---

## 3. `listen` — subscribe to a channel

```typescript
listen(channel: string, handler: (event: unknown) => void): () => void;
```

`listen` is the host-to-design streaming channel. The design subscribes
to a named channel; the host calls the handler for each event published
on that channel.

### 3.1 Return value

`listen` returns an **unsubscribe function**. Calling it removes the
handler. Calling it more than once is a no-op.

```js
const unsubscribe = window.OPENUI_HOST.listen("agent.message", handler);
// later
unsubscribe();
```

### 3.2 Multiple subscribers per channel

A host MUST permit multiple handlers per channel (each `listen` call
registers an independent handler).

### 3.3 Unknown channel

If a design subscribes to a channel the host's schema does not declare,
the host MAY:

- Silently accept (and never emit), or
- Throw synchronously from `listen` with an `Error("unknown channel: ...")`.

Implementations SHOULD throw. Designs targeting a known schema should
never subscribe to unknown channels.

---

## 4. Reserved actions and channels

Layer 0 reserves a small core. Layer 1 schemas extend with their own.

### 4.1 `data.refetch` (action, reserved)

```typescript
invoke("data.refetch"): Promise<unknown>;
```

Returns a fresh snapshot of `OPENUI_DATA`-shaped data. The design MAY
choose to overwrite its local copy of `OPENUI_DATA`, or merge.

A host that does not support refetch (e.g. read-once initial data) MUST
either implement this as a no-op resolving with the same initial value,
or reject with `Error("data.refetch not supported")`.

### 4.2 No other reserved names

Every other action and channel string is owned by the Layer 1 schema.
The reserved-prefix policy is in [action-channel-naming.md](action-channel-naming.md).

---

## 5. Lifecycle

A typical session proceeds as:

```
HOST                                      DESIGN
1.  set OPENUI_DATA, OPENUI_SCHEMA,
    OPENUI_DESIGN_MODE=false,
    OPENUI_HOST = { invoke, listen }
2.  load design's entry point
                                          3.  read OPENUI_DATA, render
                                          4.  unsubscribe = listen("ch", h)
                                          5.  await invoke("act", payload)
6.  receive action, dispatch
7.  resolve / reject promise
                                          8.  emit("ch", evt) [host-side]
9.  ...                                   10. handler(evt)
                                          11. unsubscribe() [on unmount]
12. design unloaded
```

### 5.1 Mount

The host populates the four globals **before** loading the design's
entry script. Designs that observe missing globals MAY render a
designer-mode fallback (see § 1.4) but MUST NOT crash.

### 5.2 Unmount

When the design is unloaded (page navigation, design swap), it SHOULD
call every unsubscribe function it received from `listen`. Hosts SHOULD
NOT rely on this — their `listen` implementations should clean up
naturally when the design's JavaScript context is destroyed.

---

## 6. State diffs

Layer 0 itself does not require any specific format. However, **schemas
that include incremental state updates SHOULD use RFC 6902 JSON Patch**
as the channel event payload. This is a tooling-friendly, human-readable
standard.

```json
{
  "channel": "state.delta",
  "patch": [
    { "op": "replace", "path": "/agents/0/activity", "value": "researching" },
    { "op": "add", "path": "/recent_runs/-", "value": { /* ... */ } }
  ]
}
```

Designs applying patches SHOULD use a tested JSON Patch library; rolling
your own loses interop with tooling.

---

## 7. Error semantics

### 7.1 Errors during `invoke`

`invoke` MUST reject with an `Error` instance (not a plain object,
not a string). The `message` field is human-readable. Schemas MAY
define structured error codes via `Error.cause` or custom properties.

### 7.2 Errors in handler callbacks

If a `listen` handler throws synchronously, the host MUST catch the
throw, log it (host's discretion), and continue dispatching to other
handlers on the same channel. A throwing handler MUST NOT stop other
subscribers from receiving events.

### 7.3 Errors before mount

If `OPENUI_HOST` is undefined when the design tries to call `invoke`
or `listen`, the design SHOULD fall back to its designer-mode
behaviour (treating itself as standalone) rather than crash.

---

## 8. Data lifetime guarantees

- `OPENUI_DATA` is set **once** by the host before mount. Hosts MAY
  mutate it after mount but designs SHOULD treat it as read-only and
  use `invoke("data.refetch")` for fresh snapshots.
- `OPENUI_HOST` is set once. Hosts MUST NOT replace it after mount.
- `OPENUI_SCHEMA` is fixed for the session. Changing it requires a
  full reload of the design.
- `OPENUI_DESIGN_MODE` is fixed for the session.

---

## 9. Conformance summary

A **conforming host** MUST:

1. Set all four `window.OPENUI_*` globals before loading any design script.
2. Implement `OPENUI_HOST.invoke` returning a `Promise` per § 2.
3. Implement `OPENUI_HOST.listen` returning an unsubscribe function per § 3.
4. Implement the reserved `data.refetch` action per § 4.1.
5. Set `OPENUI_DESIGN_MODE = false`.
6. Reject `invoke` of unknown actions with a recognisable error.

A **conforming design** MUST:

1. Read the four globals only (never write).
2. Tolerate missing `OPENUI_HOST` (fall back to designer-mode).
3. Subscribe only to channels declared in the active Layer 1 schema.
4. Call its `unsubscribe` functions on teardown.
5. Treat `invoke` rejections as errors (display, retry, or fail).

A **conforming Layer 1 schema** MUST:

1. Have a stable identifier of the form `<domain>/<version>`.
2. Declare the shape of `OPENUI_DATA`.
3. Declare the set of actions (with payload and return-value shapes).
4. Declare the set of channels (with event shapes).
5. Declare the components a design exposes (with prop shapes).

See [manifest.md](manifest.md) for the schema-declaration format.
