# The Engine-Design Contract

> **Why this is a top-level document**: until recently, producing a single
> polished UI design was expensive enough that "core" and "design" were
> co-developed by the same team in the same repo. Now that a designer
> can ship a complete, contract-conforming UI from `claude.ai/design` in
> hours, **the interface between engine and design becomes Reyn's most
> consequential external API** — comparable in importance to the engine's
> internal principles (P1–P8). Designs and engine should evolve on
> independent release cadences, bound only by this contract.
>
> This document is the canonical specification of that contract. The
> operational documents in `docs/web/` (prompt template, selection,
> distribution) are how this contract is exercised in practice.

---

## What the contract binds

The contract sits between two parties:

- **The engine**: `src/reyn/`, including the web gateway under
  `src/reyn/web/`. Owns data shape, runtime semantics, and the OS-level
  vocabulary (Phase, Skill, Agent, Run, Event, Workspace, etc.).
- **The design**: a `web/designs/<name>/` (or
  `reyn/local/designs/<name>/`, etc.) directory containing tokens,
  components, and pages. Owns visual chrome, density, vocabulary
  translation for the App face, and interaction details.

Each side may evolve on its own schedule **as long as the contract holds**.
A bundled engine release is compatible with all designs whose
`contract_version` is supported by that engine. A design is portable
across all engine releases that support its `contract_version`. This is
the same shape as POSIX (kernel/userspace), HTTP (server/client), or
SQL (engine/queries) — a standardised interface enabling independent
evolution.

---

## The four layers of the contract

The full IF is captured across four layers. Together they completely
define what the design needs to know about the engine, and vice versa.

### Layer 1 — Data shapes (engine → design)

What the engine sends, what the design must accept:

- **WebSocket message envelope** (`/ws/chat/{agent_name}`): the engine
  pushes JSON messages tagged with a `kind` from the OS-generic
  taxonomy: `agent`, `status`, `error`, `intervention`, `trace`,
  `skill_done`. Payload includes `text`, optional `meta` (run_id,
  skill_name, agent_name), and for `intervention` the `choices` and an
  acknowledgement token.
- **REST endpoint shapes** (`/api/agents`, `/api/skills`, `/api/runs`,
  `/api/topologies`, `/api/permissions`, `/api/budget/usage`): each
  documented under `src/reyn/web/routers/` with stable JSON shapes.
  All skill-domain values (artifact types, phase names, decision
  values) are passed through as opaque strings — the design never
  interprets them, only displays them.
- **Server config** (`/api/web/config`): the engine reports
  `default_design`, `available_designs[]`, `output_language`, and
  `contract_version` it implements.

### Layer 2 — Action surface (design → engine)

What the design sends, what the engine must accept:

- **User text submission**: `{type: "user_message", text: string}` over
  the WebSocket. Engine routes to `session.submit_user_text`.
- **Intervention answer**: `{type: "intervention_answer", id: string,
  choice_id?: string, text?: string}` over the same WebSocket.
- **Permission decision** (REST): `PUT /api/permissions/{key}` with a
  rule body.
- **Design selection** (in-shell, no engine round-trip): localStorage
  + URL param. Reported back to the engine only as a usage signal, not
  a state change.

### Layer 3 — Component contracts (design ↔ shell ↔ design)

What every design's components must look like, so the shell's adapters
can pass props uniformly:

- **Per-face required components** with exact prop interfaces. See
  [claude-design-prompt.md § Component contracts](claude-design-prompt.md).
- **Token schema**: `tokens.json` keys and shapes. See
  [claude-design-prompt.md § Token schema](claude-design-prompt.md).
- **Naming conventions**: PascalCase TSX files, page files end with
  `Page`, files live under `components/` or `pages/`.

The shell-side TypeScript types in `web/shell/contracts/v<MAJOR>/` are
the single source of truth at typecheck time. This document describes
their semantics; the `.ts` files are the precise definitions.

### Layer 4 — Anti-requirements (forbidden behaviour)

What designs must NOT do, regardless of what the rest of the contract
allows:

- No `fetch` / `XMLHttpRequest` / WebSocket calls inside design
  components. The shell injects all data via props.
- No global state imports (Zustand, Redux, Recoil, Jotai, …). The
  shell owns state.
- No bundler / framework configs (Tailwind, postinstall scripts,
  `package.json`). The shell owns the build.
- No hardcoded user-visible strings on the App face. All text comes
  via props, allowing i18n through the shell.
- No engine-vocabulary leakage on the App face (`phase`, `artifact`,
  `control_ir`, `event`, `validation`, `schema`).

These rules are statically checkable. Violations fail
`reyn design lint` at install time.

---

## Versioning

The contract uses **SemVer** under a single `contract_version` key.
Designs declare the version they target in `design.yaml`; the engine
declares the versions it supports in
`src/reyn/web/contracts/SUPPORTED.md`.

```
contract_version: "1.2.0"
                   │ │ └── patch — clarifications only, no semantic change
                   │ └──── minor — backward-compatible addition
                   └────── major — breaking change, designs must update
```

### What counts as each kind of change

**Major (breaking)** — designs targeting the previous major must be
updated:
- Removing a required component
- Changing the type of a required prop
- Removing a required token key
- Changing the WebSocket envelope keys
- Removing a REST endpoint or changing its response shape (other than
  additive)
- Renaming an existing kind in the message taxonomy

**Minor (additive)** — designs targeting an older minor still work:
- Adding a new optional component to the contract (designs without
  it: the shell falls back to the default design's version, or hides
  the feature)
- Adding a new optional prop to an existing component (designs
  ignore it; shell passes it only if present)
- Adding a new token key (designs without it: that token's theming
  degrades to a sensible default, set by the shell)
- Adding a new REST endpoint (designs only need it if they consume
  the new feature)
- Adding a new optional kind to the message taxonomy (designs
  rendering only known kinds keep working)

**Patch** — no behaviour change, only documentation or wording fixes
in this document.

### Compatibility check

`reyn design add` and the boot path of `reyn web` both check that the
design's `contract_version` major matches the engine's supported major,
and that the design's minor is ≤ the engine's minor.

```
design.yaml says contract_version: "1.2.0"
engine supports     1.4.x
                   ──────
                   compatible: same major, design's minor ≤ engine's minor

design.yaml says contract_version: "2.0.0"
engine supports     1.4.x
                   ──────
                   incompatible: major mismatch — refuse install with diagnostic

design.yaml says contract_version: "1.5.0"
engine supports     1.4.x
                   ──────
                   incompatible: design needs features the engine doesn't have
                   yet — refuse install with "upgrade Reyn to use this design"
```

---

## Evolution process

Every contract change goes through this flow. The aim is that contract
changes are visible, reviewable, and slow enough that the community has
time to keep up.

```
1. Propose a contract change as a PR touching ALL of:
     - this document (engine-design-contract.md) — the prose semantics
     - web/shell/contracts/v<MAJOR>/*.ts — the machine-readable types
     - claude-design-prompt.md — operational template
     - bundled designs under web/designs/ — show that the change is
       implementable; update each design to satisfy the new contract

2. Bump contract_version per SemVer rules:
     - patch  : SUPPORTED.md only
     - minor  : SUPPORTED.md + designs may opt into the new feature
     - major  : SUPPORTED.md + new directory web/shell/contracts/v2/,
                 deprecation note in v1, migration guide

3. For majors only: ship a deprecation period.
     - Announce in CHANGELOG: "v1 deprecated, removal in 2 minor versions"
     - Engine continues to support v1 for at least 2 minor releases
     - reyn design lint warns on v1 designs starting from the
        deprecation announcement
     - Removal in a future major release of Reyn itself (not just a
        contract minor)

4. Update community channels:
     - awesome-reyn-designs: tag designs by the contract major they
        target
     - Release notes link to the migration guide
```

Patch and minor changes can ship in any Reyn release. Major changes are
high-friction by design — the bar is "the existing contract genuinely
prevents Reyn from delivering value" rather than "we want to clean
things up".

---

## Why this contract is the central artifact

For most of software's history, "the UI" and "the engine" were
co-evolved. A redesign meant a code change. Designers and developers
worked together synchronously. The interface between them was implicit,
encoded in shared assumptions and reviewed by humans.

LLM-driven design tools (Claude Design, Figma AI, v0, etc.) are
collapsing the cost of producing a UI from "weeks of human effort" to
"hours of conversation". When the cost of producing a UI approaches
zero, the bottleneck shifts from "designing the UI" to "ensuring it
fits the engine cleanly". The IF stops being implicit and becomes the
**limiting reagent of the whole system**.

A few consequences for Reyn:

- **The IF is what makes the design ecosystem possible**. Without a
  stable, statically-checkable contract, community designs would be
  one-off integrations — high friction, low diversity. With it,
  publishing a design is `reyn design pack && git push`.
- **The IF lets the engine evolve faster, not slower**. A design that
  pins `contract_version: "1.2.0"` is unaffected by engine internals
  — Reyn's runtime can be rewritten arbitrarily as long as the
  contract holds. This is the same trick HTTP played for the web.
- **The IF is the OSS competitive moat**. Reyn's value is not just the
  engine; it's the engine **and the surface area on which a design
  community can grow**. Whoever defines the design contract well in
  this category will own the gravitational pull.

That is why this document exists, and why changes to it are reviewed
with the same gravity as changes to CLAUDE.md's P1–P8.

---

## Implementation milestones

The contract is being established progressively. Current state and
near-term roadmap:

| Milestone | Status |
|---|---|
| Layer 1 (data shapes) defined and shipped via `feat/web-gateway` | ✅ done |
| Layer 4 (anti-requirements) documented in `claude-design-prompt.md` | ✅ done |
| Layer 3 (component contracts) prose in `claude-design-prompt.md` | ✅ done |
| Layer 3 machine-readable in `web/shell/contracts/v1/*.ts` | ⏳ frontend phase |
| Layer 2 (action surface) for `intervention_answer` over WS | ⏳ frontend phase |
| `contract_version` declared in `design.yaml` and validated at install | ⏳ `reyn design ...` CLI |
| `web/shell/contracts/SUPPORTED.md` (engine-side declaration) | ⏳ frontend phase |
| Bundled `web/designs/<name>/` set with at least one canonical design | ⏳ post-Claude-Design first export |
| Deprecation pipeline (warn on stale contracts) | ⏳ on first minor bump |

The vision in [design-distribution.md](design-distribution.md) requires
all of the above to be real before community publishing is a smooth
loop. The current focus is to land the milestones above in order; this
document defines the destination.

---

## Relationship to Reyn's principles

CLAUDE.md's P1–P8 govern the **engine's internal coherence**. They are
the constitution of the OS. They say nothing about UI.

This contract governs the **engine's external surface to the design
layer**. It is to P1–P8 what HTTP is to a web server's internal
architecture: orthogonal, complementary, and equally load-bearing for
the project's success.

Where the two interact:

- **P5 (Workspace)**: design-installed files in
  `reyn/local/designs/<name>/` are workspace state. Edits and
  installs emit events.
- **P6 (Events)**: every design install / remove / select emits an
  event (`design_installed`, `design_removed`, `design_selected`).
  Design-related runtime debugging uses the same audit log as
  everything else.
- **P7 (OS skill-agnostic)**: the contract's data shapes are
  skill-agnostic. Skill-domain values (artifact types, phase names)
  flow through the IF as opaque strings — the design renders them,
  never interprets them. A new skill or new phase requires zero
  contract changes.

This last property is what makes the contract sustainable across
arbitrary skill / phase evolution. It is the design-side equivalent
of P7.

---

## See also

- [claude-design-prompt.md](claude-design-prompt.md) — how Claude
  Design is constrained to produce contract-conformant exports
- [multi-design-selection.md](multi-design-selection.md) — how the
  shell selects among installed designs at runtime
- [design-distribution.md](design-distribution.md) — how designs are
  published, installed, and discovered
- [design_brief.md](design_brief.md) — the visual / brand
  specification for Reyn's two faces
- `CLAUDE.md` (project root) — engine-side principles (P1–P8)
