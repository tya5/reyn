# CLI Redesign — Design Doc (PR-cli-2)

> **Purpose**: this is the design specification for replacing Reyn's current
> prompt_toolkit-based REPL with a [Textual](https://textual.textualize.io/)
> TUI as the default interactive surface. It is the output of design-side
> brainstorming and serves as the implementation contract for PR-cli-2.
>
> **Prerequisites**: PR-cli-1 (slash prefix `:` → `/` migration + drop of
> `reporters/rich.py`) must land first. This doc assumes that state.

---

## Goals

1. **Default UX = Textual TUI**, comparable in polish to Claude Code, codex
   CLI, gemini-cli. Status line + scrollable conversation pane + bottom
   input box, with markdown rendering, streaming tokens, slash-command
   autocomplete.
2. **`--cui` flag = plain stdout debug mode**. No cursor manipulation, no
   color, no status updates — purely for piping output to logs or
   debugging when the TUI itself misbehaves.
3. **`/command` palette** with Tab completion sourced from the actual
   registered commands (no static list).
4. **Streaming tokens render in-place** as the model produces them; the
   conversation pane scrolls as new content arrives.
5. **Interventions (`ask_user`)** and **permission prompts** integrate
   into the TUI as inline soft questions with chip suggestions, not
   modal dialogs.
6. **No engine modification.** The TUI is a renderer + input collector;
   it sits on top of the existing `ChatSession.outbox` / `submit_user_text`
   API.

## Non-goals

- **Mouse support.** Keyboard-first. Mouse is fine if Textual provides
  it for free, but it's not a target.
- **Custom theming via config.** Ship one well-tuned theme; user
  customisation is out of scope for v1.
- **Multi-pane / split-view** (Studio-style inspector). Reyn's web UI
  has Studio for that. The TUI shows one conversation at a time; agent
  switching is via `/attach` (existing slash command).
- **Mobile / SSH-into-phone friendliness.** Standard 80×24 minimum,
  optimised for 100×30+ desktop terminals.

---

## Reference UX (Claude Code anatomy)

The user-stated reference is Claude Code. Its observable shape:

```
┌─ Reyn ──────────────────────────────────────── alice · gemini-2.5-flash · $0.42 today ─┐
│                                                                                       │
│  Tetsuya  >  Find me a paper on solid-state batteries from 2025                       │
│                                                                                       │
│  Aria     I'll search for recent papers on solid-state batteries from 2025.           │
│           Looking through the literature now...                                       │
│                                                                                       │
│           [⟳ searching arxiv]                                                         │
│           [⟳ found 14 results · filtering by date]                                    │
│                                                                                       │
│           Here are three notable papers from 2025:                                    │
│                                                                                       │
│           1. **Sulfide-based cathode materials …                                      │
│              ...                                                                      │
│                                                                                       │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ > █                                                                                   │
│   /agents  /attach  /cost  /budget  /skills      Esc·Esc rewind  Ctrl·C cancel        │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

Key elements lifted from Claude Code:

- **Status line at top**: agent name · model · token + cost today
- **Conversation pane** scrollable, distinct sender prefixes, markdown
  rendered inline, status pills (`⟳ searching`) for in-progress activity,
  blockquote-style for thinking/trace
- **Input area at bottom**, multi-line, Enter sends, Shift+Enter newline
- **Slash command hints** in a footer line under the input
- **Esc·Esc rewind** indicator (we don't ship rewind in v1; the slot is
  reserved for future use)
- **Ctrl·C cancel** for the in-flight model call

Differences for Reyn:

- Reyn surfaces multi-agent (we attach to one but can switch). The
  status line top-right shows the **active agent identity**, not just
  model name.
- Reyn has an event audit log distinct from chat. We surface a
  "show details" affordance (`/inspect <run_id>` or similar) but the
  default conversation pane stays clean.
- Reyn's intervention (`ask_user`) gets a **chip row inline** — see
  § Intervention UI below.

---

## Layout (Textual widgets)

```
App
└── Screen (root)
    ├── Header           ← status line (agent · model · budget today)
    ├── ConversationView ← RichLog with markdown + custom kind dispatch
    └── InputBar
        ├── Input        ← TextArea or Input (multi-line if Textual supports)
        └── Footer       ← slash hints + keybind hints
```

**Widget choices**:

- **`textual.app.App`** — root.
- **`textual.widgets.Header`** — built-in status bar. Subclass to add
  agent / model / budget on the right side.
- **`textual.widgets.RichLog`** for the conversation pane. Each
  outbox message becomes a renderable; `RichLog.write(...)` appends
  with auto-scroll. Streaming uses `RichLog.write` per token batch
  (e.g., every 5–10 tokens or on whitespace boundaries to avoid
  rendering thrash).
  - Alternative: a `VerticalScroll` containing per-message `Static`
    widgets. Use this if `RichLog` markdown support is insufficient.
    Decide during impl POC; default is `RichLog`.
- **`textual.widgets.TextArea`** for the input (multi-line, paste-aware,
  syntax-aware off). Falls back to `Input` if `TextArea` is too heavy
  for our use.
- **Custom `Static` rows** for slash command hints below the input.

**Layout CSS** (Textual TCSS):

```css
Screen {
    layout: vertical;
}
Header {
    dock: top;
    height: 1;
}
ConversationView {
    height: 1fr;
    border: tall $primary-background-darken-2;
    padding: 1 2;
}
InputBar {
    dock: bottom;
    height: auto;
    border-top: tall $primary-background-darken-2;
}
InputBar > Input {
    margin: 0 2;
}
InputBar > #hints {
    height: 1;
    margin: 0 2;
    color: $text-muted;
}
```

---

## Keybindings

| Binding | Action |
|---|---|
| **Enter** | Send message (when input is non-empty and not in history-search mode) |
| **Shift+Enter** | Newline (multi-line input) |
| **Tab** | Open `/command` palette (autocomplete) |
| **Esc** | Close palette; if no palette, no-op |
| **Esc·Esc** | (reserved for rewind, no-op in v1) |
| **Ctrl+C** | Cancel in-flight model call (existing `cancel` semantics); double Ctrl+C to quit |
| **Ctrl+L** | Clear conversation pane (does not affect engine state) |
| **Ctrl+D** | Quit (with confirm prompt if mid-conversation) |
| **PageUp / PageDown** | Scroll conversation pane |
| **Up / Down (in input)** | History navigation when input is empty/cursor at start; otherwise cursor move |
| **Ctrl+R** | Reverse search through input history (prompt_toolkit-style) |

**Notes**:

- Esc·Esc rewind is reserved; binding registered as no-op so users
  who muscle-memory it from Claude Code don't get an error. Future
  PR may implement actual rewind via `EventStore` replay.
- We do NOT bind Cmd+K or Cmd+/ — those are GUI conventions and
  Textual handles platform key prefixes inconsistently across
  terminals. Stick to Ctrl-prefixed keys.

---

## Slash command palette

Trigger: `Tab` while input has cursor.

Behaviour:

1. If input starts with `/`, autocomplete the literal command from
   the registered set (e.g., `/a` → suggests `/agents`, `/attach`).
2. If input is empty, open a popup listing all registered commands
   with one-line descriptions.
3. Arrow keys navigate, Enter selects, Esc closes.

Source of truth for the command list: a registry in
`src/reyn/chat/slash.py` (new file) that decouples command names
from `session.py`'s 2058-line god module. Each command is a tiny
descriptor:

```python
@dataclass
class SlashCommand:
    name: str           # "agents" (without /)
    summary: str        # "List or switch agents"
    handler: Callable   # async (session, args) -> outbox messages
    completer: Callable | None = None  # optional dynamic completion (e.g., agent names for /attach)
```

The registry is populated by importing each slash module
(`slash/agents.py`, `slash/cost.py`, …). `session.py` no longer hard-
codes command parsing; it delegates to the registry.

**Note**: the slash module split is a **side-effect refactor** of
PR-cli-2. We touch `session.py`'s slash parsing anyway; pulling
commands into modules makes the TUI's command palette source clean.
This is in scope for PR-cli-2.

---

## Streaming token rendering

The existing engine produces tokens via `ChatSession.outbox` as
`OutboxMessage(kind="agent", text=...)`. For streaming, the engine
emits multiple messages incrementally (or one message with chunked
text — TBD by reading existing code).

**TUI handling**:

1. Open a new `[message id]` row when a new "agent" message arrives.
2. Append text to that row's renderable as more tokens arrive.
3. Coalesce by **time-window** (e.g., 16 ms) rather than per token,
   to avoid Textual re-render thrash.
4. When a `__end__` or new `kind` arrives, the streaming row is
   "sealed" and a new row starts.

Implementation: a `StreamingRow` widget that exposes `append(text)`
and triggers refresh. Internally a `Static` with a `Text` renderable.

If `RichLog` is sufficient, we can use its `write` method per chunk
and rely on terminal redraw. POC during impl will decide.

---

## Status line content

Top-right of `Header`:

```
alice · gemini-2.5-flash · 12,345 / 100,000 today · $0.42 / $5.00
```

Sources:

- **Agent name** = `AgentRegistry.attached_name`
- **Model** = `session.config.cost.default_model` (or whatever the
  current resolved model is when active)
- **Budget today** = `BudgetTracker.snapshot()["daily_tokens"]` /
  `daily_tokens_cap` and `daily_cost_usd` / `daily_cost_usd_cap`
- **Live update**: subscribe to a "status changed" event (or poll
  every 1 s; TBD). The simplest is **post-message refresh**: after
  every model call completes, fetch `BudgetTracker.snapshot()` and
  update the header.

If budget caps aren't configured (`unlimited`), display just the
absolute counters: `12,345 today · $0.42`.

---

## Intervention UI

When the engine emits `kind="intervention"`, render a soft-tinted
box with chips:

```
┌─ Aria asks ─────────────────────────────────────────────────────┐
│ Should I read notes.txt?                                       │
│                                                                │
│  [ Yes, once ]   [ Yes, always ]   [ No ]  ◯ free response …  │
└─────────────────────────────────────────────────────────────────┘
```

Implementation:

- A custom `Intervention` widget that mounts inline in the
  conversation pane (not a modal).
- Chips are buttons; selecting one calls
  `session.submit_intervention_response(choice_id=...)`.
- Free-text fallback expands to an `Input` overlay for typing a
  response, submitted via the same API.

The existing intervention bus (PR6 / PR7 / PR8) already routes the
prompt and answer; the TUI is just the renderer.

---

## Permission prompts

Same shape as interventions but distinct kind on the outbox
(`kind="error"` for denied, `kind="intervention"` for prompt).
Reuse the `Intervention` widget with chip set:

```
[ Allow once ]   [ Always ]   [ Deny ]
```

---

## ChatSession integration

The TUI app does not modify `ChatSession`. It interacts via:

**Engine → TUI** (output):
- Subscribe to `session.outbox` (asyncio queue): `while True: msg = await session.outbox.get(); render(msg)`.
- Or, given multi-agent (`AgentRegistry`), subscribe to
  `registry.repl_outbox` which already merges across agents.

**TUI → Engine** (input):
- User text → `session.submit_user_text(text)` (or, in multi-agent,
  `registry.attached_session.submit_user_text(text)`).
- Slash command → dispatch via `slash.registry[name].handler(session, args)`.
  The handler uses existing internal APIs.
- Intervention answer → `session.submit_intervention_response(...)` (the
  existing `ChatSession` API; verify exact method during impl).

**Async model**: Textual is async-first. The TUI's main loop runs
inside Textual's event loop; we spawn background tasks for
`outbox.get()` and update widgets via `app.call_from_thread` if
needed (Textual handles asyncio natively, so probably no thread
juggling).

---

## Theme / brand

One built-in theme, no user customisation in v1. Direction:

- **Background**: terminal default (don't fight the user's terminal
  bg).
- **Accent**: warm coral-ish (#C8553D, matching coral design's App
  primary).
- **Conversation prefixes**:
  - User: bold, terminal-default fg
  - Agent: bold, accent
  - Status (`⟳ ...`): dim italic, accent
  - Error (`✗ ...`): bold red
  - Trace (`· ...`): dim, terminal-default fg
  - Skill done (`✓ ...`): bold green
- **Borders / dividers**: `tall` border style in muted tone.

Textual's `tcss` (CSS-like) syntax handles all of this in one file.

---

## Edge cases

- **Terminal resize**: Textual handles natively. RichLog reflows.
- **Paste with newlines**: prompt for "send as multi-line message?
  Enter to send, Esc to edit". Default: paste as-is, user hits
  Enter to send.
- **Long messages**: ConversationView is scrollable. New messages
  auto-scroll to bottom unless user has scrolled up (sticky
  scroll-pin behaviour).
- **Unicode / CJK**: Textual handles wide chars. Verify via JP test
  fixture.
- **Fast streaming**: time-window coalescing (above) prevents render
  thrash. Verify with a 1000-token streaming benchmark.
- **Crash recovery**: TUI process exit doesn't lose state (engine's
  `ChatSession` persists via PR21 WAL). On restart, reload the last
  conversation from `EventStore` if possible (TBD; minor feature).
- **Screen reader**: out of scope for v1, but Textual is partially
  ARIA-compatible.

---

## Migration of existing slash commands

Current commands in `session.py` (after PR-cli-1's `/` rename):
`/list`, `/cancel`, `/answer`, `/cost`, `/budget`, `/agents`,
`/attach`, `/skills`. Each migrates to a module under
`src/reyn/chat/slash/`:

```
src/reyn/chat/slash/
├── __init__.py    ← registry assembled here
├── agents.py      ← /agents, /attach
├── budget.py      ← /cost, /budget
├── chat.py        ← /list, /cancel, /answer
└── skills.py      ← /skills
```

The TUI command palette autocompletes from this registry.

The `session.py` god module shrinks (slash parsing extracted). This
is a welcome side effect.

---

## Implementation plan (PR-cli-2)

Estimated file changes (rough):

1. **New**: `src/reyn/chat/tui/` package
   - `app.py` — `ReynTUIApp(textual.app.App)`
   - `widgets/header.py` — status line widget
   - `widgets/conversation.py` — conversation pane widget
   - `widgets/input_bar.py` — input + footer hints
   - `widgets/intervention.py` — intervention/permission chip widget
   - `widgets/streaming_row.py` — streaming token row
   - `theme.tcss` — Textual CSS
2. **New**: `src/reyn/chat/slash/` package (registry-based slash commands)
3. **Modified**: `src/reyn/cli/commands/chat.py`
   - Default = TUI (Textual app).
   - `--cui` flag = plain console.py path.
   - `--rich` flag was removed in PR-cli-1; `--cui` is the only
     reporter selector.
4. **Modified**: `src/reyn/chat/session.py`
   - Remove inline slash parsing (delegated to `slash` registry).
   - Keep all engine logic.
5. **Modified**: `pyproject.toml`
   - Add `textual>=0.50` (or latest stable) as core dep.
   - The previous `[rich]` extra was removed in PR-cli-1; `rich` is
     now a transitive dep of `textual`.
6. **Tests**: `tests/cli/test_tui_smoke.py` (Textual has a `Pilot`
   testing API for headless TUI testing).

Estimated diff size: ~1500 lines new, ~500 lines deleted (mostly
session.py slash parsing extraction).

---

## Phasing within PR-cli-2

Sub-phases for PR review (sequential commits in a single PR):

1. **Slash registry refactor** — extract slash commands into
   `chat/slash/` modules, no behaviour change. Tests updated.
2. **Textual TUI scaffold** — `chat/tui/app.py` + Header +
   ConversationView + InputBar, no streaming, no intervention. Hello
   world that displays one message.
3. **ChatSession wiring** — outbox subscription, submit_user_text,
   slash dispatch via registry. No streaming yet.
4. **Streaming token rendering** — chunk coalescing, scroll-pin.
5. **Intervention + permission widgets**.
6. **Status line live updates** — budget snapshot per model call.
7. **CLI flag wiring** — default = TUI, `--cui` = plain console.
8. **Tests** — Pilot-based smoke tests.

Each phase is a clean commit that compiles and runs (subject to
the previous phases). PR review reads commit-by-commit.

---

## Out of scope (deferred)

- **Esc·Esc rewind** (slot reserved, no implementation).
- **Multi-pane Studio-like inspector** in TUI (Reyn's web UI has it).
- **User-configurable theme** (one bundled theme only).
- **Mouse / touchpad polish** (keyboard-first; mouse "works if
  Textual supplies it", we don't tune it).
- **Plugin slash commands** (registry is closed for v1; users can
  edit `chat/slash/` source if they want to extend).
- **Persistent scrollback history across sessions** (refresh starts
  with empty pane; engine state persists via PR21).
- **Screen reader optimisation** (Textual's defaults; no extra
  ARIA work).

---

## Open questions for impl phase

These are things we'll likely discover during phase 2 (TUI scaffold)
and resolve in phase 3 or later:

- **`RichLog` vs custom `VerticalScroll`** — POC both for streaming;
  pick whichever survives 1000-token-fast-stream benchmark.
- **`TextArea` vs `Input`** — `TextArea` for multi-line is nicer;
  `Input` is simpler. Decide after seeing keybinding interactions.
- **Outbox subscription model** — single coroutine pulling from
  queue, or callback registration. Textual prefers the former.
- **Resize behaviour during streaming** — do we pause stream until
  resize completes, or accept transient render glitches?
- **Cancel UX** — Ctrl+C during streaming: cancel the model call
  (existing semantics) or abort entire run (kill all skills)?
  Current behaviour is the former; preserve.

---

## See also

- [docs/deep-dives/spec/openui/](../deep-dives/spec/openui/) — separate web UI work, unrelated to
  CLI but parallel infrastructure
- [feedback_model_delegation_routing.md](in memory) — Sonnet/Opus
  routing for implementation phases
- Textual docs: https://textual.textualize.io/
- prompt_toolkit (current REPL): kept only for the `--cui` path
  inputs (debug mode), since plain CUI still wants line editing.
