# Design Author Guide — swap your own UI into Reyn

> **Audience**: end users who want their own visual style for Reyn's web
> UI without touching Reyn's engine. If you want to *publish* a design
> for others to install, see [design-distribution.md](design-distribution.md)
> (Roadmap). The protocol contract underneath is in
> [docs/openui/](../openui/) — you don't need to read it to author a
> design.

Reyn's web UI is a swappable shell on top of a fixed engine contract.
You author the shell visually in `claude.ai/design`; Reyn wires the
engine in at runtime. This guide walks you through that loop.

---

## Workflow — from "I want to design" to "running picker shows it"

1. **Copy the prompt.** Open
   [claude-design-prompt.md](claude-design-prompt.md) (or the bundled
   paste-ready version
   [tmp/claude-design-prompt-v2.md](../../tmp/claude-design-prompt-v2.md))
   and copy the whole thing.
2. **Edit the brief at the top.** Only the section between the
   🔓 EDITABLE markers — the design brief. Fill in your brand voice,
   palette, density, screens, inspirations. Leave everything below the
   🔒 LOCKED marker alone.
3. **Paste into `claude.ai/design`.** Append `→ App + Studio` (or
   `→ App` / `→ Studio`) on the last line, then send.
4. **Iterate visually in the canvas.** Tweak colors, layout, copy in
   plain English on the canvas chat. Don't ask Claude Design to change
   the OpenUI globals or component names — those are the locked
   contract that lets Reyn's engine wire in.
5. **Export and drop in.** Use Export → `.zip`, extract into
   `reyn/local/designs/<your-slug>/`, then refresh / restart `reyn web`.
   The design picker discovers it automatically.

```bash
DESIGN=warm-coral   # whatever slug you like
mkdir -p "reyn/local/designs/$DESIGN"
unzip ~/Downloads/Reyn-export.zip -d "reyn/local/designs/$DESIGN"
```

See [multi-design-selection.md](multi-design-selection.md) for the
three-root layout (`local` vs `project` vs bundled) and selection
priority.

---

## What you edit, what you don't

The prompt has two zones, visually fenced.

### 🔓 Editable — your design brief

The section at the top, marked with `🔓 EDITABLE`. Fill in:

- **Brand voice** — 1–2 sentences setting the tone.
- **Palette** — primary color, light / dark / both, state colors.
- **Density** — cozy / comfortable / dense.
- **Typography** — body and mono font choices.
- **Inspirations / avoid** — sites or aesthetics you want to riff on
  or steer away from.

This is your design intent. Edit freely. Iterate as much as you want.
This zone exists to be customized.

### 🔒 Locked — the protocol contract

Everything below the `🔒 LOCKED` marker. This is the OpenUI Layer 0
protocol + reyn-ui/v1 schema:

- The `window.OPENUI_HOST` / `OPENUI_DATA` / `OPENUI_SCHEMA` /
  `OPENUI_DESIGN_MODE` globals.
- Required component names (`TodayScreen`, `Conversation`,
  `SkillGraphPage`, …) and their prop shapes.
- Action and channel names (`agent.submit`, `agent.message`, …).
- The `Reyn.html` two-entry split and the Babel `transformScriptTags`
  polling snippet.

**Don't touch any of it.** If you do, here's what breaks:

- Renaming a component → host can't find it → blank slot.
- Removing the `OPENUI_HOST.invoke` call from a submit handler → user
  messages don't reach the engine → silent UI.
- Changing an action / channel name → engine emits one name, design
  listens for another → no streaming, no live updates.
- Removing the Babel polling snippet → JSX scripts never compile when
  the host injects the design → white screen.
- Removing the `link[href="styles.css"]` → no styling.
- Dropping the App ↔ Studio toggle (`ModeToggle`) → users stuck on
  one face.
- Removing the `OPENUI_HOST?.invoke` fallback branch → standalone
  preview crashes.

If something in the locked zone genuinely doesn't fit, that's a
schema-level conversation, not a per-design tweak. Open an issue.

---

## Example briefs

Drop one of these into the 🔓 EDITABLE zone to start.

### Default coral

> **Brand voice**: warm and approachable but technically credible.
> Inspired by Linear's clarity and Stripe's precision; OpenClaw's
> friendliness on the App face.
>
> **Primary color**: coral (#F97560-ish).
> **Mode**: both, light primary.
> **Density**: comfortable.
>
> **Typography**:
> - Body: Inter
> - Display: Instrument Serif (App headers only)
> - Mono: JetBrains Mono (Studio only)
>
> **Inspirations**: Claude.ai (App tone), Linear (Studio density).
> **Avoid**: Tailwind-default look, generic SaaS purple.

### Dark monochrome

> **Brand voice**: terminal-inspired, minimal, no decoration.
> A power user's UI — ascetic, fast, keyboard-first even on App.
>
> **Primary color**: pure neutrals (#0a0a0a / #fafafa).
> Single accent: white on black.
> **Mode**: dark only.
> **Density**: dense.
>
> **Typography**:
> - Body: system-ui mono fallback (Berkeley Mono / IBM Plex Mono)
> - Mono: same
>
> **Inspirations**: tldraw, Vercel docs in dark mode, vim splash.
> **Avoid**: gradients, drop shadows, rounded corners >4px.

### Nordic

> **Brand voice**: cool, calm, slightly formal.
> Cozy without being cute. Library-quiet rather than coffee-shop-warm.
>
> **Primary color**: muted blue-grays (slate-400 to slate-700).
> Single warm accent: amber for soft asks.
> **Mode**: both, dark primary.
> **Density**: cozy.
>
> **Typography**:
> - Body: Inter
> - Mono: IBM Plex Mono
>
> **Inspirations**: Notion, Things 3, the Nord palette.
> **Avoid**: pure black, neon accents, monospace on the App face.

---

## Troubleshooting

**White screen when I start `reyn web`** → check the bottom of
`Reyn.html` for the Babel polling snippet (`setInterval` →
`Babel.transformScriptTags()`). The host injects designs after
`DOMContentLoaded`, so Babel's auto-runner has already fired and
finds nothing. The polling snippet is the only thing that triggers
JSX compilation in embedded mode.

**Design doesn't show in the picker** → confirm
`reyn/local/designs/<slug>/Reyn.html` exists. The picker looks for
`Reyn.html` (host-mountable runtime), *not* `Reyn UI.html` (the
design canvas with artboards). If your export only produced the
canvas, re-prompt and ask for the two-entry split.

**Engine doesn't respond when I send a message** → check the submit
handler in `Conversation`. It must `await
window.OPENUI_HOST.invoke("agent.submit", { agentId, text })`. A
common bug: the v1 export sometimes simulated a fake reply with
`setTimeout` *unconditionally* — only the standalone-preview branch
(`if (!window.OPENUI_HOST)`) should fall back to a fake reply.

**Styles don't apply** → confirm `<link href="styles.css">` is in
the HTML head. The export sometimes inlines the styles into a
`<style>` block — that works in standalone but not when the host
mounts the design as a sub-tree. The host expects an external
`styles.css`.

**App face is showing engine words** like `phase` or `artifact` →
that's a design bug, not a wiring bug. The locked contract says App
hides engine vocabulary; Studio surfaces it. Ask the canvas to
re-render those copy blocks in plain language.

---

## Contributing

A future `reyn design add gh:author/repo` flow will let you publish
your design and let others install it with one command. See
[design-distribution.md](design-distribution.md) for the design.

For now, the workflow is:

1. Author your design as above.
2. Drop into `reyn/local/designs/<slug>/` to use it locally.
3. Move into `reyn/project/designs/<slug>/` and commit it to share
   with your team.
4. Push to a public repo — when `reyn design add` lands, it'll be
   installable directly.

> **Roadmap.** `reyn design add` and the publishing pipeline are
> not yet implemented. The on-disk layout above is forward-compatible
> with the planned CLI.
