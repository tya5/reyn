# Engine-Design Contract

> Reyn's web UI is built on the **OpenUI** protocol, with a Reyn-specific
> Layer 1 schema (`reyn-ui/v1`). This document captures the architectural
> framing — *why* the contract exists, *what* it binds, *how* the layers
> compose, and the *evolution policy*. The protocol itself is specified
> in [docs/openui/](../openui/).

---

## Why this is a top-level document

Until recently, producing a polished UI design was expensive enough that
core engine and visual design were co-developed by the same team in the
same repo. Now that a designer can ship a complete, contract-conforming
UI from `claude.ai/design` in hours, **the interface between engine and
design becomes Reyn's most consequential external surface** — comparable
in importance to the engine's internal principles (P1–P8) for the
project's success.

The killer feature this contract enables is **design swappability**:
end users can drop a new design into a directory and `reyn web` shows
it, without rebuilding anything. That is the user-facing impact.

Designs and engine should evolve on independent release cadences, bound
only by this contract.

---

## The three layers

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 0 — OpenUI host adapter protocol                      │
│   window.OPENUI_HOST  (invoke + listen)                     │
│   window.OPENUI_DATA  (initial data, schema-shaped)         │
│   window.OPENUI_SCHEMA (e.g. "reyn-ui/v1")                  │
│   window.OPENUI_DESIGN_MODE (designer preview vs embedded)  │
│   spec: docs/openui/spec/layer-0.md                         │
│   domain-neutral, transport-neutral                         │
└─────────────────────────────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│ Layer 1 — reyn-ui/v1 domain schema                          │
│   ReynUiData shape (Agents, Runs, Events, SkillGraph, ...)  │
│   Actions: agent.submit, agent.intervention.answer, ...     │
│   Channels: agent.message, run.started, state.delta, ...    │
│   Components: TodayScreen, Conversation, SkillGraphPage, .. │
│   spec: docs/openui/schemas/reyn-ui-v1/                     │
│   Reyn-specific, but agent-domain-shaped                    │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│ Layer 2 — implementation-specific (inside Layer 1 data)     │
│   Reyn's Skill / Phase / Workspace / Topology / Control IR  │
│   Carried as opaque structured JSON inside ReynUiData.      │
│   The schema documents their shape but Layer 0 does not     │
│   interpret them — they pass through verbatim from Reyn     │
│   gateway to design.                                        │
│   (This satisfies Reyn's P7 on the host adapter side.)      │
└─────────────────────────────────────────────────────────────┘
```

The full canonical spec for each layer lives in
[docs/openui/](../openui/). This document tells you why we chose this
shape, how it relates to Reyn's principles, and how it evolves.

---

## Why OpenUI rather than an existing protocol

Two adjacent protocols already exist and were evaluated:

### AG-UI (Agent-User Interaction Protocol)

[ag-ui-protocol/ag-ui](https://github.com/ag-ui-protocol/ag-ui) — open,
event-based protocol for "any AG-UI compliant frontend" ↔ "any AG-UI
compliant agent backend". Adopted by Microsoft Agent Framework, Oracle
Open Agent Specification, CopilotKit, Google ADK, AWS Strands, Mastra,
Pydantic AI, Agno, LlamaIndex, AG2, LangGraph, CrewAI. ~28 standardised
event types. Real industry traction.

**Why we did not adopt as-is**:

1. AG-UI is **agent-specific by scope**. Its documentation explicitly
   binds it to "any agentic backend". Reyn's longer-term vision keeps
   the door open for the same UI host pattern to apply to non-agent
   tools (file managers, IDEs, CLI utilities). A locked-in agent
   protocol forecloses that.
2. **The killer feature for end users is design swap, not backend
   interop**. AG-UI optimises for "swap the LLM provider" /
   "swap the frontend framework"; OpenUI optimises for "swap the
   visual design". These are different axes — AG-UI doesn't
   directly enable the latter.
3. AG-UI **does not replace design work**. Even fully adopted, Reyn
   would still need custom designs for Today / Library / SkillGraph /
   RunTimeline / Permissions screens — CopilotKit covers only chat
   plumbing (~30-40% of Reyn's surface). The cost of refactoring
   Reyn's gateway to AG-UI events did not buy a corresponding cost
   reduction in design work.
4. **Reyn-specific concepts** (Skill, Phase, Workspace, Topology,
   Control IR) would have to live inside AG-UI's `Custom` event type,
   which is the protocol's escape hatch. Going through Custom for our
   core concepts means we get less of AG-UI's typed event vocabulary
   and more of its bookkeeping.

### MCP (Model Context Protocol)

Adjacent, but solves a different problem (LLM ↔ external
tool/resource). Not a UI host protocol. We borrow its **governance
philosophy** (neutral name, spec-first, multi-vendor adoption from day 1)
but not its surface.

### What we kept from AG-UI

OpenUI is informed by AG-UI even though we did not adopt it:

- **JSON Patch (RFC 6902) for state diffs** — AG-UI's StateDelta event
  taught us this is a clean, tooling-friendly choice. Our
  `state.delta` channel uses it.
- **Lifecycle event vocabulary** — `run.started` / `run.finished` /
  `phase.started` / `phase.finished` echo AG-UI's `RunStarted` /
  `StepStarted` directly.
- **Spec-first, neutral naming** — same governance shape as MCP / LSP.

The full evaluation is in the project memory
(`project_engine_design_contract_standard.md`); this document captures
the result.

### Future re-evaluation

If AG-UI traction reaches a point where Reyn's positioning suffers from
*not* being AG-UI-compatible, the door is open: a future Layer 1
schema (`ag-ui/v1`) could be added, the host could route appropriately,
and existing reyn-ui/v1 designs would keep working. We chose the path
that preserves option value rather than committing now.

---

## Layer responsibilities

### Layer 0 (`docs/openui/`) — the universal protocol

**Owns**: the four `window.OPENUI_*` globals, `invoke` / `listen`
semantics, the manifest format, action / channel naming rules, the
reserved `data.refetch` action, JSON Patch convention for `state.delta`.

**Does not own**: any domain-specific data shape, any specific action /
channel name beyond reserved, any component contract.

**Stability**: Layer 0 is intentionally minuscule. Changes here are
breaking for every host and every design across all schemas. Bumps
should be rare and well-warranted. Currently `1.0`.

### Layer 1 (`docs/openui/schemas/reyn-ui-v1/`) — Reyn's domain schema

**Owns**: the `ReynUiData` shape, the set of actions
(`agent.submit`, `data.refetch`, `permission.update`, …), the set of
channels (`agent.message`, `run.started`, `state.delta`, …), and the
component contracts (App / Studio surfaces, prop shapes).

**Does not own**: visual chrome, density, color palette, component
implementation, brand voice. Those are owned by individual designs.

**Stability**: SemVer. 1.x.y is additive; 2.0 is breaking. See the
schema's [README](../openui/schemas/reyn-ui-v1/README.md) for the
versioning policy and changelog.

### Layer 2 (inside Layer 1 data) — Reyn-specific extensions

**Owns**: Skill, Phase, Workspace, Topology, Control IR op shapes —
all the engine-level concepts that the Studio face renders verbatim and
the App face hides behind humanized wrappers.

These types are typed in `data.types.ts` for documentation, but the
**Layer 0 protocol does not interpret them**. They are pass-through
opaque values. New skills, new phase types, new event types appear in
Studio without bumping the schema.

This is the design-side analogue of P7 (OS skill-agnostic): the schema
is skill-agnostic too.

---

## Relationship to Reyn's principles (P1–P8)

CLAUDE.md's P1–P8 govern the **engine's internal coherence**. They are
the constitution of the OS. They say nothing about UI.

This contract governs the **engine's external surface to the design
layer**. It is to P1–P8 what HTTP is to a web server's internal
architecture: orthogonal, complementary, equally load-bearing.

Where the two interact:

- **P5 (Workspace)**: design-installed files in
  `reyn/local/designs/<name>/` are workspace state. Edits and installs
  emit events.
- **P6 (Events)**: every design install / remove / select emits an
  event (`design_installed`, `design_removed`, `design_selected`).
  Design-related runtime debugging uses the same audit log as
  everything else.
- **P7 (OS skill-agnostic)**: Layer 1's data shape carries
  skill-domain values (artifact types, phase names) as opaque strings.
  The host produces them, the design renders them, neither interprets.
  A new skill or phase requires zero schema or contract changes.

---

## Evolution policy

Every change to a Layer 0 or Layer 1 spec is a PR that touches the
corresponding `docs/openui/` files. SemVer rules apply per layer.

### Patch (e.g. 1.0.0 → 1.0.1)

Documentation clarifications only. No behaviour change. No version bump
required for hosts or designs.

### Minor (e.g. 1.0 → 1.1)

Additive. Existing designs continue to work without modification.
Hosts implementing the older minor SHOULD upgrade lazily.

What counts as additive:

- New optional component (with `required: false`)
- New optional prop on existing component
- New action or channel
- New optional payload field on existing action
- New `data.types.ts` field

### Major (e.g. 1.x → 2.0)

Breaking. Designs targeting `1.x` do not work with hosts implementing
`2.0` and vice versa.

What counts as breaking:

- Removing or renaming a component / action / channel / data field
- Changing the type of an existing prop / payload field / event field
- Changing the meaning of an existing string identifier

Major bumps SHIP a migration guide and a deprecation period: at least
one minor release where the old form is marked deprecated but still
works, before the major bump removes it.

### Versioning of designs

A design's `design.yaml` declares the schema it targets:

```yaml
schema: reyn-ui/v1     # any 1.x.y
schema: reyn-ui/1.2    # any 1.2.x
schema: reyn-ui/1.2.3  # exact pin
```

The host accepts any compatible declaration. See
`design-distribution.md` for full details on the manifest.

---

## Implementation status

| Layer | Status |
|---|---|
| Layer 0 spec | ✅ docs/openui/spec/ |
| Layer 0 TypeScript types | ✅ docs/openui/types/ |
| Layer 0 JSON Schema validator (manifest) | ✅ docs/openui/schemas/manifest.schema.json |
| reyn-ui/v1 schema | ✅ docs/openui/schemas/reyn-ui-v1/ |
| reyn-ui/v1 TypeScript types | ✅ docs/openui/schemas/reyn-ui-v1/data.types.ts |
| Reyn host implementation (`OPENUI_HOST`, gateway endpoints) | ⏳ PR30 |
| Reference design (coral/) re-exported as reyn-ui/v1 compliant | ⏳ PR31 |
| `reyn design ...` CLI (install / list / remove community designs) | ⏳ Phase 2 |
| `@openui/validator` external library | ⏳ Phase 2-3 |
| Lifting `docs/openui/` into a standalone repo | ⏳ on traction |

---

## See also

- [docs/openui/README.md](../openui/README.md) — entry point for the
  OpenUI specification
- [docs/openui/spec/layer-0.md](../openui/spec/layer-0.md) — Layer 0
  protocol normative spec
- [docs/openui/schemas/reyn-ui-v1/](../openui/schemas/reyn-ui-v1/) —
  reyn-ui/v1 schema
- [claude-design-prompt.md](claude-design-prompt.md) — the prompt
  template Cowork pastes into Claude Design to generate
  reyn-ui/v1-compliant designs
- [multi-design-selection.md](multi-design-selection.md) — how the host
  picks among installed designs at runtime
- [design-distribution.md](design-distribution.md) — how community
  designs are installed, shared, and discovered
- [design_brief.md](design_brief.md) — the visual / brand specification
  for Reyn's two faces
- `CLAUDE.md` (project root) — engine-side principles (P1–P8)
