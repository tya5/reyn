# Claude Design Prompt Template — Reyn

> Paste **§ Opening Prompt** below into a fresh `claude.ai/design` thread as the
> first message. Append `→ App` or `→ Studio` to specify which face you want
> generated. Iterate via the canvas chat.
>
> The whole point of this template is **swappability**: every Claude Design
> export must drop into `web/design/<face>/` and Just Work, without rewriting
> the Reyn shell. To make that possible, the template fixes:
>
> 1. The output file structure (Claude Design produces these files, this naming)
> 2. The token schema (`tokens.json` keys are stable across designs)
> 3. The component prop contracts (Reyn's adapter layer passes these props)
>
> If a generated design violates any of these, it isn't swap-ready. Fix the
> design (or the template) before exporting.

---

## How to use

1. Open `claude.ai/design`, start a new project under the Reyn organisation.
2. Paste the **Opening Prompt** (next section), with `→ App` or `→ Studio`
   appended on the last line.
3. Iterate visuals on the canvas.
4. Before export, run through the **Acceptance Checklist** at the bottom.
5. Export → `Send to Claude Code` (handoff bundle) **or** `.zip`.
6. Drop the export into `web/designs/<name>/<face>/`, where `<name>` is
   a short slug for this design variant (e.g. `warm`, `dark`, `claude`),
   and `<face>` is `app` or `studio`. Multiple designs coexist; users pick
   one at web startup or via URL param. See
   `docs/web/multi-design-selection.md` for the selection mechanism.

   ```bash
   # Add a new design
   mkdir -p web/designs/warm/app
   unzip <new_app_export>.zip -d web/designs/warm/app

   # Replace an existing design (App face only)
   rm -rf web/designs/warm/app
   unzip <new_app_export>.zip -d web/designs/warm/app

   # If TypeScript still typechecks across all designs, swap is good.
   ```
7. Commit on a branch separate from `feat/web-gateway` (frontend integration
   happens in its own session).

---

## § Opening Prompt

````markdown
You are designing one face of a two-face product called Reyn. Read the spec
below in full before producing anything. The output will drop into a code
project that has fixed expectations for file structure, design tokens, and
component prop shapes — listed at the end of this prompt. If you can't
satisfy them, ask before generating.

## What is Reyn

Reyn is an LLM-driven workflow engine. Skills (Markdown files) declare graphs
of phases; the runtime calls the LLM, validates outputs, executes side-effect
ops, and emits an audit-grade event log. Reyn has two web faces:

- **App** — the friendly, end-user surface. Pick an agent, chat, get things
  done. Think Claude.ai / OpenClaw / ChatGPT in tone. Default landing.
- **Studio** — the dense, dev-tool surface. Build & debug Skills, inspect
  runs, edit permissions. Think Linear / Vercel / Cursor / Temporal Web UI.
  Hidden behind a "Studio" button on the App rail.

The two faces share the backend and the agent identity, but **share nothing
in visual chrome, density, or vocabulary**. Switching App ↔ Studio should
feel like opening a different mode of the same product.

> If a screen is App-side, it must never expose engine-level vocabulary
> (phase / artifact / control_ir / event / validation / schema). Studio-side
> screens should expose all of it, fluently.

## Personas

- **End user** (App) — non-technical, default UI language Japanese. Cares
  about picking the right agent and trusting that things got done.
- **Skill author** (Studio) — developer. Cares about editing skills,
  debugging runs, eval iteration.
- **Reviewer / on-call** (Studio) — replays failed runs, fixes permissions
  and budget.

## Domain (Studio-side vocabulary; App must hide all of it)

Agent, Skill, Phase, Artifact, Run, Event, Eval, Topology, Workspace,
Permission policy, Budget, Control IR op. (See the project's
`docs/web/design_brief.md` for the full glossary if it's been imported.)

## App-side vocabulary translations

| Engine | App |
|---|---|
| Agent | Agent (kept) |
| Skill | "a thing your agent can do" — phrase as a verb on cards |
| Phase | (hidden) |
| Artifact | (hidden — render as form field for entry, plain text/markdown for output) |
| Run | "Aria is working on it" / "Aria finished" |
| Event | (hidden — humanised inline status) |
| `ask_user` Control IR | Inline soft question with chip suggestions |
| Validation error | "Hmm, something didn't look right — let me try again" |
| Permission prompt | "Aria wants to read `notes.txt`. Allow once / always / no" |
| Budget hit | "You've used 80% of today's budget" toast |
| Sub-skill nesting | "Aria asked the writing helper to draft a section" |
| `loop_limit_exceeded` | "I'm having trouble making progress — want to give me more guidance?" |

## App face — visual & interaction language

- Vibe: warm, light, breathing. Closer to Claude.ai / OpenClaw / Notion's
  onboarding than to Vercel.
- Color: warm primary (coral / amber / soft teal — pick one). Light mode
  primary; dark mode polished.
- Typography: humanist sans (Inter / Geist / Söhne), generous line-height,
  large readable body. **No monospace anywhere on App.**
- Density: low. Big cards, generous padding. Mobile-friendly,
  desktop-first.
- Iconography: rounded line icons. Optional mascot avatars per agent
  (OpenClaw-style).
- Voice: first-name agents, present tense ("Aria is researching…"). No
  jargon. No exclamation marks.

### App priority screens (generate in this order)

1. **Today / home** — calm, warm, scannable. Greeting, recap of recent
   agent activity (humanised, no IDs / no event types), agent cards row,
   suggested quick-starts.
2. **Conversation** — bread-and-butter chat. Streaming text feels like
   speech. Subtle "thinking…" pill. Inline soft questions (chips) for
   `ask_user`. Background skill status as friendly inline banner ("Aria
   found 5 sources").
3. **Agent gallery + profile sheet** — pickable personalities. Tap → chat;
   long-press → friendly profile sheet with a small "Open in Studio" link.
4. **Library card → guided run** — tap a card, walk through a tiny form
   (auto-rendered from artifact JSON Schema, presented as labeled inputs
   with examples).

## Studio face — visual & interaction language

- Vibe: dev-tool dense. Dark mode primary. Linear / Vercel dashboard /
  Cursor / Temporal / LangSmith.
- Color: cool accent (teal / cyan); state colors used sparingly. Yellow =
  intervention, red = error, green = done.
- Typography: monospace for IDs / paths / JSON / event types; sans-serif
  (Inter) for chrome.
- Density: high. Tables, side rails, keyboard shortcut hints.
- Iconography: simple line icons. Glyphs `⟳ · ✗` carry over from the TUI.
- Voice: neutral, technical. Engine terms verbatim.

### Studio priority screens

1. **Conversation + live skill-run inspector** — same chat UX as App, plus
   a right rail with the live mini-map: phase graph, current phase
   highlighted, control-IR ops fired, token/cost so far.
2. **Skill graph** — interactive node-and-edge canvas. Phases as nodes;
   transitions as edges; sentinel `end` styled distinctly; sub-skills
   (`@name`) styled as nested. Hover = instructions; click = phase detail.
3. **Run timeline / event log** — vertical timeline with collapsible
   groups, filter chips by event type, jump-to-phase markers.
4. **Permissions table** — ops × rules grid, editable inline.

## Shared rules (both faces)

- Agent identity (name, color, avatar) is the same across faces.
- Top-right "App ↔ Studio" toggle on every screen.
- Edits never destroy: skill / phase / artifact edits create a new
  version; old runs replay against the version they ran with.
- The agent's spoken voice is identical across faces — only the chrome
  differs.

## ─── HARD OUTPUT REQUIREMENTS — DO NOT VIOLATE ───

The following are **machine-checked** when the design lands in code. If
your generated design doesn't satisfy them, the swap will fail.

### 1. File structure

The export must contain at minimum:

```
manifest.json              ← whatever Claude Design produces; ignored by Reyn
tokens.json                ← see § Token Schema below — REQUIRED
components/
  <ComponentName>.tsx      ← one file per component; PascalCase
  ...
pages/
  <PageName>Page.tsx       ← one file per page; PascalCase + "Page" suffix
  ...
```

No other top-level files. No `index.html` boilerplate. No `package.json`.
The Reyn shell provides routing, build tooling, and runtime.

### 2. Token schema (tokens.json)

`tokens.json` MUST validate against this schema. New designs CAN add
extra keys but MUST keep these:

```json
{
  "color": {
    "background": "<hex>",
    "surface":    "<hex>",
    "primary":    "<hex>",
    "primary_fg": "<hex>",
    "muted":      "<hex>",
    "muted_fg":   "<hex>",
    "border":     "<hex>",
    "warn":       "<hex>",
    "error":      "<hex>",
    "success":    "<hex>"
  },
  "typography": {
    "font_sans": "<css font-family stack>",
    "font_mono": "<css font-family stack>",
    "size": {
      "xs": "<rem>", "sm": "<rem>", "base": "<rem>",
      "lg": "<rem>", "xl": "<rem>", "2xl": "<rem>"
    },
    "weight": {
      "regular": 400, "medium": 500, "semibold": 600, "bold": 700
    }
  },
  "spacing": {
    "0": "0", "1": "<rem>", "2": "<rem>", "3": "<rem>",
    "4": "<rem>", "6": "<rem>", "8": "<rem>", "12": "<rem>", "16": "<rem>"
  },
  "radius": {
    "sm": "<rem>", "md": "<rem>", "lg": "<rem>", "full": "9999px"
  },
  "shadow": {
    "sm": "<css>", "md": "<css>", "lg": "<css>"
  }
}
```

Use these as CSS variables in component styles
(`var(--color-primary)`, `var(--space-4)`, etc.) so a new tokens.json
re-themes the whole UI.

### 3. Component contracts (REQUIRED components)

Each face must export the components below with **exactly** these prop
shapes. The Reyn shell's adapter layer passes props in this shape.
Components that diverge cannot be swapped in.

#### Both faces

```typescript
// components/ChatMessage.tsx
export type ChatMessageKind =
  | "agent" | "status" | "error"
  | "intervention" | "trace" | "skill_done"

export interface ChatMessageProps {
  kind: ChatMessageKind
  content: string                       // markdown OK
  agentName: string
  agentColor?: string                   // hex; falls back to default
  runId?: string                        // optional, surfaces "Open in Studio"
  timestamp: string                     // ISO 8601
  /** Only present when kind="intervention". Choices to render as chips. */
  choices?: { id: string; label: string }[]
  /** Only present when kind="intervention". Called with chosen id or free text. */
  onAnswer?: (answer: { choiceId?: string; text?: string }) => void
}

// components/AgentCard.tsx
export interface AgentCardProps {
  name: string
  color: string                         // hex
  avatarUrl?: string
  tagline: string                       // one-line "what I'm good at"
  lastActiveAt?: string                 // ISO 8601, optional
  onClick: () => void
}

// components/ModeToggle.tsx
export interface ModeToggleProps {
  current: "app" | "studio"
  onChange: (next: "app" | "studio") => void
}
```

#### App face only

```typescript
// components/QuickstartCard.tsx
export interface QuickstartCardProps {
  title: string                         // verb phrase, e.g. "Research a topic"
  description: string
  iconKey?: string                      // optional icon hint; renderer maps it
  onClick: () => void
}

// components/RecapLine.tsx
export interface RecapLineProps {
  agentName: string
  agentColor: string
  /** Already humanised — engine vocab MUST NOT appear here. */
  message: string
  runId?: string                        // optional "Open in Studio" link
}

// pages/TodayPage.tsx
export interface TodayPageProps {
  greeting: string                      // "Good morning, Tetsuya"
  recap: RecapLineProps[]
  agents: AgentCardProps[]
  quickstarts: QuickstartCardProps[]
}

// pages/ConversationPage.tsx (App)
export interface ConversationPageProps {
  agentName: string
  agentColor: string
  messages: ChatMessageProps[]
  onSubmit: (text: string) => void
  /** Set when an ask_user is pending; render as inline soft question. */
  pendingIntervention?: ChatMessageProps  // kind="intervention"
}
```

#### Studio face only

```typescript
// components/EventTimelineItem.tsx
export interface EventTimelineItemProps {
  type: string                          // raw engine event type, displayed verbatim
  timestamp: string                     // ISO 8601
  payload: Record<string, unknown>      // opaque; render as collapsible JSON
  isError?: boolean
}

// components/SkillGraphNode.tsx
export interface SkillGraphNodeProps {
  phaseName: string                     // verbatim
  isCurrent?: boolean
  isError?: boolean
  visitCount?: number
  onClick: () => void
}

// pages/RunDetailPage.tsx
export interface RunDetailPageProps {
  runId: string
  skillName: string
  status: "running" | "finished" | "failed" | "aborted"
  startedAt: string
  durationMs?: number
  events: EventTimelineItemProps[]
  /** Adjacency for the skill graph mini-map, if available. */
  graph?: { nodes: SkillGraphNodeProps[]; edges: { from: string; to: string }[] }
}

// pages/PermissionsPage.tsx
export interface PermissionsPageProps {
  rules: {
    op: string                          // verbatim, e.g. "file.write"
    pattern: string
    decision: "allow" | "deny" | "prompt"
  }[]
  onEdit: (index: number, next: { decision: "allow" | "deny" | "prompt" }) => void
  onDelete: (index: number) => void
}
```

### 4. Anti-requirements (do not do)

- Do **NOT** include hardcoded mock data. All data must come from props.
- Do **NOT** include `fetch()` calls or imports of HTTP / WebSocket
  clients. Reyn's shell injects data.
- Do **NOT** introduce global state (Zustand, Redux, Recoil, Jotai, …).
  The shell owns state.
- Do **NOT** ship a Tailwind config / framework wrapper. Use plain CSS or
  CSS-in-JS scoped per component, referencing `tokens.json` via
  CSS variables.
- Do **NOT** invent new component names beyond those above without
  flagging it in the canvas chat first. Extra components are fine but
  they need a Reyn-side contract before they ship.
- Do **NOT** mix App and Studio component aesthetics in one export. One
  face per export.
- Do **NOT** include test files, Storybook configs, build scripts,
  README, or `.gitignore`. The shell owns those.

### 5. i18n hooks

All user-visible strings in App-face components must be passed via props
(do not hardcode English or Japanese inside components). The shell maps
Reyn's `output_language` setting to the appropriate string before
passing it down. Studio-face strings can stay English (developer
audience).

---

## Now, generate the design

Append one of the following lines to specify which face:

> `→ App`
>
> `→ Studio`

Begin by enumerating which priority screens you'll cover and what tokens
you propose, then iterate.
````

---

## Acceptance Checklist (before export)

Cowork session runs through this before clicking Export:

- [ ] **One face per export** — App and Studio are separate exports.
- [ ] **All required components present** — see § 3 above for the list.
- [ ] **Component prop shapes match the contract verbatim** (extra optional
      props OK; missing required props NOT OK).
- [ ] `tokens.json` validates against § 2 schema (all required keys present,
      values valid CSS).
- [ ] **No hardcoded mock data** in components — all from props.
- [ ] **No HTTP / WebSocket / global state** in components.
- [ ] **No Tailwind / framework configs** — plain CSS, CSS variables only.
- [ ] **App face strings come via props** (i18n hook); Studio face strings
      can be inline English.
- [ ] **App face never says** `phase`, `artifact`, `control_ir`, `event`,
      `validation`, or `schema` (case-insensitive grep over the export).
- [ ] **Studio face uses engine terms verbatim** — no humanised paraphrases.
- [ ] **App ↔ Studio toggle present** on every page-level component.

If anything fails, fix it in the canvas before exporting.

---

## Drop-in procedure (after export)

```bash
# Pick a slug for this design variant ("warm", "dark", "claude", etc.)
DESIGN=warm
FACE=app    # or studio

TARGET="web/designs/${DESIGN}/${FACE}"

# Wipe and replace this face of this design
rm -rf "$TARGET"
mkdir -p "$TARGET"
unzip <new_export>.zip -d "$TARGET"

# Verify the swap is clean across all designs
cd web && npm run typecheck
# - If typecheck passes, contracts hold for the new design; users can
#   pick it via the design selector at startup.
# - If typecheck fails, the diff tells you which contract broke. Fix
#   the design (re-prompt Claude Design with the missing contract
#   excerpt) or, if the contract itself needs to evolve, propose the
#   change on the shell side.
```

Adding a brand-new design alongside existing ones is the same procedure
with a fresh `$DESIGN` slug. Removing a design is `rm -rf web/designs/<name>`.
Users select among available designs at startup; see
`docs/web/multi-design-selection.md`.

---

## When to update this template

- A new required component on either face → add to § 3 component
  contracts, bump tokens.json schema if needed, re-prompt designs that
  predate the change.
- Reyn data model changes (new WebSocket message kind, new agent profile
  field) → update the contract for the affected component.
- Brand pivot → only `tokens.json` should need to change. If components
  themselves need different markup, add a new component name (don't
  mutate the existing contract).

The whole point of fixing component contracts is that a brand pivot only
changes pixels and tokens, not code structure on the Reyn side.
