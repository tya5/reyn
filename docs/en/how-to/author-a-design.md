---
type: how-to
topic: web
audience: [human]
applies_to: [reyn/local/designs/, claude.ai/design]
---

# Author your own Reyn web UI

**Goal:** swap Reyn's web UI for your own visual style without touching
Reyn's engine. The fastest path is `claude.ai/design`; this page gives
you a paste-ready prompt with a copy button.

## How it works in one diagram

```
[your design]  ⇄  [OpenUI Layer 0 protocol]  ⇄  [Reyn engine]
   ↑                  (window.OPENUI_HOST)         ↑
   you author              (locked)            never touch
```

Reyn's web shell wires the engine in at runtime. Your design only
needs to follow the OpenUI protocol — no Reyn-specific glue code.

## 1. Copy the prompt

Click the **copy** icon at the top-right of the block, then paste it
into a fresh `claude.ai/design` thread.

````markdown
# Reyn Design Prompt

You are designing a UI for **Reyn**, a workflow-engine-driven agent OS.
The design will integrate with Reyn via the **OpenUI Layer 0 protocol**
and the **reyn-ui/v1 Layer 1 schema**.

<!-- =================================================================
  🔓 EDITABLE — fill in your design brief below.
  Edit freely. This is your design intent.
================================================================= -->

## Your design brief

**Brand voice** (1–2 sentences):
> [REPLACE: e.g. "Warm and approachable but technically credible.
> Inspired by Linear's clarity and Stripe's precision."]

**Primary color**: [REPLACE: coral / amber / teal / monochrome / your-pick]
**Mode**: [REPLACE: light / dark / both]
**Density**: [REPLACE: cozy / comfortable / dense]

**Typography**:
- Body: [REPLACE: e.g. Inter, Geist, Söhne]
- Display (optional): [REPLACE: e.g. Instrument Serif for App headers only]
- Mono (Studio only): [REPLACE: e.g. JetBrains Mono, IBM Plex Mono]

**Screens to prioritise** (App side):
- [REPLACE: Today, Conversation, Agent gallery, Library card → guided run]

**Screens to prioritise** (Studio side):
- [REPLACE: Conversation+inspector, Skill graph, Run timeline, Permissions]

**Inspirations** (optional): [REPLACE]
**Avoid** (optional): [REPLACE]

<!-- =================================================================
  🔒 LOCKED — DO NOT EDIT BELOW THIS LINE.
  This is the OpenUI / reyn-ui/v1 protocol contract. Editing it
  will break the engine ↔ design integration.
================================================================= -->

## What is Reyn

Reyn lets non-technical users converse with specialist AI agents and
lets developers build & ship those agents from Markdown. The Reyn UI
has **two faces**:

- **App** — friendly end-user surface (default landing). Tone:
  Claude.ai / OpenClaw / ChatGPT. Hides engine vocabulary entirely.
- **Studio** — dense developer surface. Build & debug skills, inspect
  runs, edit permissions. Tone: Linear / Vercel / Cursor / Temporal /
  LangSmith. Surfaces engine vocabulary verbatim.

The two faces share agent identity (name, color, avatar) and a
top-right App ↔ Studio toggle, but **share nothing else** — different
chrome, density, vocabulary.

## How the design connects to the engine

Reyn implements the **OpenUI Layer 0 protocol**. The design reads four
globals on `window`:

- `window.OPENUI_HOST` — `{ invoke(action, payload?), listen(channel, handler) }`
- `window.OPENUI_DATA` — initial data (shape: `ReynUiData`)
- `window.OPENUI_SCHEMA` — `"reyn-ui/v1"`
- `window.OPENUI_DESIGN_MODE` — `true` standalone, `false` embedded

## Required components, data shape, actions, channels

Specified canonically in:

- Component contracts: `docs/openui/schemas/reyn-ui-v1/components.md`
- Data shape (`ReynUiData`): `docs/openui/schemas/reyn-ui-v1/data.types.ts`
- Actions / channels: `docs/openui/schemas/reyn-ui-v1/manifest.yaml`

Treat as the contract: every required component must be exported,
prop shapes must match, action / channel names must be used as defined.

## Hard rules

- **No hardcoded mock data inside components.** Read from
  `window.OPENUI_DATA` when embedded, fallback mock when standalone.
  Mock lives in a separate `data.js`.
- **All user actions go through `window.OPENUI_HOST.invoke()`.** Every
  user-driven side-effect (sending a message, answering an
  intervention, attaching to an agent, switching face, accepting a
  permission, cancelling a run) MUST `await
  window.OPENUI_HOST.invoke(<action>, <payload>)`. Local-only state
  updates stay in component state.
- **All async data goes through `window.OPENUI_HOST.listen()`** with
  unsubscribe on unmount.
- **No `fetch` / `XMLHttpRequest` / `WebSocket` calls in components.**
- **No global state libraries** (Zustand, Redux, …). Local state only.
- **No bundler / framework configs.** The host owns the build.
- **App face vocabulary**: never expose `phase`, `artifact`,
  `control_ir`, `event`, `validation`, `schema`. Studio uses these
  verbatim.
- **i18n**: App face strings come from `OPENUI_DATA.COPY[lang]`.
- **Two HTML entries (REQUIRED)**:
  - `Reyn.html` — host-mountable runtime, hash-routed App/Studio
    mount, no artboards. This is what `reyn web` loads.
  - `Reyn UI.html` — design canvas with artboards (designer mode only).
- **`Reyn.html` MUST trigger Babel transformation explicitly** if it
  uses babel-standalone:

  ```html
  <script>
    (function () {
      var t = setInterval(function () {
        if (window.Babel && Babel.transformScriptTags) {
          clearInterval(t);
          Babel.transformScriptTags();
        }
      }, 50);
    })();
  </script>
  ```

  Auto-runner fires on DCL, which has already passed when the host
  shell injects the design.

## Now generate

Append one of these on the next line and send:

> `→ App + Studio` (recommended — both faces in one export)
>
> `→ App` (App face only)
>
> `→ Studio` (Studio face only)

Begin by enumerating which screens you'll cover and your token /
typography proposal, then iterate.
````

## 2. Fill in the brief

Inside `claude.ai/design`, edit ONLY the section between the
`🔓 EDITABLE` markers. Replace each `[REPLACE: …]` placeholder with
your choice. Leave everything below `🔒 LOCKED` untouched.

## 3. Iterate visually

Tweak colors, layout, copy in plain English on the canvas chat. Don't
ask Claude Design to change the OpenUI globals or component names —
those are the locked contract that lets Reyn's engine wire in.

## 4. Export and drop in

Use **Export → `.zip`**, then:

```bash
DESIGN=warm-coral   # whatever slug you like
mkdir -p "reyn/local/designs/$DESIGN"
unzip ~/Downloads/Reyn-export.zip -d "reyn/local/designs/$DESIGN"
```

Refresh / restart `reyn web`. The design picker discovers it
automatically.

## Example briefs

### Default coral

```
Brand voice: Warm and approachable, like a knowledgeable friend.
             Inspired by Claude.ai App tone and Stripe's precision.
Primary color: coral
Mode: light
Density: comfortable
Body: Inter
Display: Instrument Serif (App headers only)
Mono: JetBrains Mono (Studio)
Inspirations: Claude.ai, Linear, OpenClaw
Avoid: generic SaaS purple, Tailwind-default look
```

### Dark monochrome (terminal-inspired)

```
Brand voice: Quiet competence. The interface gets out of the way.
             Inspired by Vercel and the bare elegance of well-tuned
             terminal apps.
Primary color: monochrome (zinc 50→950)
Mode: dark
Density: dense
Body: Inter
Mono: JetBrains Mono
Inspirations: Vercel dashboard, Linear's dark mode, Warp
Avoid: any color that isn't a neutral
```

### Nordic

```
Brand voice: Cool, precise, breathing. Inspired by Nordic minimal
             design and the Notion-but-quieter aesthetic.
Primary color: muted blue-gray (oklch 0.62 0.06 240)
Mode: both
Density: cozy
Body: Inter
Display: Söhne (App)
Mono: IBM Plex Mono
Inspirations: Notion, Things 3, Bear Notes
Avoid: anything saturated
```

## Troubleshooting

- **White screen** — host shell can't see the design's globals. Most
  often: the `Babel.transformScriptTags()` polling snippet is missing.
- **Design doesn't appear in the picker** — check
  `reyn/local/designs/<slug>/Reyn.html` exists. The shell fetches
  exactly this filename.
- **Engine doesn't respond to messages** — your `submit` handler isn't
  calling `window.OPENUI_HOST.invoke('agent.submit', { agentId, text })`.
  The PR31-era v1 export had this bug; check the design's
  `app-screens.jsx` and `studio-screens.jsx` for the wiring.
- **Styles don't apply** — the design's `<link href="styles.css">`
  tag must be inside the design's HTML, not in the shell. The shell
  rewrites the relative URL automatically.

## See also

- [`design-author-guide.md`](../../web/design-author-guide.md) — the
  full operational guide (richer than this how-to)
- [`multi-design-selection.md`](../../web/multi-design-selection.md) —
  three-root layout (`local` / `project` / bundled), selection priority
- [`docs/openui/`](../../openui/) — the OpenUI Layer 0 protocol spec
  (you don't need to read it to author a design; it's the contract
  the locked zone enforces)
