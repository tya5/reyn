# Multi-design Selection

> **Navigation**: this is the operational document for runtime design
> selection. The architecture (3-layer model + AG-UI evaluation) is in
> [engine-design-contract.md](engine-design-contract.md). The protocol
> spec is in [docs/openui/](../openui/). The prompt template for
> generating designs is in [claude-design-prompt.md](claude-design-prompt.md).
> The publish / install pipeline is in
> [design-distribution.md](design-distribution.md).

Reyn's web UI supports **multiple OpenUI-conformant designs**
co-existing on disk, with a runtime picker letting users choose one.
This doc describes the directory layout, selection priority, and host /
design responsibilities for switching at runtime.

The integration goal is fixed by the OpenUI Layer 0 protocol: every
design exposes the same `window.OPENUI_*` contract, every host populates
those globals the same way. Switching designs is therefore a question of
"which directory does the host load this session" — no code changes.

---

## Directory layout

Designs live in three roots, resolved in the same order as Reyn's
skills (project → local → bundled):

```
reyn/project/designs/         ← project-checked-in designs (committed)
│   └── <slug>/               ← organisation brand, shared via the repo
│
reyn/local/designs/           ← user-specific designs (gitignored)
│   └── <slug>/               ← personal palette, drafts, downloaded community designs
│
web/designs/                  ← bundled with the Reyn repo (stdlib-equivalent)
└── <slug>/                   ← reference / canonical designs always available
```

Each `<root>/<slug>/` directory is a Claude Design export dropped in as
a unit (typically a `.zip` extracted in place). Examples of contents:
`Reyn.html`, `app-screens.jsx`, `studio-screens.jsx`, `data.js`,
`styles.css`, `icons.jsx`. Hosts never modify these directories.

### Why a single directory per design (not `<slug>/{app,studio}/`)

Claude Design exports both faces in a unified bundle (App and Studio
screens live in the same artefact set, sharing tokens, sharing icons,
sharing the boot scaffold). Splitting them at the filesystem level
would require synthetic separation that nobody asks for. The face
distinction is enforced by the schema's component contracts (a
component declares `surface: app` or `surface: studio` in its manifest
entry — see [components.md](../openui/schemas/reyn-ui-v1/components.md)),
not by directory layout.

A design MAY ship only one face by simply not exporting components
declared `surface: studio` (or vice versa). The host falls back to the
default design's missing face when needed.

### Resolution priority

When the host lists "available designs", it merges the three roots and
deduplicates by `<slug>`. If two roots define the same slug, the
higher-priority root wins:

```
reyn/project/designs/  >  reyn/local/designs/  >  web/designs/
```

This mirrors Reyn's skill resolution (project → local → stdlib). An
organisation can override a bundled design by checking in their own
version under the same slug in `reyn/project/designs/`; an individual
user can override that locally under `reyn/local/designs/`.

### Why three roots

- **`reyn/project/designs/`**: organisation brand design, committed to
  the repo. Subject to PR review.
- **`reyn/local/designs/`**: gitignored, per-user. Personal palettes,
  community designs downloaded via `reyn design add`, or working
  drafts before contributing upstream.
- **`web/designs/`**: bundled with Reyn (stdlib equivalent). Always
  available; never user-modified.

---

## Adding a design

Pick the root based on who the design is for:

```bash
# Bundled with the repo (rare — only Reyn's canonical reference designs)
ROOT=web/designs

# Project-level, committed to the consuming repo
ROOT=reyn/project/designs

# Personal / experimental, not committed
ROOT=reyn/local/designs

DESIGN=<slug>          # e.g. "warm", "lobster", "v1"

mkdir -p "${ROOT}/${DESIGN}"
unzip <export>.zip -d "${ROOT}/${DESIGN}"
```

Verify the design conforms to **OpenUI Layer 0 + reyn-ui/v1 Layer 1**
(component contracts, action / channel use, data shape). The minimum
checks are:

1. The design references `window.OPENUI_*` globals (not a Reyn-specific
   alternative naming).
2. The design's expected schema is `reyn-ui/v1` (declared in
   `design.yaml` if present, see
   [design-distribution.md](design-distribution.md)).
3. Required components for at least one face are exported.

A future `reyn design lint` command will check these statically. For
now, validation is manual.

Removing a design: `rm -rf <root>/<slug>`. If the user had selected it,
the host falls back to the default at next load.

---

## Selection mechanisms (priority order)

The host resolves which design to render in this order, first match
wins:

1. **URL query param** — `?design=<slug>` overrides everything else for
   this request. Useful for sharing a specific look in a screenshot or
   review thread. Persists to localStorage on success so a refresh
   keeps the same design.
2. **localStorage** — `localStorage.openui_design = "<slug>"`. Set by
   URL-param resolution above, by the in-app design picker, or
   manually for testing.
3. **Server default** — passed by the `reyn web` CLI as a runtime
   config value. Served when no client-side preference exists.
4. **First available alphabetically** — fallback when no default is
   configured and no preference exists. Listed by directory scan.

The user can switch designs anytime via:

- The design picker in the host's top-right menu (next to the
  App ↔ Studio toggle). Lists all installed designs with source
  badges (`project` / `local` / `stdlib`).
- A `?design=<slug>` URL query parameter.
- Editing localStorage manually (e.g. for testing).

### Schema match check

Before mounting a chosen design, the host verifies schema
compatibility:

1. The design declares its target schema (typically `reyn-ui/v1` —
   either in `design.yaml` or by convention). See
   [design-distribution.md](design-distribution.md) for the manifest.
2. The host implements one or more schema versions (e.g. `reyn-ui/v1`).
3. If the major version doesn't match, the host refuses to mount and
   shows a clear "this design targets reyn-ui/v2 but your Reyn supports
   reyn-ui/v1" diagnostic. The picker may visually mark incompatible
   designs.

Designs without an explicit schema declaration are assumed to target
`reyn-ui/v1` (matching the host) — provisional for v1, may be
tightened in future.

---

## Server-side default

The default design is set at `reyn web` startup, in priority order:

1. CLI flag: `reyn web --default-design <slug>`
2. Environment variable: `REYN_WEB_DEFAULT_DESIGN=<slug>`
3. `reyn.yaml`:
   ```yaml
   web:
     default_design: <slug>
   ```
4. None — fall through to "first available alphabetically".

The host exposes the resolved default and the full available roster via
a single endpoint:

```
GET /api/web/config
```

```json
{
  "default_design": "warm",
  "schemas_supported": ["reyn-ui/v1"],
  "available_designs": [
    {"slug": "warm",     "source": "stdlib",  "schema": "reyn-ui/v1", "faces": ["app", "studio"]},
    {"slug": "dark",     "source": "stdlib",  "schema": "reyn-ui/v1", "faces": ["app", "studio"]},
    {"slug": "my-pink",  "source": "local",   "schema": "reyn-ui/v1", "faces": ["app"]},
    {"slug": "acme",     "source": "project", "schema": "reyn-ui/v1", "faces": ["app", "studio"]}
  ]
}
```

The picker fetches this on load and resolves selection per the priority
above. The `source` badge is displayed alongside each entry. The
`schema` field allows the picker to grey out designs incompatible with
the host's `schemas_supported`.

User-managed designs (`reyn/local/designs/`, `reyn/project/designs/`)
are served as static assets via the host on demand — drop in a new
design and refresh the browser to see it. Bundled designs
(`web/designs/`) are baked into the production build but served from
disk in dev.

---

## What this enables

- **Brand experimentation**: keep two or three brand directions side
  by side, A/B test, decide later.
- **Theme variants**: light / dark / high-contrast as separate designs
  (a single design can also offer variants via designer-mode chrome,
  but distinct designs scale better).
- **Per-environment defaults**: dev defaults to a "playful" design,
  prod defaults to "warm".
- **Reviewer mode**: a design system review session uses
  `?design=<slug>` URLs to walk through candidates.
- **End-user personalisation**: an end user drops their own Claude
  Design export into `reyn/local/designs/<slug>/` and selects it via
  the picker. No PR, no rebuild, no permission.
- **Organisation override**: a deploying org checks
  `reyn/project/designs/<slug>/` into their fork and sets it as the
  default in `reyn.yaml`. Bundled designs remain as fallback.
- **Community design ecosystem**: an author publishes their design as
  open source. Other users install via `reyn design add gh:<author>/<repo>`,
  the package lands in `reyn/local/designs/<slug>/`, picker shows it
  next refresh. See [design-distribution.md](design-distribution.md).

---

## Out of scope (deferred)

- **Per-user design preferences saved server-side** — for v1,
  localStorage is enough. Add when multi-user auth lands.
- **Hot reload of new designs without browser refresh** — for v1,
  refresh after dropping a new export is fine. SSE-driven hot reload
  may come later.
- **Design-specific routes** (one design has an extra page another
  doesn't) — not allowed. Designs differ in chrome and tokens, not in
  features. If a feature needs a per-design branch, add it to the
  `reyn-ui/v1` schema and require all designs to implement it (or mark
  the component `required: false` with a host-side fallback).
