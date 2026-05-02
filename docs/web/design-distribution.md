# Design distribution

> **Vision**: anyone with `claude.ai/design` access can author a Reyn
> design, publish it as open source, and other users `reyn design add
> <source>` it to switch their UI without rebuilding anything.

This document describes the distribution layer that sits on top of
[multi-design selection](multi-design-selection.md). The selection layer
answers "which design am I rendering right now?"; this layer answers
"where do designs come from, how do they get installed, and how do
authors publish their own?"

---

## End-to-end story

```
                       ┌─────────────────────────────────────────────┐
                       │            Designer's machine                │
                       │                                             │
  prompts Claude       │  1. Open claude.ai/design                   │
  Design with the      │  2. Paste docs/web/claude-design-prompt.md  │
  Reyn template        │  3. Iterate on canvas                       │
                       │  4. Export → zip                            │
                       │  5. reyn design pack <slug>                 │
                       │     → produces a publishable archive +      │
                       │       design.yaml manifest                  │
                       │  6. Push to GitHub (or any URL host)        │
                       └────────────────────┬────────────────────────┘
                                            │
                                            ▼  publish
                       ┌─────────────────────────────────────────────┐
                       │     github.com/<author>/reyn-<slug>         │
                       │     (a small repo of design files +         │
                       │      manifest, no code beyond components)   │
                       └────────────────────┬────────────────────────┘
                                            │
                                            ▼  share link
                       ┌─────────────────────────────────────────────┐
                       │            End user's machine                │
                       │                                             │
                       │  reyn design add gh:<author>/reyn-<slug>    │
                       │  → fetches, validates contracts,            │
                       │    drops into reyn/local/designs/<slug>/    │
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

`reyn design add <source>` accepts several forms; the resolver picks the
right backend by prefix:

| Prefix | Example | Backend |
|---|---|---|
| `gh:` | `gh:author/reyn-warm-coral` | GitHub HTTPS clone (defaults to default branch); `gh:author/reyn-warm-coral@v1.2.0` for a tag |
| `git:` | `git:https://gitlab.com/u/r.git` | Generic git clone over HTTPS |
| `https:` | `https://example.com/dl/coral.zip` | Direct zip download |
| `npm:` | `npm:reyn-design-coral` | npm registry (the package's `dist` is unpacked) |
| `file:` | `file:./my-coral/` | Local copy (useful for local dev) |

The `gh:` scheme is the recommended path for community designs — it's
free, version-controllable, and discoverable via GitHub topics.

---

## Manifest: `design.yaml`

Every distributable design package must include a `design.yaml` at its
root. This is what tells Reyn the design's identity, version, faces, and
compatibility.

```yaml
# design.yaml at the root of the design package
name: warm-coral
version: 1.2.0
faces:
  - app
  - studio
title:
  en: "Warm Coral"
  ja: "ウォーム・コーラル"
description:
  en: "A warm, coral-leaning palette inspired by sunset light."
  ja: "夕焼けの光に着想した、コーラル寄りの暖色系。"
author:
  name: "Designer Name"
  github: "designer-handle"
license: "MIT"
homepage: "https://github.com/designer-handle/reyn-warm-coral"
screenshots:
  - "screenshots/today.png"
  - "screenshots/conversation.png"
  - "screenshots/agent-card.png"
# Compatibility — refuse to install if Reyn's contract version is incompatible
contract_version: "1.0"
# Optional tags for future search / discovery
tags: ["warm", "coral", "light"]
```

The package's directory layout, alongside `design.yaml`:

```
reyn-warm-coral/
├── design.yaml
├── README.md             ← author-supplied; rendered in the picker's detail sheet
├── LICENSE
├── screenshots/          ← referenced from design.yaml
│   ├── today.png
│   └── …
├── app/                  ← per the Claude Design prompt template
│   ├── tokens.json
│   ├── components/
│   └── pages/
└── studio/               ← optional; omit if app-only
    ├── tokens.json
    ├── components/
    └── pages/
```

Manifest validation rules:
- `name` must match `^[a-z][a-z0-9-]*$` and be unique within `reyn/local/designs/`
- `version` must be SemVer
- `contract_version` must be in the set Reyn's installed runtime supports
- At least one of `faces` must exist on disk
- Each listed face must satisfy the contracts in `claude-design-prompt.md`

A package failing any of these rules cannot be installed — the user gets
a clear diagnostic.

---

## CLI surface

```bash
# Install / update / remove
reyn design add <source>            # fetch, validate, install to reyn/local/designs/
reyn design update <name>           # re-fetch from the original source
reyn design rm <name>               # remove from reyn/local/designs/

# Local list & inspect
reyn design list                    # show installed designs across all three roots
reyn design show <name>             # render the manifest + screenshots in the terminal
reyn design lint <name | path>      # run contract validation without installing

# Publishing your own
reyn design init <name>             # scaffold a design.yaml + README in cwd or under reyn/local/designs/
reyn design pack <name>             # produce <name>-<version>.zip ready for distribution
reyn design publish <name>          # convenience — pack + push to a configured git remote (optional)
```

### Resolution order for "where does an installed design land?"

`reyn design add` always installs to `reyn/local/designs/<name>/` by
default. To install at the project level (committed to the team's repo):

```bash
reyn design add gh:author/reyn-warm-coral --to project
```

This drops it into `reyn/project/designs/<name>/` instead. The team can
then commit the directory.

---

## Validation pipeline (what `reyn design add` does)

```
1. Fetch from source URI
2. Verify the package contains design.yaml
3. Parse manifest → check schema, contract_version compatibility
4. For each face listed in manifest:
     - Verify tokens.json validates against the schema
     - Run a static check that components export the contract prop shapes
       (uses the same TypeScript contracts the shell relies on)
5. If any check fails: print a diagnostic with file + line and abort.
   Nothing is written to reyn/local/designs/
6. On success: copy package contents to reyn/local/designs/<name>/
7. Print: "Installed warm-coral@1.2.0 from gh:author/reyn-warm-coral.
   Available in the picker on next reyn web load."
```

The static check runs without a live `reyn web` — it parses the package's
`*.tsx` files, extracts type information, and checks against the
contracts shipped with Reyn. This means a malformed design fails install
even on a machine that hasn't run the web shell yet.

---

## Discovery (community designs)

For v1, we don't host a central registry. Designs live wherever their
authors put them. To find designs:

1. **GitHub topic**: authors tag their repo with `reyn-design`. Users
   search via the GitHub UI or:
   ```bash
   reyn design search "warm"
   # Lists matching public repos with the reyn-design topic.
   # Backed by GitHub's search API.
   ```
2. **Awesome list**: a community-maintained `awesome-reyn-designs` repo
   linking notable designs. The Reyn org may seed this.
3. **Manual share**: any URL works; designs spread by word of mouth on
   social, blogs, etc.

A central registry can come later if the community grows. The same
manifest format and CLI commands keep working.

---

## Trust & safety

Designs ship `*.tsx` components that run in the user's browser. To make
this safe by construction, the contract template (`claude-design-prompt.md`)
already forbids:

- `fetch` / `XMLHttpRequest` / WebSocket calls inside components
- Global state imports (Zustand, Redux, etc.)
- Hardcoded secrets, tokens, or external URLs other than asset paths
- Non-standard build artifacts (no postinstall scripts, no
  bundler configs)

`reyn design lint` runs these checks at install time. Components that
violate the rules are refused — even if the static type contracts pass,
a network call will fail the install.

For v1, this static linting is the only safety mechanism. The author
identity comes from the source URI (a GitHub repo handle is as
trustworthy as the user judges its author to be). Signed manifests and a
trust registry are explicit non-goals for the first iteration.

---

## Versioning & updates

Designs are SemVer. The author bumps `design.yaml.version`; users
reinstall with:

```bash
reyn design update warm-coral             # re-fetch latest from origin
reyn design add gh:author/reyn-warm-coral@v2.0.0   # pin a specific tag
```

Reyn's frontend doesn't track design versions at runtime — installed
files are the source of truth. The version surfaces only in:

- The picker's detail sheet ("warm-coral · v1.2.0 · local")
- `reyn design list` output
- The contract-version compatibility check at install

If a new Reyn release bumps `contract_version` in a breaking way,
designs published against the old version refuse to install (with a
diagnostic linking to a migration note). Designs that haven't bumped
will need the author to update them; users on the new Reyn can stay on
older bundled designs in the meantime.

---

## Publishing workflow (author-side)

```
1. claude.ai/design → generate your design (App, Studio, or both)
   following docs/web/claude-design-prompt.md.
2. Export → zip.
3. mkdir my-design && unzip <export>.zip -d my-design/app
   (and / or my-design/studio).
4. cd my-design && reyn design init <name>
   → scaffolds design.yaml, README.md, LICENSE template, screenshots/.
5. reyn design lint .
   → runs all the install-time checks locally. Fix anything that
     fails.
6. Take screenshots of the App and Studio faces against a real
   reyn web (or your dev environment), drop them into screenshots/.
7. reyn design pack
   → produces my-design-<version>.zip plus a CHANGELOG entry.
8. Push the directory to GitHub (or your preferred host). Tag with
   the topic `reyn-design` so it appears in `reyn design search`.
9. Share the URL — gh:<your-handle>/<repo> — wherever your audience
   lives.
```

This whole flow is intentionally low-friction: no accounts, no central
service, no submission queue. The only requirement is that the design
satisfies the prompt template's contracts, which is enforced statically.

---

## Why this maps onto Reyn's principles

- **Skill resolution mirror** (CLAUDE.md): `project / local / stdlib` for
  designs is the same pattern as for skills. End users already know the
  shape.
- **P5 (Workspace single source of truth)**: a design's files on disk in
  `reyn/local/designs/<name>/` are the only state. No background sync, no
  hidden cache.
- **P6 (Events as audit truth)**: install / remove / update emit
  `design_installed` / `design_removed` / `design_updated` events to the
  Reyn event log, with the source URI and version. Reproducing a user's
  setup is "replay these install events".
- **P7 (OS skill-agnostic)**: the gateway and CLI never embed
  design-specific knowledge. They read manifests and validate contracts;
  no design name or token name is hardcoded.
- **Predictability over autonomy**: contracts are static and explicit.
  Authors don't guess what the OS will accept; they read the template
  and the linter says yes or no.

---

## Out of scope (deferred)

- **Centralised registry** — too much infra for v1; GitHub + topic search
  works.
- **Signed manifests / author verification** — relies on Web of Trust /
  PKI; defer until there's a community.
- **Dependency between designs** (one design extending another) — adds
  complexity; not asked for by users yet.
- **Paid / proprietary designs** — Reyn's distribution model is OSS-first.
  A vendor can keep their design private by not publishing it; that's
  enough.
- **Per-page design granularity** — a design covers a face wholesale.
  Mixing pages from two designs requires an explicit Reyn shell change.
- **Hot-reload of installed designs without browser refresh** — refresh
  is fine for v1; SSE-driven hot-reload can come later.

---

## Relationship to other docs

```
design_brief.md             ─→ what the UI should look and feel like
claude-design-prompt.md     ─→ how Claude Design is constrained to produce
                               contract-compliant exports
multi-design-selection.md   ─→ how Reyn picks among installed designs
design-distribution.md      ─→ this doc — how designs are installed,
                               shared, and discovered
```

Together: an end-to-end pipeline from "I have a vision" to "another user
is using it" with no build steps in the middle for the user.
