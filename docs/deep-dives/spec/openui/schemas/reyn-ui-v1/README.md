# reyn-ui/v1 schema

This is the first reference Layer 1 schema for OpenUI. It declares the
data shape, actions, channels, and component contracts that designs
target when they're authored for Reyn — a workflow-engine-driven agent OS.

## Files

| File | Role |
|---|---|
| [manifest.yaml](manifest.yaml) | Schema declaration (data type ref, actions, channels, components). The machine-readable contract. |
| [data.types.ts](data.types.ts) | TypeScript types for the data shape (`ReynUiData`) and the typed action / channel maps (`ReynUiActions`, `ReynUiChannels`) used with `TypedOpenUIHost`. |
| [components.md](components.md) | Prose component contract: per-component prop shapes, surface guidance, what each renders and what it doesn't. |

## Identifier

```
reyn-ui/v1     ← any 1.x.y compatible
reyn-ui/1.0.0  ← exact (this initial revision)
```

A design declares which it targets in its `design.yaml` (see
`docs/deep-dives/spec/design/design-distribution.md`). A host claiming to implement
`reyn-ui/v1` accepts any 1.x.y design.

## Versioning

This schema follows SemVer:

- **1.0.x** — clarifications, doc fixes only.
- **1.1**, **1.2**, … — additive: new components / channels / actions /
  optional payload fields. Existing 1.0 designs continue to work.
- **2.0** — breaking: removed or renamed components, payload field
  removals, channel event shape changes. Designs targeting 1.x must
  migrate.

Every minor bump SHOULD include a changelog entry in this file noting
what was added. Major bumps SHOULD ship a migration guide.

## Surface decomposition

The schema explicitly distinguishes **App** and **Studio** surfaces, per
Reyn's two-face design brief:

- **App face** — friendly, end-user-friendly. Hides engine vocabulary
  (`phase`, `artifact`, `run`, `event`, …). 6 required components.
- **Studio face** — dense, developer-facing. Surfaces engine vocabulary
  verbatim. 4 required components.
- **Shared** — only `ModeToggle`. App and Studio deliberately share no
  other visual chrome.

A design MAY ship only one face (`app` or `studio`); the host falls
back to a default for the missing face.

## Layer 2 — opaque pass-through types

The Studio face renders Reyn-specific concepts (Skill graph, Phase
status, Permission rules, RunEvent timeline, Workspace state) carried
inside `ReynUiData`. These types are typed in `data.types.ts` for
documentation, but **the Layer 0 protocol does not interpret them**.
They are pass-through values: the host produces them, the design
renders them, neither side parses meaning.

This satisfies Reyn's P7 (OS skill-agnostic) on both sides and lets new
skills / phases / events appear in Studio without bumping the schema.

## Compatibility with adjacent protocols

- **AG-UI**: not adopted as a runtime dependency, but adjacent good
  ideas were borrowed: lifecycle event vocabulary (`run.started` /
  `run.finished` / `phase.started` / `phase.finished` echo AG-UI's
  RunStarted / StepStarted), and JSON Patch (RFC 6902) for state
  diffs. The rationale for not adopting AG-UI directly is in
  `docs/deep-dives/spec/design/engine-design-contract.md`.
- **A2UI**: not used. A2UI is for agent-generated UI components
  (declarative widgets emitted by a model); reyn-ui/v1 designs are
  human / Claude Design-authored static React components.

## Adding a design that targets reyn-ui/v1

```bash
# Pick a slug
DESIGN=mydesign

# Drop in the design (must export the components in components.md)
mkdir -p reyn/local/designs/$DESIGN
cp -r path/to/your-export/* reyn/local/designs/$DESIGN/

# Verify (a future `reyn design lint` will check; for now it's manual)
ls reyn/local/designs/$DESIGN/    # should include your design's entry point
```

When `reyn web` starts, the design picker discovers your design and
labels it with the `local` source badge. Selecting it loads the design
with the host's `reyn-ui/v1` data + adapter.

## Changelog

### 1.0.0 (initial)

- 4 globals via OpenUI Layer 0.
- Data shape: `ReynUiData` covering Agent, Run, Event, SkillGraph, etc.
- 7 actions: `data.refetch`, `agent.{submit,intervention.answer,add,remove}`,
  `run.cancel`, `permission.{update,remove}`.
- 8 channels: `agent.message`, `run.{started,finished}`,
  `phase.{started,finished}`, `state.delta`, `budget.updated`,
  `permission.prompted`.
- 11 components: 6 App, 4 Studio, 1 shared (`ModeToggle`).
