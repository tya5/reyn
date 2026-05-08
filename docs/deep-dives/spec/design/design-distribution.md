# Design Distribution

> **Status**: **Deprioritised to v1.x.** Sits on top of the runtime
> design-selection layer (also v1.x). The Reyn web v0 ships with one
> bundled design (`reyn-default`). See
> [engine-design-contract.md](engine-design-contract.md) for the
> updated headline framing (App/Studio split is v0; runtime design
> swap and distribution are post-v1).
>
> This document is preserved as forward design so the eventual
> distribution pipeline can be built on a contract that already
> anticipates it.

> **Navigation**: this is the operational document for community design
> distribution. The architecture (3-layer model + AG-UI evaluation) is in
> [engine-design-contract.md](engine-design-contract.md). The contract
> spec is in [docs/deep-dives/spec/openui/](../openui/). The runtime selection mechanism
> is in [multi-design-selection.md](multi-design-selection.md).

> **Vision** (v1.x target): anyone with `claude.ai/design` access can
> author a Reyn design (targeting `reyn-ui/v1`), publish it as open
> source, and other users `reyn design add <source>` it to switch their
> UI without rebuilding anything.

This doc describes the publish / install pipeline that sits on top of
the runtime selection layer. Selection answers "which design am I
rendering right now?"; distribution answers "where do designs come from,
how do they get installed, and how do authors publish their own?"

---

## End-to-end story

```
                       ┌─────────────────────────────────────────────┐
                       │            Designer's machine                │
                       │                                             │
                       │  1. claude.ai/design + reyn-ui/v1 prompt    │
                       │     (see docs/deep-dives/spec/design/claude-design-prompt.md)  │
                       │  2. Iterate on canvas                       │
                       │  3. Export → zip                            │
                       │  4. reyn design init <slug>                 │
                       │     → scaffolds design.yaml + README        │
                       │  5. reyn design lint                        │
                       │     → static OpenUI Layer 0 + reyn-ui/v1    │
                       │       conformance check                     │
                       │  6. reyn design pack                        │
                       │     → produces a publishable zip            │
                       │  7. Push to GitHub (or any URL host)        │
                       └────────────────────┬────────────────────────┘
                                            │
                                            ▼  publish
                       ┌─────────────────────────────────────────────┐
                       │     github.com/<author>/reyn-design-<slug>  │
                       │     (a small repo: design files + manifest, │
                       │      tagged with the GitHub topic           │
                       │      `reyn-design`)                         │
                       └────────────────────┬────────────────────────┘
                                            │
                                            ▼  share link
                       ┌─────────────────────────────────────────────┐
                       │            End user's machine                │
                       │                                             │
                       │  reyn design add gh:<author>/reyn-design-<slug> │
                       │  → fetch, validate (manifest + Layer 0      │
                       │    + reyn-ui/v1 conformance), drop into     │
                       │    reyn/local/designs/<slug>/               │
                       │                                             │
                       │  reyn web (already running, or restart)     │
                       │  → picker shows the new design with a       │
                       │    "local" badge, end user selects it       │
                       │  → UI re-renders without rebuild            │
                       └─────────────────────────────────────────────┘
```

This is the "skills for the UI layer". Reyn already has community
extensibility for behaviour (skills under `reyn/project/` /
`reyn/local/`); this extends the same pattern to chrome.

---

## Source URI scheme

`reyn design add <source>` accepts several forms; the resolver picks
the right backend by prefix:

| Prefix | Example | Backend |
|---|---|---|
| `gh:` | `gh:author/reyn-design-warm` | GitHub HTTPS clone (defaults to default branch); `gh:author/reyn-design-warm@v1.2.0` for a tag |
| `git:` | `git:https://gitlab.com/u/r.git` | Generic git clone over HTTPS |
| `https:` | `https://example.com/dl/warm.zip` | Direct zip download |
| `npm:` | `npm:reyn-design-warm` | npm registry (the package's `dist` is unpacked) |
| `file:` | `file:./my-warm/` | Local copy (useful for local dev) |

The `gh:` scheme is the recommended path for community designs — free,
version-controllable, and discoverable via the GitHub topic
`reyn-design`.

---

## Manifest: `design.yaml`

Every distributable design package includes a `design.yaml` at its
root. This declares identity, OpenUI schema target, version, and
metadata.

```yaml
# design.yaml at the root of the design package
slug: warm
title:
  en: "Warm"
  ja: "ウォーム"
description:
  en: "A warm, coral-leaning palette inspired by sunset light."
  ja: "夕焼けの光に着想した、コーラル寄りの暖色系。"

# OpenUI Layer 1 schema this design targets.
# Values use SemVer-compatible identifiers; see docs/deep-dives/spec/openui/spec/manifest.md
# § 4 for shorthand vs. pinned forms.
schema: "reyn-ui/v1"     # any 1.x.y compatible

# Design's own version (independent of schema version).
version: "1.2.0"

# Which faces this design ships. Both, or just one (host falls back
# to the default design's missing face).
faces:
  - app
  - studio

# Author / licensing
author:
  name: "Designer Name"
  github: "designer-handle"
license: "MIT"
homepage: "https://github.com/designer-handle/reyn-design-warm"

# Picker / detail sheet
screenshots:
  - "screenshots/today.png"
  - "screenshots/conversation.png"
  - "screenshots/agent-card.png"

# Optional: tags for future search / discovery
tags: ["warm", "coral", "light"]
```

The package's directory layout, alongside `design.yaml`:

```
reyn-design-warm/
├── design.yaml             ← manifest (above)
├── README.md               ← author-supplied; rendered in picker's detail sheet
├── LICENSE
├── screenshots/            ← referenced from design.yaml
│   ├── today.png
│   └── …
├── Reyn.html               ← entry point (when extracted, host loads this in iframe)
├── app-screens.jsx         ← App face screens (window.AppScreens.*)
├── studio-screens.jsx      ← Studio face screens (window.StudioScreens.*)
├── data.js                 ← mock data (used in standalone preview)
├── data.types.js           ← shape documentation (matches reyn-ui/v1 ReynUiData)
├── styles.css              ← visual styling
└── icons.jsx               ← icon components
```

The exact filenames are conventions inherited from Claude Design
exports; what matters for OpenUI conformance is that the design
exposes the components and globals declared by `reyn-ui/v1`.

### Manifest validation rules

- `slug` matches `^[a-z][a-z0-9-]*$` and is unique within the target
  install directory.
- `schema` matches one of the schemas the host supports
  (`/api/web/config` reports `schemas_supported`).
- `version` is SemVer.
- At least one face listed in `faces` is materially present (the
  design exports the components that face requires).

A package failing any of these rules cannot be installed cleanly — the
user gets a clear diagnostic.

---

## CLI surface

```bash
# Install / update / remove
reyn design add <source>            # fetch, validate, install to reyn/local/designs/
reyn design update <slug>           # re-fetch from the original source
reyn design rm <slug>               # remove from reyn/local/designs/

# Local list & inspect
reyn design list                    # show installed designs across all three roots
reyn design show <slug>             # render the manifest + screenshots in the terminal
reyn design lint <slug | path>      # run conformance check without installing

# Publishing your own
reyn design init <slug>             # scaffold design.yaml + README + LICENSE template
reyn design pack <slug>             # produce <slug>-<version>.zip ready for distribution
reyn design publish <slug>          # convenience: pack + push to a configured git remote (optional)
```

### Where `reyn design add` installs to

By default `reyn design add` installs to `reyn/local/designs/<slug>/`
(per-user, gitignored). To install at the project level (committed to
the team's repo):

```bash
reyn design add gh:author/reyn-design-warm --to project
```

This drops it into `reyn/project/designs/<slug>/` instead. The team
can then `git add` and commit.

`web/designs/` is reserved for Reyn's bundled reference designs and
is not a target for `reyn design add`.

---

## Validation pipeline (what `reyn design add` does)

```
1. Fetch from source URI
2. Verify the package contains design.yaml; parse it
3. Manifest checks:
   - slug pattern
   - schema is in host's schemas_supported (e.g. "reyn-ui/v1")
   - version is valid SemVer
   - faces is non-empty
4. OpenUI Layer 0 conformance:
   - Entry script (Reyn.html or equivalent) does NOT hardcode mock data
     into components; reads window.OPENUI_DATA at boot
   - Source files reference window.OPENUI_HOST.invoke / .listen (not a
     Reyn-specific alternative naming)
   - No fetch / WebSocket / global state libraries imported by components
5. reyn-ui/v1 schema conformance:
   - For each face declared in manifest, the components required by the
     schema (see docs/deep-dives/spec/openui/schemas/reyn-ui-v1/components.md) are
     exported by the design (window.AppScreens.* / window.StudioScreens.*)
   - Component prop usage is consistent with schema declarations
6. If any check fails: print a diagnostic with file + line and abort.
   Nothing is written to disk.
7. On success: copy package contents to reyn/local/designs/<slug>/
8. Print: "Installed warm@1.2.0 from gh:author/reyn-design-warm.
   Available in the picker on next reyn web load."
```

The conformance check runs without a live `reyn web`; it can be
invoked standalone via `reyn design lint` for authors validating
locally before publish. A future `@openui/validator` library may
spin out the Layer 0 conformance subset for cross-engine reuse.

---

## Discovery (community designs)

For v1, no central registry. Designs live wherever their authors put
them. To find designs:

1. **GitHub topic** — authors tag their repo with `reyn-design`.
   Users search via the GitHub UI or:
   ```bash
   reyn design search "warm"
   # Lists matching public repos with the reyn-design topic.
   ```
2. **Awesome list** — a community-maintained `awesome-reyn-designs`
   repo linking notable designs. The Reyn project may seed this.
3. **Manual share** — any URL works; designs spread by word of mouth
   on social, blogs, etc.

A central registry can come later if the community grows. The same
manifest format and CLI commands keep working.

---

## Trust & safety

Designs ship JavaScript / JSX that runs in the user's browser. To make
this safe by construction, the OpenUI Layer 0 spec already forbids
the most dangerous patterns:

- `fetch` / `XMLHttpRequest` / `WebSocket` calls inside components
- Global state imports (Zustand, Redux, Recoil, …)
- Hardcoded secrets, tokens, or external URLs other than asset paths
- Non-standard build artefacts (no postinstall scripts, no bundler
  configs)

`reyn design lint` verifies these statically at install time. Designs
that violate the rules are refused — even if they pass surface-level
checks, an embedded network call or global state import fails the
conformance check.

For v1, this static linting is the only safety mechanism. The author
identity comes from the source URI (a GitHub repo handle is as
trustworthy as the user judges its author to be). Signed manifests
and a trust registry are explicit non-goals for the first iteration.

---

## Versioning & updates

Both the design and the schema it targets follow SemVer
independently:

- The **design** has its own `version` in `design.yaml`. Authors
  bump it when they iterate visuals or component implementation.
- The **schema** (e.g. `reyn-ui/v1`) is what the design targets.
  Schema bumps are governed by the Reyn project (see
  [engine-design-contract.md](engine-design-contract.md) for the
  evolution policy).

A user reinstalls a design with:

```bash
reyn design update warm                  # re-fetch latest from origin
reyn design add gh:author/reyn-design-warm@v2.0.0   # pin a specific tag
```

Reyn does not track design versions at runtime — installed files are
the source of truth. The version surfaces in:

- The picker's detail sheet ("warm · v1.2.0 · local")
- `reyn design list` output

When Reyn ships an incompatible major (e.g. `reyn-ui/v1` → `reyn-ui/v2`),
designs targeting the old major refuse to install on the new host
with a diagnostic linking to a migration note. Designs that have not
bumped will need the author to update them; users on the new Reyn can
stay on older bundled designs in the meantime.

---

## Publishing workflow (author-side)

```
1. claude.ai/design → generate your design (App, Studio, or both),
   following docs/deep-dives/spec/design/claude-design-prompt.md and the reyn-ui/v1 schema.
2. Export → zip.
3. mkdir my-design && unzip <export>.zip -d my-design/
4. cd my-design && reyn design init warm
   → scaffolds design.yaml (with schema: "reyn-ui/v1"), README,
     LICENSE template, screenshots/.
5. reyn design lint .
   → runs OpenUI Layer 0 + reyn-ui/v1 conformance checks locally.
     Fix anything that fails.
6. Take screenshots of the App and Studio faces against a real reyn
   web (or your dev environment); drop them into screenshots/.
7. reyn design pack
   → produces my-design-<version>.zip plus a CHANGELOG entry.
8. Push the directory to GitHub (or your preferred host). Tag with
   the topic `reyn-design` so it appears in `reyn design search`.
9. Share the URL — `gh:<your-handle>/<repo>` — wherever your audience
   lives.
```

This whole flow is intentionally low-friction: no accounts, no central
service, no submission queue. The only requirement is that the design
satisfies the OpenUI Layer 0 + reyn-ui/v1 conformance checks, which
are enforced statically.

---

## Why this maps onto Reyn's principles

- **Skill resolution mirror** (CLAUDE.md): `project / local / stdlib`
  for designs is the same pattern as for skills. End users already
  know the shape.
- **P5 (Workspace single source of truth)**: a design's files on disk
  in `reyn/local/designs/<slug>/` are the only state. No background
  sync, no hidden cache.
- **P6 (Events as audit truth)**: install / remove / update emit
  `design_installed` / `design_removed` / `design_updated` events
  to the Reyn event log, with the source URI and version. Reproducing
  a user's setup is "replay these install events".
- **P7 (OS skill-agnostic)**: the host adapter and CLI never embed
  design-specific knowledge. They read manifests, run conformance
  checks, and route data through OpenUI; no design name, screen
  name, or token name is hardcoded.
- **Predictability over autonomy**: contracts are static and
  explicit. Authors don't guess what the host will accept; they read
  the spec and the linter says yes or no.

---

## Out of scope (deferred)

- **Centralised registry** — too much infra for v1; GitHub + topic
  search works.
- **Signed manifests / author verification** — relies on Web of Trust
  / PKI; defer until there's a community.
- **Dependency between designs** (one design extending another) —
  adds complexity; not asked for by users yet.
- **Paid / proprietary designs** — Reyn's distribution model is
  OSS-first. A vendor can keep their design private by not
  publishing it.
- **Per-page design granularity** — a design covers a face wholesale.
  Mixing pages from two designs requires an explicit schema change.
- **Hot-reload of installed designs without browser refresh** —
  refresh is fine for v1; SSE-driven hot-reload can come later.

---

## Relationship to other docs

```
design_brief.md                ─→ what the UI should look and feel like
claude-design-prompt.md        ─→ how Claude Design is constrained to
                                  produce reyn-ui/v1 conformant exports
multi-design-selection.md      ─→ how Reyn picks among installed designs
                                  at runtime
design-distribution.md         ─→ this doc — how designs are installed,
                                  shared, and discovered
engine-design-contract.md      ─→ architecture (3-layer model + why
                                  OpenUI rather than AG-UI)
docs/deep-dives/spec/openui/                   ─→ canonical OpenUI Layer 0 protocol +
                                  reyn-ui/v1 Layer 1 schema spec
```

Together: an end-to-end pipeline from "I have a vision" to "another
user is using it" with no build steps in the middle for the user.
