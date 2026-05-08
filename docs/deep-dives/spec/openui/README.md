# OpenUI — Reyn's web UI contract

> **Scope** — this is the contract between Reyn (the agent runtime) and any
> web design that wants to render Reyn's state. It is shipped inside the
> Reyn repository because it exists to serve Reyn's web UI needs.
>
> The protocol is designed *as if* it could be reused by another host or
> domain in the future, but **we make no claim that it is a neutral
> multi-vendor protocol**. Calling it one without traction (a second host,
> a second schema author) would be premature; protocols only become
> protocols once independent adopters validate the abstraction. For now,
> "OpenUI" is shorthand for "the layered Reyn-web contract".

A small, transport-light contract for separating **how an application's
state flows** from **how it is rendered**. Reyn ships with a default
design and the contract that lets that design — and, in the future,
any drop-in replacement — render the same state.

OpenUI is split into three layers:

```
Layer 0 — Host adapter contract
            window globals + invoke + listen + manifest

Layer 1 — Domain schemas
            reyn-ui/v1 declares: data shape, action set, channel set,
            required component contracts

Layer 2 — Implementation-specific extensions
            Reyn's Skill / Phase / Workspace / Topology types,
            carried in Layer 1 data shape via opaque pass-through
```

## What's actually being delivered

The headline experience Reyn builds on top of this contract is **two
co-existing UIs from one engine**:

- **App view** — end-user surface. Conversational, hides engine vocabulary
  ("phase", "artifact", "control IR"), shows progress in domain language.
- **Studio view** — operator / developer surface. Shows the engine in
  its full glory: phase graphs, control IR, WAL events, permissions.

This App/Studio split is the **primary product value** of the web layer.
It exists in no other LLM agent stack we surveyed: LangGraph Studio is
operator-only, LobeChat / OpenWebUI are user-only, Temporal Web is
operator-only. Reyn ships both from the same engine state.

**Design swappability** is a secondary capability that the layered
contract makes possible: any design that targets `reyn-ui/v1` can be
dropped in without a build step. This is useful for org branding and
internal A/B exploration but is *not* the headline feature, and we are
deliberately deprioritising the "switch designs at runtime" feature
(`reyn design` CLI, multi-design directory layout) until v1.x.

## Status

Pre-1.0 alpha. Reference host (Reyn shell) and reference design
(`reyn-default`) are under active development. The contract may make
breaking changes between minor versions until 1.0.

If a second independent host or domain adopts this contract — at which
point the "neutral protocol" claim becomes defensible — the directory
can be lifted into a standalone repository (`openui-spec`); names and
file paths are chosen so a `git mv` is the only structural move
required. Until then, the path remains under `reyn/docs/deep-dives/spec/openui/`.

## Why this shape (not AG-UI, not custom RPC)

We considered adjacent work and intentionally did not adopt them:

- **AG-UI** (Agent-User Interaction Protocol) — well-designed but
  scoped specifically to agent backends. Adopting it would constrain
  this contract to the agent domain, while we want headroom for
  non-agent Reyn-adjacent CLI tools (file explorers, log viewers, etc.)
  that may eventually share rendering primitives. AG-UI compatibility
  may be added in the future as a Layer 1 schema (`ag-ui/v1`) so a
  Reyn host can be made AG-UI-compliant by routing.
- **MCP** (Model Context Protocol) — solves a different problem
  (LLM ↔ external tool/resource). We borrow naming conventions but
  not the transport.
- **Tauri's `invoke` / `listen`** — desktop-app runtime, not a web
  contract. We borrow the API shape (`invoke` for RPC, `listen` for
  streams) because it is well-tested and minimal.

The contract is **transport-light** (`window`-based, no required network
protocol), **domain-neutral within Reyn's reach** (a schema-tagged Layer
1 selects the domain), and **drop-in-friendly** (no build step required
for a new design).

For Reyn's specific evaluation that led to this shape rather than AG-UI,
see the rationale section in `docs/deep-dives/spec/design/engine-design-contract.md`.

---

## Layered contract

| Layer | Defines | Document |
|---|---|---|
| **Layer 0** | Host adapter contract — the four `window.OPENUI_*` globals, the `invoke` and `listen` functions, the manifest format that schemas extend, and the action / channel naming rules. Host- and design-agnostic. | [spec/layer-0.md](spec/layer-0.md) [spec/manifest.md](spec/manifest.md) [spec/action-channel-naming.md](spec/action-channel-naming.md) |
| **Layer 1** | Domain schemas — each one declares an identifier (`<domain>/<version>`), a `Data` shape, a set of actions, a set of channels, and the components a design must export. Hosts implement one or more schemas; designs target one. | [schemas/reyn-ui-v1/](schemas/reyn-ui-v1/) — first reference schema |
| **Layer 2** | Implementation-specific data inside Layer 1 — opaque pass-through values (e.g. Reyn's Skill / Phase / Workspace types). Layer 0 and 1 never interpret these; they flow through as JSON. | (no spec; carried inside Layer 1 data) |

---

## Quick read

A typical OpenUI exchange looks like this:

```js
// HOST side (e.g. Reyn shell)
window.OPENUI_DATA = await fetch('/api/web/initial-data').then(r => r.json());
window.OPENUI_SCHEMA = "reyn-ui/v1";
window.OPENUI_DESIGN_MODE = false;

const ws = new WebSocket("/ws");
window.OPENUI_HOST = {
  invoke: async (action, payload) => { /* dispatch on action, send via ws or fetch */ },
  listen: (channel, handler) => { /* subscribe to ws messages of this channel, return unsubscribe */ },
};

// Then load the design's entry point:
//   <iframe src="/designs/<chosen>/index.html">
//   or  <script type="text/babel" src="/designs/<chosen>/screens.jsx">

// DESIGN side (the file in /designs/<slug>/)
const initial = window.OPENUI_DATA;
renderInitial(initial);

document.querySelector("#submit").addEventListener("click", () =>
  window.OPENUI_HOST.invoke("agent.submit", { agentId, text })
);

const unsubscribe = window.OPENUI_HOST.listen("agent.message", (msg) =>
  appendToConversation(msg)
);
// later: unsubscribe();
```

That is the entire surface. Layer 1 schema fills in what `action` strings,
`channel` strings, and `OPENUI_DATA` shape mean for a given domain.

---

## Index

- [spec/layer-0.md](spec/layer-0.md) — Host adapter contract (the four
  globals, `invoke`, `listen`, lifecycle, error semantics, state diff format)
- [spec/manifest.md](spec/manifest.md) — Layer 1 schema descriptor format
- [spec/action-channel-naming.md](spec/action-channel-naming.md) —
  namespace rules and reserved prefixes
- [types/host.d.ts](types/host.d.ts) — TypeScript types for the host adapter
- [types/manifest.d.ts](types/manifest.d.ts) — TypeScript types for manifest
- [schemas/manifest.schema.json](schemas/manifest.schema.json) — JSON Schema
  validating any Layer 1 manifest
- [schemas/reyn-ui-v1/](schemas/reyn-ui-v1/) — First reference Layer 1 schema

---

## Acknowledgements

Several design choices are inspired by adjacent work:

- **`invoke` / `listen` API shape**: Tauri's IPC, Chrome DevTools Protocol,
  Debug Adapter Protocol.
- **JSON Patch (RFC 6902) for state diffs on streaming channels**: AG-UI's
  StateDelta event taught us this is a clean, tooling-friendly choice.
- **Lifecycle event vocabulary** (`run.started`, `phase.started`, …):
  AG-UI's RunStarted / StepStarted naming is the direct inspiration; the
  exact set of events is left to each Layer 1 schema.
- **Layered contract style** (Layer 0 transport, Layer 1 domain): MCP's
  separation of transport vs server protocol is the precedent. We do not
  claim parity with MCP's neutral-protocol governance — that's a future
  earned by adoption, not a current claim.

The contract itself is intentionally smaller than any of the above; its
job is to be the thinnest possible surface a design and a host can both
target without fighting transport, framework, or domain assumptions.
