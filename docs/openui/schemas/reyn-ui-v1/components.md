# reyn-ui/v1 — Component Contracts

This document is the prose companion to [manifest.yaml](manifest.yaml)'s
`components` section. For each component a design must export, it gives
the full prop contract, surface-specific guidance, and notes on what the
component renders and what it does not.

The vocabulary is App / Studio. App = the friendly end-user surface;
Studio = the dense developer surface. See `docs/web/design_brief.md`
for the full design framing.

---

## Conventions

- All components are **React components** exported by name from the design.
  How the design organises files (one file per component, or
  `app-screens.jsx` exporting many via `window.AppScreens`) is up to the
  design; what matters is that the host can locate components by name.
- `lang: "en" | "ja"` is passed to every component that renders user-
  visible text on the App face. Studio components MAY ignore it.
- Every page-level component MUST include a `ModeToggle` (or call into a
  parent that renders one) so users can flip App ↔ Studio at any time.
- Components do not call `fetch` / `WebSocket` / global state. They
  receive data via `window.OPENUI_DATA` (read at mount) and via the props
  the host passes; they emit user actions via the callbacks in props,
  which the host wires to `OPENUI_HOST.invoke(...)` / `listen(...)`.

---

## App face

### `TodayScreen`

**Surface**: App. **Required**: yes.

The default landing for end users. Greeting, recap of recent agent
activity, agent cards row, suggested quickstarts.

```typescript
interface TodayScreenProps {
  /** Called when the user taps an agent card or "Chat with Aria" button. */
  onPickAgent: (agentId: string) => void;
  /** Called when the user taps "Open Library". */
  onOpenLibrary: () => void;
  /** Active language. */
  lang: "en" | "ja";
  /** Layout variant. Default `"default"` if absent. */
  layout?: "default" | "hero" | "agents-first";
}
```

**Renders from `OPENUI_DATA`**: `AGENTS`, `RECAP`, `QUICKSTARTS`, `COPY[lang]`.

**Vocabulary**: humanized only — never `phase`, `artifact`, `run id`, `event`.
Recap entries' `text` field comes pre-humanised from the host.

---

### `Conversation` (App)

**Surface**: App. **Required**: yes.

The bread-and-butter chat. Streaming text, "thinking…" pill while the
agent works, inline soft questions for `ask_user` interventions, friendly
"Aria is researching…" status banners.

```typescript
interface ConversationProps {
  /** Which agent is being conversed with. */
  agentId: string;
  /** User submitted a chat message. Wires to `agent.submit`. */
  onSubmit: (text: string) => void;
  /** User answered an intervention. Wires to `agent.intervention.answer`. */
  onAnswerIntervention: (a: { choiceId?: string; text?: string }) => void;
  /** "Open in Studio" tap. Hosts navigate the user to the Studio side. */
  onOpenStudio: () => void;
  lang: "en" | "ja";
}
```

**Renders from `OPENUI_DATA`**: `AGENTS` (find by id), `CONVO_ARIA` (the
transcript fixture for the active agent), `COPY[lang]`.

**Subscribes to**: `agent.message` (append), `run.started` /
`run.finished` (humanise as inline status banner), `phase.started` /
`phase.finished` (optional, humanise as "Aria found 3 sources").

**Vocabulary**: hidden engine terms; surface humanised activity instead.

---

### `AgentsGallery`

**Surface**: App. **Required**: yes.

Tile-style gallery of agents-as-personalities. Big avatar / accent-color
tiles with one-line "what I'm good at".

```typescript
interface AgentsGalleryProps {
  /** Tap an agent tile → start chatting. */
  onPickAgent: (agentId: string) => void;
  /** Long-press / "···" → open the friendly profile sheet. */
  onOpenProfile: (agentId: string) => void;
  /** Tap "+ Add agent" → friendly creation flow. */
  onAddAgent: () => void;
  lang: "en" | "ja";
}
```

**Renders from `OPENUI_DATA`**: `AGENTS`, `COPY[lang]`.

---

### `AgentProfileSheet`

**Surface**: App. **Required**: yes.

A friendly profile sheet — name, "what I'm good at" (humanized
`role` + `allowed_skills`), recent activity. Has a small "Open in
Studio" link for power users.

```typescript
interface AgentProfileSheetProps {
  agentId: string;
  /** Close the sheet. */
  onClose: () => void;
  /** "Open in Studio" — host navigates to Studio agent settings. */
  onOpenStudio: (agentId: string) => void;
  lang: "en" | "ja";
}
```

**Renders from `OPENUI_DATA`**: `AGENTS` (find by id), `RECAP` (filter by
`agent`), `COPY[lang]`.

---

### `LibraryScreen`

**Surface**: App. **Required**: yes.

A friendly catalog of what the agents *can do*, presented as cards:
"Write a blog post", "Research a topic", "Summarize a long article",
"Build a new skill". Tapping a card opens `GuidedRunFlow`.

```typescript
interface LibraryScreenProps {
  /** Tap a card → start the guided run flow. */
  onPickCard: (libraryItemId: string) => void;
  lang: "en" | "ja";
}
```

**Renders from `OPENUI_DATA`**: `LIBRARY`, `COPY[lang]`.

---

### `GuidedRunFlow`

**Surface**: App. **Required**: yes.

After a user taps a Library card, walks them through a tiny form whose
fields come from the underlying skill's entry-phase artifact JSON Schema
— rendered as labeled inputs with examples and helper text. On submit,
launches the skill and shows progress humanised.

```typescript
interface GuidedRunFlowProps {
  /** Which Library item the user is running. */
  libraryItemId: string;
  /** Run finished successfully — pass back any final output. */
  onComplete: (result: unknown) => void;
  /** User cancelled before submitting. */
  onCancel: () => void;
  lang: "en" | "ja";
}
```

**Renders from `OPENUI_DATA`**: `LIBRARY` (find by id), `COPY[lang]`.

**Subscribes to**: `run.started` / `run.finished` (humanise progress),
`agent.message` (any agent intermediate status during the run).

---

## Studio face

### `StudioConversation`

**Surface**: Studio. **Required**: yes.

Same conversation as App side, plus a right rail with the live
skill-run inspector: phase graph, current phase highlighted, control-IR
ops fired, token/cost so far. The bridge between the two faces.

```typescript
interface StudioConversationProps {
  agentId: string;
  /** If a run is active, its id; else undefined. */
  runId?: string;
  /** User clicked an event in the right-rail event log. */
  onSelectEvent: (eventId: number) => void;
  lang: "en" | "ja";
}
```

**Renders from `OPENUI_DATA`**: `AGENTS`, `CONVO_ARIA_STUDIO` (engine-
level transcript), `SKILL_GRAPH`, `RUN_EVENTS`, `COPY[lang]`.

**Subscribes to**: `agent.message`, `run.started` / `run.finished`,
`phase.started` / `phase.finished`, `state.delta`.

**Vocabulary**: engine terms verbatim — `phase`, `artifact`, event
type names, control-IR op names — Studio audience expects them.

---

### `SkillGraphPage`

**Surface**: Studio. **Required**: yes.

Interactive node-and-edge canvas. Phases as nodes; allowed transitions
as edges; sentinel `end` styled distinctly; sub-skills (`@name`) styled
as nested. Hover = instructions; click = phase detail.

```typescript
interface SkillGraphPageProps {
  /** Which skill to render. */
  skillName: string;
  /** Click a phase node → open phase detail. */
  onSelectPhase: (phaseId: string) => void;
  lang: "en" | "ja";
}
```

**Renders from `OPENUI_DATA`**: `SKILL_GRAPH`, `SKILL_MD`, `COPY[lang]`.

---

### `RunTimelinePage`

**Surface**: Studio. **Required**: yes.

Vertical timeline with collapsible event groups, filter chips by event
type, jump-to-phase markers. Selecting an event opens a side panel with
full payload (ContextFrame, LLM input/output, IR op args).

```typescript
interface RunTimelinePageProps {
  runId: string;
  /** Selecting an event in the timeline. */
  onSelectEvent: (eventId: number) => void;
  lang: "en" | "ja";
}
```

**Renders from `OPENUI_DATA`**: `RUN_EVENTS`, `RUNS_LIST`, `SKILL_GRAPH`,
`COPY[lang]`.

---

### `PermissionsPage`

**Surface**: Studio. **Required**: yes.

Ops × rules grid, editable inline.

```typescript
interface PermissionsPageProps {
  /** User edited a row → wires to `permission.update`. */
  onUpdate: (rule: PermissionRule) => void;
  /** User deleted a row → wires to `permission.remove`. */
  onRemove: (op: string, glob: string) => void;
  lang: "en" | "ja";
}
```

**Renders from `OPENUI_DATA`**: `PERMISSIONS`, `COPY[lang]`.

---

## Shared chrome

### `ModeToggle`

**Surface**: shared. **Required**: yes.

The App ↔ Studio toggle, present on every screen (top-right corner per
the design brief).

```typescript
interface ModeToggleProps {
  mode: "app" | "studio";
  onChange: (next: "app" | "studio") => void;
  lang: "en" | "ja";
}
```

This is the only shared component; per the design brief, App and Studio
share *no* visual chrome aside from agent identity (name, color, avatar).
Agent identity flows through props on the relevant screen, not through
shared components.

---

## Notes for design authors

- **Don't fetch**. The host populates `OPENUI_DATA` and emits channel
  events. If you find yourself wanting `fetch()`, you probably want
  `OPENUI_HOST.invoke('data.refetch')` or a dedicated action / channel
  in the schema.
- **Don't store global state**. The host owns durable state. Local
  component state for transient UI (input text, expanded/collapsed) is
  fine.
- **Match the surface vocabulary**. App face MUST avoid `phase`,
  `artifact`, `event`, etc. Studio face MUST surface them verbatim.
- **One component per name**. The host looks components up by exact
  name. Sub-components inside a screen are fine — they just aren't
  part of the contract.
