# OpenUI Protocol

A small, transport-light protocol for separating **how an application's state
flows** from **how it is rendered**. It is the contract that lets a designer
ship a web design, drop it into a host application's directory, and have the
host's data drive that design — without either side knowing the other's
implementation details.

OpenUI is split into three layers:

```
Layer 0 — Host adapter protocol  ←  this directory's spec/
            window globals + invoke + listen + manifest

Layer 1 — Domain schemas          ←  this directory's schemas/<schema-id>/
            e.g. reyn-ui/v1, file-manager/v1, image-viewer/v1
            each schema declares: data shape, action set, channel set,
            required component contracts

Layer 2 — Implementation-specific extensions
            e.g. Reyn's Skill / Phase / Workspace / Topology types,
            carried in Layer 1 data shape via opaque pass-through
```

The killer feature is **design swappability**: any design that targets a
schema can be dropped into a directory the host watches, and the user can
switch between designs at runtime — `rm -rf <slug> && unzip new_design.zip`.

---

## Status

This is the canonical spec, maintained alongside the first reference host
(Reyn). Once the spec stabilises and a second independent host or schema
adopts it, the directory may be lifted into a standalone repository
(`openui-spec`); names and file paths are chosen so a `git mv` is the only
move needed.

---

## Why "OpenUI" and not an existing protocol

Several adjacent protocols exist. We considered and intentionally did not
adopt them as the single answer:

- **AG-UI** (Agent-User Interaction Protocol) — well-designed but scoped
  specifically to agent backends. Adopting it would constrain OpenUI to the
  agent domain, while OpenUI's goal includes file managers, media players,
  IDE-like tools, and any CLI application that wants a swappable web UI.
  AG-UI compatibility may be added in the future as a Layer 1 schema
  (`ag-ui/v1`) so an OpenUI host can be made AG-UI-compliant by routing.
- **MCP** (Model Context Protocol) — solves a different problem
  (LLM ↔ external tool/resource). Excellent precedent for neutral
  multi-vendor protocol design; we follow its naming and governance
  philosophy.
- **Tauri's `invoke` / `listen`** — desktop-app runtime, not a web protocol.
  We borrow the API shape (`invoke` for RPC, `listen` for streams) because
  it is well-tested and minimal.

OpenUI sits at the intersection: it is **transport-light** (`window`-based,
no required network protocol), **domain-neutral** (a schema-tagged Layer 1
selects the domain), and **design-swap-first** (the file structure and
loading behaviour are designed so users can drop in new designs without a
build step).

For Reyn's specific evaluation that led to OpenUI rather than AG-UI, see
the rationale section in `docs/web/engine-design-contract.md`.

---

## Layered specification

| Layer | Defines | Document |
|---|---|---|
| **Layer 0** | Host adapter protocol — the four `window.OPENUI_*` globals, the `invoke` and `listen` functions, the manifest format that schemas extend, and the action / channel naming rules. Host- and design-agnostic. | [spec/layer-0.md](spec/layer-0.md) [spec/manifest.md](spec/manifest.md) [spec/action-channel-naming.md](spec/action-channel-naming.md) |
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

- [spec/layer-0.md](spec/layer-0.md) — Host adapter protocol (the four
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
- **Neutral name + spec-first, implementation-second governance**: Anthropic's
  Model Context Protocol set the precedent that worked.

The protocol itself is intentionally smaller than any of the above; OpenUI's
job is to be the thinnest possible surface a design and a host can both
target without fighting transport, framework, or domain assumptions.
