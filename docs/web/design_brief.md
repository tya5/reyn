# Reyn — Claude Design Brief

> Paste this whole document as the opening message in `claude.ai/design`. It contains everything Claude Design needs to draft a first-pass UI for Reyn's browser app.

---

## 0. The single most important framing

**Reyn has two faces and the UI must respect that split.**

- **App** (default landing) — a warm, friendly, end-user experience. Pick an agent, talk to it, get things done. Think **OpenClaw** ("the lobster way" — your AI buddy on every channel), **Claude.ai**, **ChatGPT** — the kind of surface a non-technical friend could use without ever hearing the words "phase," "artifact," or "control IR." This is what we show first.
- **Studio** (separate entry point) — a dense, dev-tool surface for building and debugging Skills. Skill graph editor, run inspector with the full event log, eval workflows, permissions, topology editing. Closer to **Linear / Vercel / Cursor / Temporal Web UI / LangSmith** in tone. Hidden behind a single "Studio" / "Build" button in the sidebar; never bleeds into the App surface.

The two faces share the same backend and the same data model. They share **nothing** in visual chrome, density, or vocabulary. Switching from App to Studio should feel like opening a different mode of the same product — like Notion's view modes, or like switching from Cursor's chat sidebar to its file tree.

> **Design rule for everything below:** if a screen is on the App side, it must never expose engine-level vocabulary (phase / artifact / control_ir / event / validation). If a screen is on the Studio side, it should expose all of it, fluently.

---

## 1. Product in one sentence

**Reyn lets anyone use specialist AI agents through a friendly chat (App), and lets developers build & ship those agents from Markdown (Studio).** Underneath, it's an LLM-driven workflow engine: skills are graphs of phases declared in `.md` files, the runtime calls the LLM, validates outputs against JSON Schema, executes side-effect ops, and emits an audit-grade event log.

End users never see any of that. Builders see all of it.

## 2. Personas

| Persona | Where they live | What they care about |
|---|---|---|
| **End user / agent operator** (primary on App) | App | Picking the right agent, having a smooth chat, trusting that things got done |
| **Skill author / developer** (primary on Studio) | Studio | Editing skills, debugging runs, iterating on quality via evals |
| **Reviewer / on-call** | Studio | Replaying a failed run, reading event logs, fixing permissions / budget |

The end-user persona is the **brand surface**. Don't assume technical literacy. Don't assume English literacy either — default UI language is Japanese.

## 3. Core domain model (Studio-side vocabulary; App must hide it)

Names match the codebase. The App surface translates these into human terms (see §6).

- **Agent** — a long-lived chat persona. Has `name`, `role` (free-text system-prompt addendum), `allowed_skills` (allowlist; null = unrestricted, [] = no skills, [...] = scoped), `created_at`, last activity, history.
- **Skill** — a Markdown file declaring `entry`, `graph` (allowed phase transitions), `final_output`, optional `finish_criteria`. Resolved in order: `reyn/project/` → `reyn/local/` → `src/stdlib/skills/`. Skills can embed sub-skills via `@skill_name` graph nodes.
- **Phase** — stateless processing unit. Declares only `input` (artifact type) + free-form instructions. Output schema is determined externally (next phase's input or skill's `final_output`).
- **Artifact** — typed, JSON-Schema-validated structured data passed between phases. YAML file with `name` + `schema` + optional `wrapped: false` flag.
- **Run** — one execution of a skill. Has a `run_id`, event log (JSONL), per-phase artifacts, cost, status.
- **Event** — append-only audit record. Types in Appendix B.
- **Eval** — Markdown spec with cases & per-phase quality criteria. Run via LLM-as-judge.
- **Topology** — agent-to-agent communication graph: `network` (free), `team` (leader-centric), `pipeline` (sequence).
- **Workspace** — canonical state store for a chat/run. Single source of truth.
- **Permission policy** — per-op rules (`allow`, `deny`, `prompt`). Persisted to `.reyn/approvals.yaml`.
- **Budget** — per-day / per-month token & cost ledger; can block runs that exceed limits.
- **Control IR op** — side effect a phase requests *before* deciding the next transition. Catalog: `file` (read/write/glob/grep/edit/delete), `ask_user`, `lint`, `run_skill`, `shell` (gated), `mcp`, `web_search`, `web_fetch`.

## 4. Information architecture

A persistent left rail with **mode switching at the top**, not section navigation. The two modes have completely different sub-navigation.

```
┌─ App  ▼ ─┐
│ ▸ Today  │           ← App-mode nav (friendly, sparse)
│ ▸ Agents │
│ ▸ Library│
└──────────┘
   . . . . .
[ Open Studio → ]      ← persistent button at the bottom of the App rail
```

```
┌─ Studio  ▼ ──┐
│ ▸ Skills     │       ← Studio-mode nav (dev-tool, dense)
│ ▸ Runs       │
│ ▸ Evals      │
│ ▸ Topologies │
│ ▸ Settings   │
└──────────────┘
   . . . . .
[ ← Back to App ]
```

### 4a. App mode (end-user surface)

Three sub-routes. Friendly, breathing room, plain language.

1. **Today** (default landing) — a calm home screen. Shows: a greeting, a one-line "what your agents have been up to" recap (last completed runs, surfaced as "Aria finished researching renewable-energy trends — see what she found"), a row of agent "cards" you can tap to start chatting, and a few suggested quick-starts ("Summarize a webpage", "Plan a trip", "Review my draft"). No metrics. No graphs.
2. **Agents** — a gallery of agents-as-personalities. Big avatar / accent-color tiles. Each tile shows the agent's name, a one-line "what I'm good at" (derived from `role` + a humanized version of `allowed_skills`), and a "Chat" CTA. Tap a tile → conversation. A "+ Add agent" tile opens a friendly creation flow ("Give your agent a name and a personality" — no mention of `role` field or YAML).
3. **Library** — a friendly catalog of what the agents *can do*, presented as cards: "Write a blog post," "Research a topic," "Summarize a long article," "Build a new skill." Clicking a card kicks off the relevant skill, optionally collecting required inputs through a step-by-step form (the form is generated from the skill's entry-phase artifact schema, but the user just sees labeled fields with examples).

That's it. Three routes. No "Skills" / "Runs" / "Evals" terminology anywhere on this side.

### 4b. Studio mode (developer surface)

Five sub-routes. Dense, keyboard-first, exhaustive.

1. **Skills**
   - Skills list (table): name, source (project / local / stdlib), entry phase, # of phases, last run.
   - Skill detail: rendered graph (visual node diagram of phases + transitions), `skill.md` viewer, phases tab, artifacts tab, "Run this skill" CTA.
   - Phase detail: input artifact, instructions (Markdown), incoming/outgoing edges.
   - Artifact detail: schema preview (rendered tree), example payload.
2. **Runs** (event-log explorer)
   - Runs list: skill, started_at, duration, status, cost.
   - Run detail: event timeline with filter chips (`phase_started`, `llm_called`, `validation_error`, …); selecting an event opens a side panel with full payload (ContextFrame, LLM input/output, IR op args). "Replay" toggle steps through events. Skill graph annotated with each phase's visit count and any errors.
3. **Evals**
   - Eval specs list per skill.
   - Eval run detail: per-case, per-phase quality criteria with pass / fail / aspirational badges; LLM judge rationales expandable.
   - Score-over-time chart for skill iteration.
4. **Topologies**
   - List & visual editor: network / team / pipeline graphs with drag-to-add edges.
5. **Settings**
   - **Agents** (advanced): edit `role` prompt, `allowed_skills` allowlist, raw profile YAML.
   - **Permissions**: ops × rules table, editable inline.
   - **Models & Limits**: model class mapping (light/standard/strong), per-call timeout, retries, max phase visits.
   - **Budget**: daily/monthly caps, current usage, ledger view.
   - **Project context**: edit `REYN.md` / `CLAUDE.md` references; edit `reyn.yaml`.

## 5. Priority screens for the first design pass

**App side (do these first — they are the brand surface):**

1. **Today / home** — calm, warm, scannable. Greeting, recap of what agents have been doing (humanized — no IDs, no event types), agent cards row, suggested quick-starts. This is the screen a new user lands on; it has to be inviting.
2. **Conversation** — the bread-and-butter. Big readable type, generous spacing, streaming text that feels like someone talking. Subtle "thinking…" pill while the agent works (replaces engine `status` events). When the agent runs a skill in the background, show a friendly "Aria is researching" / "Aria found 5 sources" inline status, not a phase-by-phase log. If the agent needs input mid-task (`ask_user`), show it as a soft inline question with optional suggestion chips — never a modal.
3. **Agent gallery + agent profile sheet** — pickable personalities. Tap a tile → conversation. Long-press / "···" → a friendly profile sheet (name, what they're good at, recent activity). The sheet has a small "Open in Studio" link for power users.
4. **Library card → guided run flow** — tapping a card walks the user through a tiny form (auto-generated from the entry-phase artifact's JSON Schema, but rendered as labeled inputs with examples and helper text).

**Studio side (priority within Studio):**

5. **Conversation + live skill-run inspector** (Studio-side conversation view) — same conversation as App, plus a right rail with the live skill-run mini-map: phase graph, current phase highlighted, control-IR ops fired, token/cost so far. This is the bridge between the two faces.
6. **Skill graph** — interactive node-and-edge canvas. Phases as nodes; allowed transitions as edges; sentinel `end` styled distinctly; sub-skills (`@name`) styled as nested. Hover = instructions; click = phase detail.
7. **Run timeline / event log** — vertical timeline with collapsible event groups, filter chips by event type, jump-to-phase markers. Debugging surface.
8. **Permissions table** — ops × rules, editable inline.

## 6. Translation glossary — engine vocabulary → App vocabulary

The App surface must never expose the left column. Use the right column.

| Engine term | What the user sees |
|---|---|
| Agent | Agent (or "your agent") — keep it; OpenClaw uses it too and users get it |
| Skill | A "thing your agent can do" — phrase as a verb in cards: "Research a topic" |
| Phase | (hidden) |
| Artifact | (hidden — render as a form field for entry, plain text/markdown for output) |
| Run | (hidden — surface as "Aria is working on it" / "Aria finished") |
| Event | (hidden — humanized into inline status messages) |
| `ask_user` Control IR | Inline soft question with chip suggestions |
| Validation error | "Hmm, something didn't look right — let me try again" + retry indicator |
| Permission prompt | "Aria wants to read `notes.txt`. Allow once / always / no" — plain language |
| Budget threshold | Soft "you've used 80% of today's budget" toast; hard stop says "let's pause for today or raise the cap" with a one-tap link to Studio |
| Sub-skill nested call | "Aria asked the writing helper to draft a section" |
| `loop_limit_exceeded` | "I'm having trouble making progress on this — want to give me more guidance?" |

## 7. States & edge cases

**App side:**

- **Streaming output** — soft fade-in of tokens; "thinking…" pill (✨ or `⟳`) while the model is working.
- **Background skill in progress** — collapsed status banner above the latest message ("Researching renewable energy — 3 sources found"), expandable to a humanized step list. Never expose phase names.
- **Mid-task question (`ask_user`)** — appears inline as a soft-tinted speech bubble from the agent with optional chip suggestions and a free-text fallback.
- **Permission prompt** — modal-lite (sheet from bottom on mobile, inline card on desktop), plain language, "Allow once / Always / No."
- **Budget warning** — non-blocking toast at 80%; blocking sheet at 100% with "raise the cap" link that opens Studio → Settings → Budget.
- **Sub-skill nesting** — "Aria asked the writing helper to draft a section" with a single-line nested indicator. Don't show depth numerically.
- **Crash recovery / restored sessions** — silent or a single subtle toast: "Picked up where you left off."
- **Empty states** — "Welcome — let's set up your first agent" with a single CTA. The "default" agent should auto-create with a friendly name (e.g., the user picks one) so the empty state is rare.

**Studio side:**

- **All of the above, exposed at engine level.** Same events, but rendered as `phase_started` / `validation_error` / `permission_denied` etc.
- **Replay mode** — step through a finished run's events; UI shows the LLM input/output exactly.
- **Validation diff** — when the LLM output didn't match schema, show a side-by-side: rejected output vs. expected schema.

## 8. Visual language

The two faces have **deliberately different aesthetics**. Treat them as siblings, not twins.

### App (friendly)

- **Vibe**: warm, light, breathing. Closer to **Claude.ai / OpenClaw / Notion / Linear's onboarding** than to Vercel/Cursor.
- **Color**: a warm primary (could be a coral / amber / soft teal — let the brand decide; OpenClaw's lobster red is one anchor reference). Light mode primary; dark mode also polished. State colors: warm yellow for soft asks, muted red for errors phrased gently, soft green for done.
- **Typography**: a humanist sans for chrome and reading (e.g., Inter / Geist / Söhne); generous line-height; large readable body. No monospace anywhere on this surface (no code, no IDs).
- **Density**: low. Big cards, generous padding. Mobile-friendly even though desktop-first.
- **Iconography**: rounded line icons. Maybe one playful animal-or-mascot motif per agent (avatar) — riffing on OpenClaw's lobster mascot. Avatars carry the warmth.
- **Voice**: first-name agents, present tense ("Aria is researching…"). No jargon. No exclamation marks. Warm but not childish.

### Studio (developer)

- **Vibe**: dev-tool dense. Dark mode primary. Linear / Vercel dashboard / Cursor / Temporal / LangSmith.
- **Color**: cool accent (teal / cyan); state colors used sparingly. Yellow = intervention, red = error, green = done.
- **Typography**: monospace for IDs, paths, JSON, event types; sans-serif (e.g., Inter) for chrome.
- **Density**: high. Tables, side rails, keyboard shortcut hints visible.
- **Iconography**: simple line icons. Glyphs `⟳ · ✗` carry over from the TUI.
- **Voice**: neutral, technical. Surface engine terms verbatim.

### Shared

- The same **agent identity** (name, color, avatar) is used in both faces, so a developer who flips to App sees the same agent they were debugging.
- A small "App ↔ Studio" toggle in the top-right of every screen. Cmd/Ctrl+E or similar to flip fast.

## 9. Interaction principles

- **App is finger-and-voice friendly**, Studio is keyboard-first.
- **Never leak engine vocabulary into App.** A linter rule for the design: if a copy block contains "phase," "artifact," "control_ir," "event," "schema," "validation" — it belongs in Studio.
- **Provenance is one click away.** App messages have a quiet "···" → "Open this run in Studio" link, so a curious user (or a dev colleague helping out) can drop into the trace.
- **Edits never destroy.** Phase / artifact / skill edits create a new version; old runs always replay against the version they were run with.
- **The agent's voice is consistent** between App and Studio — the model output is the same; only the surrounding chrome differs.

## 10. Out of scope (first design pass)

- Mobile-only layout (App should be responsive, but desktop-first).
- A built-in skill marketplace.
- Real-time multi-user collaboration.
- In-Studio visual phase-graph **editing** (read-only graph in v1; editing stays in `.md` / `.yaml` files).
- Voice input/output (mention as a future for the App side; design doesn't need to solve it now).

## 11. Suggested deliverables to ask Claude Design for

1. **App-side** hi-fi screens for Priority 1–4 in §5 (Today, Conversation, Agent gallery + profile sheet, Library card → guided run).
2. **Studio-side** hi-fi screens for Priority 5–8 (Conversation+inspector, Skill graph, Run timeline, Permissions table).
3. **Two design systems** that share tokens but diverge: an App theme (warm, breathing) and a Studio theme (dense, dark). One color palette + type scale + spacing scale per theme.
4. **Empty states** for both surfaces.
5. **The App ↔ Studio handoff moment** — one frame showing the toggle and how the same agent identity carries across.
6. **One hero screenshot** per face: App hero = a calm Today screen with a friendly agent card and a recap line; Studio hero = the conversation+inspector view with a live skill graph in the right rail.

---

### Appendix A — message kinds emitted to the chat outbox

`agent` (model reply), `status` (transient `⟳ ...`), `error` (`✗ ...`), `intervention` (yellow-bordered ask_user prompt), `trace` (dim `· ...` debug line), `skill_done` (green-bordered completion summary). Each has a `meta` dict carrying `skill_name` / `run_id` / `run_id_short`.

App surface translation: `agent` → normal message; `status` → "thinking…" pill; `error` → soft "hmm, something went off" recovery; `intervention` → inline soft question with chips; `trace` → hidden; `skill_done` → "Aria finished" line.

### Appendix B — event types (full list)

`chat_started`, `chat_stopped`, `user_message_received`, `user_intervention_received`, `context_built`, `llm_called`, `validation_error`, `normalization_error`, `control_ir_failed`, `control_ir_skipped`, `control_ir_validation_error`, `permission_denied`, `tool_executed` (op ∈ read_file / write_file / edit_file / delete_file / glob_files / grep), `mcp_called`, `mcp_completed`, `mcp_failed`, `shell_started`, `shell_timeout`, `web_search_started`, `web_search_completed`, `web_search_failed`, `web_fetch_started`, `run_skill_started`, `skill_run_spawned`, `skill_run_failed`, `skill_node_started`, `loop_limit_exceeded`, `compaction_check`, `compaction_failed`, `budget_reset`.

App surface only ever sees humanized renderings of these. Studio surface sees them verbatim.

### Appendix C — CLI subcommands (today's surface area, all need a Studio equivalent)

`reyn init`, `reyn run <skill> <input>`, `reyn chat [agent]`, `reyn skills [name]`, `reyn eval <eval.md>`, `reyn lint <skill>`, `reyn events <log.jsonl>`, `reyn config {show, fields, get, set}`, `reyn agent {list, new, rm, show}`, `reyn topology {list, new, show, rm, add-member, rm-member}`, `reyn memory ...`, `reyn permissions ...`.

App surface only needs: pick agent, chat, kick off a Library card. Everything else is Studio.
