# Claude Design Prompt — Reyn (reyn-ui/v1)

> **Navigation**: this is the operational document for prompting Claude
> Design. The architecture (3-layer model + AG-UI evaluation) is in
> [engine-design-contract.md](engine-design-contract.md). The protocol
> spec itself is in [docs/openui/](../openui/). The component contracts
> a design must satisfy are in
> [docs/openui/schemas/reyn-ui-v1/components.md](../openui/schemas/reyn-ui-v1/components.md).

This document holds the **prompt** Cowork pastes into a fresh
`claude.ai/design` thread to generate a reyn-ui/v1 conformant design.
It is intentionally short — the spec lives elsewhere; the prompt
references it.

---

## How to use

1. Open `claude.ai/design`, start a new project.
2. Paste the **§ Opening Prompt** below.
3. Append `→ App` or `→ Studio` on the last line to choose which face.
4. Iterate visuals on the canvas.
5. Before export, run through the **§ Acceptance Checklist**.
6. Export → `.zip` (preferred) or `Send to Claude Code`.
7. Drop into `reyn/local/designs/<slug>/`. See
   [multi-design-selection.md](multi-design-selection.md) for layout.

The prompt is in **English** because Claude Design responds more
reliably to English instructions, even when the resulting UI strings
are Japanese (i18n keys are passed via `OPENUI_DATA.COPY` at runtime —
the design itself does not hardcode language). See feedback memory
`feedback_claude_design_english.md`.

---

## § Opening Prompt

````markdown
You are designing a UI for **Reyn**, a workflow-engine-driven agent OS.
The design will integrate with Reyn via the **OpenUI Layer 0 protocol**
and the **reyn-ui/v1 Layer 1 schema**. Read this entire prompt before
producing anything.

## What is Reyn

Reyn lets non-technical users converse with specialist AI agents and
lets developers build & ship those agents from Markdown. Underneath,
it is an LLM-driven workflow engine; designs render the UI that wraps
that engine.

The Reyn UI has **two faces**:

- **App** — friendly end-user surface (default landing). Pick an agent,
  chat, get things done. Tone: Claude.ai / OpenClaw / ChatGPT. Hides
  engine vocabulary entirely.
- **Studio** — dense developer surface. Build & debug skills, inspect
  runs, edit permissions. Tone: Linear / Vercel / Cursor / Temporal /
  LangSmith. Surfaces engine vocabulary verbatim.

The two faces share the agent identity (name, color, avatar) and a
top-right App ↔ Studio toggle, but **share nothing else** — different
chrome, density, vocabulary.

## How the design connects to the engine

Reyn implements the **OpenUI Layer 0 protocol**. The design reads four
globals on `window`:

- `window.OPENUI_HOST` — `{ invoke(action, payload?), listen(channel, handler) }`
- `window.OPENUI_DATA` — initial data (shape: `ReynUiData`)
- `window.OPENUI_SCHEMA` — should be `"reyn-ui/v1"`
- `window.OPENUI_DESIGN_MODE` — `true` in standalone preview, `false`
  when embedded in the host

Pattern at boot:

```js
if (window.OPENUI_HOST && window.OPENUI_SCHEMA === "reyn-ui/v1") {
  // Embedded in Reyn host: use real data and route callbacks to host
  const data = window.OPENUI_DATA; // type: ReynUiData
  // user actions: window.OPENUI_HOST.invoke("agent.submit", { agentId, text })
  // streams:      window.OPENUI_HOST.listen("agent.message", handler)
} else {
  // Standalone preview (designer mode): use mock data, log actions to console
}
```

The design MUST work in both modes. Standalone preview shows the
design on top of mock data; embedded mode shows it on top of real
Reyn data piped through the host adapter.

## Required components, data shape, actions, channels

Do NOT redefine these here. They are specified canonically in:

- **Component contracts** (which components to export, their props):
  see `docs/openui/schemas/reyn-ui-v1/components.md`
- **Data shape** (`ReynUiData` type tree):
  see `docs/openui/schemas/reyn-ui-v1/data.types.ts`
- **Actions and channels** (what `invoke` and `listen` accept):
  see `docs/openui/schemas/reyn-ui-v1/manifest.yaml`

You will be given the contents of these files when you start. Treat
them as the contract: every required component must be exported,
prop shapes must match, action / channel names must be used as
defined.

## Visual brief

For visual / interaction direction (App's warm friendly tone, Studio's
dense developer tone, screen layouts, color guidance, density,
typography), see `docs/web/design_brief.md`. Do not deviate from the
brief without flagging it in the canvas chat.

## Hard rules

- **No hardcoded mock data inside components.** Components read from
  `window.OPENUI_DATA` (shape: `ReynUiData`) when embedded, or from a
  fallback mock when standalone. Mock data lives in a separate
  `data.js` (or equivalent) so it can be replaced.
- **All user actions go through `window.OPENUI_HOST.invoke()`.** Every
  user-driven side-effect (sending a message, answering an
  intervention, attaching to an agent, switching face, accepting a
  permission, cancelling a run, …) MUST `await
  window.OPENUI_HOST.invoke(<action>, <payload>)`. The action / payload
  shapes are listed in the schema's `manifest.yaml`. Local-only state
  updates (toggling a sidebar, focusing an input) stay in component
  state and do NOT need invoke.

  Concrete example — Conversation submit handler:

  ```js
  const submit = async (text) => {
    if (!text.trim()) return;
    setMessages(m => [...m, { kind: "user", text, complete: true }]);
    setInput("");
    if (window.OPENUI_HOST?.invoke) {
      await window.OPENUI_HOST.invoke("agent.submit", { agentId, text });
    } else {
      // Standalone preview only: synthesise a fake reply.
      simulateLocalReply(text, setMessages);
    }
  };
  ```

  The `OPENUI_HOST?.invoke` check is the ONLY place mock behaviour is
  acceptable: standalone preview without a host attached. When embedded,
  the host always wins. Components that simulate replies / data updates
  unconditionally are non-conforming.

- **All async data goes through `window.OPENUI_HOST.listen()`.**
  Streaming agent messages, live event log entries, run-status
  changes, budget updates — anything pushed by the backend — MUST
  subscribe via:

  ```js
  React.useEffect(() => {
    if (!window.OPENUI_HOST?.listen) return;
    const off = window.OPENUI_HOST.listen("agent.message", (chunk) => {
      setMessages(m => /* append/extend the streaming message */);
    });
    return off;  // unsubscribe on unmount
  }, [agentId]);
  ```

  Channel names are in the schema's `manifest.yaml`. Polling with
  `setInterval` against `OPENUI_DATA` is non-conforming; the host
  emits diffs via channels.

- **No `fetch` / `XMLHttpRequest` / `WebSocket` calls inside
  components.** Those are the host's job. Components see only
  `OPENUI_HOST.invoke` / `OPENUI_HOST.listen` / `OPENUI_DATA`.
- **No global state libraries** (Zustand, Redux, Recoil, Jotai, …).
  The host owns durable state. Local component state for transient
  UI is fine.
- **No bundler / framework configs** (Tailwind config, postinstall
  scripts, `package.json`). The host owns the build.
- **App face vocabulary**: never expose the words `phase`, `artifact`,
  `control_ir`, `event`, `validation`, `schema`. Studio face uses these
  verbatim.
- **i18n**: App face strings come from `OPENUI_DATA.COPY[lang]` via a
  `t(key, lang)` helper. Studio face strings may be inline English.
- **Both faces in one export is fine.** Generate App and Studio
  together (canonical layout: App screens in `app-screens.jsx`
  exporting `window.AppScreens.{...}`, Studio screens in
  `studio-screens.jsx` exporting `window.StudioScreens.{...}`,
  shared CSS variables in one `styles.css`). Single-face exports
  are acceptable if focusing on one face.
- **Two HTML entries (REQUIRED)**:
  - **`Reyn.html`** — *host-mountable runtime*. Single full-screen
    mount that renders `<AppPrototype/>` or `<StudioPrototype/>` based
    on the URL hash (`#studio` → Studio, anything else → App). No
    artboards, no design-canvas chrome. This is what `reyn web` loads.
  - **`Reyn UI.html`** — *design canvas*. Artboards side-by-side for
    design review, optional Tweaks panel for theme / density / lang
    switching. Designer mode only.

  The host shell loads `Reyn.html` and expects globals to come from
  the bundled scripts. `Reyn UI.html` is for `claude.ai/design` and
  for opening the export directly in a browser.

## Designer-mode niceties (optional)

When `window.OPENUI_DESIGN_MODE === true`, you MAY render a small
designer-only chrome (theme tweaks panel, color / density switcher).
Gate it explicitly so the host (with `OPENUI_DESIGN_MODE = false`)
never sees it. The two-entry split above is the canonical way to
isolate designer chrome from host-mode runtime.

## Now generate

Append one of these to specify which face:

> `→ App`
>
> `→ Studio`

Begin by enumerating which screens you'll cover and your token /
typography proposal, then iterate.
````

---

## § Acceptance Checklist

Run through this before clicking Export. Anything failing here means
the design will not load cleanly in Reyn — fix it in the canvas first.

### Structure

- [ ] **Faces are coherently scoped.** Both faces in one export is
      preferred (canonical layout); single-face exports also OK.
      Don't mix App-side screens and Studio-side screens into the
      same `window.AppScreens` global — keep them separate per the
      schema's `surface` declarations.
- [ ] **Two HTML entries**: `Reyn.html` (host-mountable runtime,
      hash-routed App/Studio mount, no artboards) and `Reyn UI.html`
      (design canvas with artboards). The host shell fetches the
      former; the latter is for design review.
- [ ] **All required components present** for the chosen face. See
      [reyn-ui/v1 components.md](../openui/schemas/reyn-ui-v1/components.md).
- [ ] **Component prop shapes match the contract verbatim** (extra
      optional props are OK; missing required props are not).

### Behaviour

- [ ] **All user actions invoke the host.** `grep` the export for
      every event handler that mutates app state (sending a message,
      answering an intervention, switching agents, accepting a
      permission, etc.). Each MUST call
      `window.OPENUI_HOST.invoke(<action>, <payload>)`. Local-only
      mock side-effects are permitted ONLY inside
      `if (!window.OPENUI_HOST) { ... }` fallback branches.
- [ ] **All async data uses `listen`.** Streaming messages, run
      events, budget updates, etc. subscribe via
      `window.OPENUI_HOST.listen(<channel>, handler)` and unsubscribe
      on unmount. No `setInterval` polling against `OPENUI_DATA`.
- [ ] **`window.OPENUI_DATA`** is the source of truth for the initial
      / static-shape data; `data.js` (or equivalent) only provides the
      fallback mock.
- [ ] **`window.OPENUI_DESIGN_MODE`** gates any designer-only chrome
      (tweaks panel etc.).
- [ ] **`window.OPENUI_SCHEMA === "reyn-ui/v1"`** check at boot, with a
      clear console warning if the schema doesn't match.

### Anti-requirements

- [ ] **No hardcoded mock data inside components** — only via
      `OPENUI_DATA` / fallback module.
- [ ] **No silent fake replies.** A Conversation that fabricates an
      agent reply via `setTimeout` outside the standalone-only
      fallback branch is non-conforming. Real reply chunks come from
      `OPENUI_HOST.listen("agent.message", ...)`.
- [ ] **No HTTP / WebSocket / global state** in components.
- [ ] **No Tailwind / framework configs.** Plain CSS, CSS variables.

### Surface vocabulary

- [ ] **App face never says** `phase`, `artifact`, `control_ir`,
      `event`, `validation`, `schema` (case-insensitive grep over
      the export, App face only).
- [ ] **Studio face uses engine terms verbatim** — no humanized
      paraphrases.

### Chrome

- [ ] **App ↔ Studio toggle** present on every page-level component
      (uses `ModeToggle` from
      [components.md](../openui/schemas/reyn-ui-v1/components.md)).

If anything fails, fix it in Claude Design before exporting.

---

## Drop-in procedure (after export)

```bash
DESIGN=<your-slug>     # e.g. "warm", "lobster", "v1"

# Wipe and replace this design's directory
rm -rf "reyn/local/designs/$DESIGN"
mkdir -p "reyn/local/designs/$DESIGN"
unzip <export>.zip -d "reyn/local/designs/$DESIGN"

# When the host implementation lands (PR30), starting `reyn web`
# will discover and offer this design in the picker.
```

To target `reyn/project/designs/<slug>/` (committed to the team's
project) instead, replace `local` with `project` in the path. See
[multi-design-selection.md](multi-design-selection.md) for the
three-root layout and selection priority.

---

## When to revise the prompt

- A new required component / new minor of `reyn-ui/v1` →
  re-prompt the design (existing designs MAY skip optional new
  components and host falls back).
- A breaking change (`reyn-ui/v2`) → re-prompt against the new schema
  identifier, or pin existing designs to v1 with the older host.
- Brand pivot → only the visual brief (`design_brief.md`) changes;
  this prompt remains.

The point of fixing the contract in `docs/openui/` is that visual
iteration only requires Claude Design to re-read the brief and the
schema files; the structural prompt above stays stable.
