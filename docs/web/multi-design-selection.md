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

```
web/
├── designs/                  ← all design variants live here
│   ├── warm/
│   │   ├── app/              ← warm App face (tokens.json + components/ + pages/)
│   │   └── studio/           ← warm Studio face
│   ├── dark/
│   │   ├── app/
│   │   └── studio/
│   └── claude/
│       ├── app/
│       └── studio/
│
├── shell/                    ← Reyn-owned, design-agnostic
│   ├── routes/               ← App/Studio toggle, design-selector route
│   ├── adapters/             ← maps Reyn data → component props
│   ├── contracts/            ← TS interfaces that EVERY design must satisfy
│   ├── api/                  ← REST + WS client to reyn.web gateway
│   └── design-loader.ts      ← dynamic import based on selection
│
└── README.md                 ← shell maintenance + add-a-design recipe
```

Each `web/designs/<name>/{app,studio}/` directory is a Claude Design
export, dropped in as-is. The shell never modifies these directories.

---

## Adding a design

```bash
DESIGN=<short-slug>            # e.g. "warm", "dark", "lobster"

mkdir -p web/designs/${DESIGN}/app
mkdir -p web/designs/${DESIGN}/studio

# Drop the App face export
unzip <app_export>.zip -d web/designs/${DESIGN}/app

# Drop the Studio face export
unzip <studio_export>.zip -d web/designs/${DESIGN}/studio

# Verify the new design satisfies the contracts
cd web && npm run typecheck
```

If `typecheck` passes, the design is selectable. No registration step:
the shell discovers designs by listing `web/designs/*/`.

Removing a design: `rm -rf web/designs/<name>`. If a user had selected
that design, the shell falls back to the default at next load.

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

The CLI passes the resolved default to the frontend via a single endpoint
`GET /api/web/config` returning:

```json
{
  "default_design": "warm",
  "available_designs": ["warm", "dark", "claude"]
}
```

The shell fetches this once on load, then resolves selection per the
priority above.

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
