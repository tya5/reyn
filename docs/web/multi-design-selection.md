# Multi-design selection

Reyn's web UI supports multiple Claude Design exports living side-by-side,
selected at startup or runtime. This doc describes the directory layout,
the selection mechanisms, and how the parts fit together.

The integration goal is set by the Claude Design prompt template
(`claude-design-prompt.md`): every export satisfies the same component
contracts and `tokens.json` schema. Therefore, switching designs is purely
a matter of pointing the shell at a different directory — no code changes.

---

## Directory layout

Designs live in three locations, resolved in the same order as Reyn's
skills (project → local → bundled):

```
reyn/project/designs/         ← project-checked-in designs (committed)
│   └── <name>/{app,studio}/  ← e.g. an organisation's brand design
│
reyn/local/designs/           ← user-specific designs (gitignored)
│   └── <name>/{app,studio}/  ← e.g. an end-user's personal palette
│
web/designs/                  ← bundled with the Reyn repo (stdlib-equivalent)
├── warm/
│   ├── app/                  ← warm App face (tokens.json + components/ + pages/)
│   └── studio/               ← warm Studio face
├── dark/
│   ├── app/
│   └── studio/
└── claude/
    ├── app/
    └── studio/

web/shell/                    ← Reyn-owned, design-agnostic
├── routes/                   ← App/Studio toggle, design-selector route
├── adapters/                 ← maps Reyn data → component props
├── contracts/                ← TS interfaces that EVERY design must satisfy
├── api/                      ← REST + WS client to reyn.web gateway
└── design-loader.ts          ← runtime discovery + selection
```

Each `<root>/<name>/{app,studio}/` directory is a Claude Design export,
dropped in as-is. The shell never modifies these directories.

### Resolution

When the shell lists "available designs", it merges the three roots and
deduplicates by name. If two roots define the same name (e.g. both
`reyn/local/designs/warm/` and `web/designs/warm/`), the higher-priority
root wins:

```
reyn/project/designs/  >  reyn/local/designs/  >  web/designs/
```

This mirrors how Reyn resolves skills (project → local → stdlib). It
means an organisation can override a bundled design by checking in their
own version under the same name in `reyn/project/designs/`, and an
individual user can override that locally under `reyn/local/designs/`.

### Why three roots

- **`reyn/project/designs/`**: checked into the repo of the project that
  uses Reyn — typically an organisation's brand design, shared across the
  team. Subject to PR review.
- **`reyn/local/designs/`**: gitignored, per-user. Lets an end user drop
  in their own Claude Design export without committing it. Useful for
  personal experimentation, palette tweaks, or keeping a working draft
  before contributing it upstream.
- **`web/designs/`**: bundled with Reyn itself. The "stdlib" of designs.
  Always available, never user-modified.

---

## Adding a design

Pick the right root based on who the design is for:

```bash
# Bundled with the repo (rare — only Reyn's own canonical designs)
ROOT=web/designs

# Project-level design committed to the consuming project
ROOT=reyn/project/designs

# Personal / experimental, not committed
ROOT=reyn/local/designs

DESIGN=<short-slug>            # e.g. "warm", "dark", "lobster"

mkdir -p "${ROOT}/${DESIGN}/app"
mkdir -p "${ROOT}/${DESIGN}/studio"

# Drop the App face export
unzip <app_export>.zip -d "${ROOT}/${DESIGN}/app"

# Drop the Studio face export (or skip if you only have one face)
unzip <studio_export>.zip -d "${ROOT}/${DESIGN}/studio"

# Verify the new design satisfies the contracts
cd web && npm run typecheck
```

If `typecheck` passes, the design is selectable. No registration step:
the shell discovers designs at runtime by reading the three roots via the
gateway's `GET /api/web/config` (see § Server-side default).

Removing a design: `rm -rf <root>/<name>`. If a user had selected that
design, the shell falls back to the default at next load.

### A face is optional

A design may ship only `app/` or only `studio/` if you only want to theme
one face. The shell falls back to the default design's other face when a
selected design is missing it. (Useful for personal designs that only
care about the App side.)

---

## Selection mechanisms (priority order)

The shell resolves which design to render in this order, first match wins:

1. **URL query param** — `?design=warm` overrides everything else for this
   request. Useful for sharing a specific look in a screenshot or design
   review thread. Persists to localStorage on success so a refresh keeps
   the same design.
2. **localStorage** — `localStorage.reyn_design = "warm"`. Set by URL
   param resolution above, by the in-app design picker, or manually for
   testing.
3. **Default from server** — passed by the `reyn web` CLI as a meta tag
   or runtime config. Served when no client-side preference exists.
4. **First design alphabetically** — fallback when no default is configured
   and no preference exists. Listed by `fs.readdirSync("web/designs")`.

The user can switch designs anytime via:

- The design picker in the shell's top-right menu (App ↔ Studio toggle
  area). Lists all `web/designs/*` and shows the active one.
- `?design=<name>` URL param.
- Editing localStorage manually.

---

## Server-side default

The default design is set at `reyn web` startup, in priority order:

1. CLI flag: `reyn web --default-design warm`
2. Environment variable: `REYN_WEB_DEFAULT_DESIGN=warm`
3. `reyn.yaml`:
   ```yaml
   web:
     default_design: warm
   ```
4. None (fall through to "first alphabetically").

The gateway exposes `GET /api/web/config` which returns the merged design
roster across all three roots:

```json
{
  "default_design": "warm",
  "available_designs": [
    {"name": "warm",     "source": "stdlib", "faces": ["app", "studio"]},
    {"name": "dark",     "source": "stdlib", "faces": ["app", "studio"]},
    {"name": "claude",   "source": "stdlib", "faces": ["app", "studio"]},
    {"name": "my-pink",  "source": "local",  "faces": ["app"]},
    {"name": "acme",     "source": "project","faces": ["app", "studio"]}
  ]
}
```

The shell fetches this once on load, then resolves selection per the
priority in § Selection mechanisms. The `source` field surfaces in the
design picker UI so users know whether they're looking at a personal
design or a project-committed one (small badge: `local` / `project` /
`stdlib`).

User designs (`reyn/local/designs/`) are served as static assets via the
gateway: assets are read from disk on each request (no build step), so a
user can drop in a new design and refresh the browser to see it.
Bundled designs (`web/designs/`) are baked into the frontend build for
production and served from disk in dev. Project designs
(`reyn/project/designs/`) work the same way as user designs.

---

## Contract enforcement

Every design under `web/designs/*/` must satisfy:

- `tokens.json` validates against the schema in
  `claude-design-prompt.md` § Token schema.
- Each face exports the components listed in
  `claude-design-prompt.md` § Component contracts, with the prop shapes
  in `web/shell/contracts/`.

The shell's `design-loader.ts` does dynamic imports against these
contracts, so a violation triggers a TypeScript error at typecheck time.
This is the only thing that has to be checked when adding or replacing
a design.

---

## What this enables

- **Brand experimentation**: keep two or three brand directions side by
  side, A/B them, decide later.
- **Theme variants**: light/dark/high-contrast as separate designs (no
  in-design dark-mode logic needed; tokens.json swap handles it).
- **Per-environment defaults**: dev environment defaults to a "playful"
  design, prod defaults to "warm".
- **Reviewer mode**: a design system review session uses
  `?design=variant-name` URLs to walk through candidates.
- **End-user personalisation**: an end user drops their own Claude Design
  export into `reyn/local/designs/my-design/` and selects it via the
  picker. No PR, no rebuild, no permission required.
- **Organisation override**: an organisation deploying Reyn checks
  `reyn/project/designs/acme/` into their fork, set as the default in
  `reyn.yaml`. Bundled designs remain available as fallback.
- **Community design ecosystem**: an author publishes their design as
  open source (e.g. on GitHub with the `reyn-design` topic). Other users
  install via `reyn design add gh:<author>/<repo>`, the package lands in
  `reyn/local/designs/<name>/`, picker shows it next refresh. See
  [design-distribution.md](design-distribution.md) for the install /
  publish pipeline.

---

## Out of scope (deferred)

- **Per-user design preferences saved server-side** — for v1, localStorage
  is enough. Add when multi-user auth lands.
- **Hot reload of new designs without server restart** — for v1, dropping
  in a new design requires the shell's dev server to reload. Production
  builds bundle whichever designs were present at build time.
- **Design-specific routes** (e.g. one design has an extra page the other
  doesn't) — not allowed. Every design must implement every contract
  page; designs differ only in chrome and tokens, not in features. If a
  feature genuinely needs a per-design branch, add it to the contract
  and require all designs to implement it.

---

## Relationship to the prompt template

`claude-design-prompt.md` ensures every Claude Design export is
contract-compliant, so multi-design selection works without per-design
adapter code. The two docs together form the integration contract:

- The prompt template constrains **what comes out of Claude Design**.
- This doc constrains **what the Reyn shell does with multiple exports**.

If a design is generated without the template, it likely won't satisfy
the contracts and won't be selectable. Always go through the template.
