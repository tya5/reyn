# Present layer — user-facing presentation of bulk data without LLM token round-trip

**Author:** architect · **Status:** DRAFT (design proposal — awaiting owner review; no
implementation dispatched) · **Date:** 2026-07-08

## Problem

When an agent obtains external data via a tool and shows it to the user, the data today
always round-trips through LLM tokens. Splitting the cost into axes:

| Axis | Cost | Status |
|---|---|---|
| **A. Ingestion (input)** | tool result entering LLM context | **Solved** by the offload mechanism + tool-result-schema-redesign arc (`docs/deep-dives/proposals/0053-tool-result-schema-redesign.md`, IMPLEMENTED): data lands in a ref file, the LLM sees schema + preview, reads back on demand via `file__read` |
| **B. Reproduction (output)** | the LLM re-types the data as output tokens to show the user | **Unsolved — target of this proposal** |
| **C. Fidelity loss** | to fit output, the LLM summarizes/truncates; the user loses data | **Unsolved — same mechanism solves it** |

The offloaded ref file is *already* "data file + handle". What is missing is a primitive
that routes that handle plus a display template to the user-facing surface directly, so
the bulk bytes never pass through LLM output.

Two existing reyn mechanisms are each half of the answer, currently unconnected:

1. **Offload** (redesign arc, landed) — the offload refs are the data handles.
   Structured stream: an inline `structured` frontmatter field, or when offloaded
   `structured: offloaded` + `structured_ref` (a `file__read`-able path) +
   `structured_preview`. Text stream: the body text, or when offloaded a plain-text note
   `...[truncated: <N> chars total — full body: file__read(path="<ref>")]...` (no
   dedicated frontmatter field for the text ref). Saves input tokens only; presentation
   still goes through output tokens.
2. **Tool-result viewer registry + LLM template** (FP-0051,
   `docs/deep-dives/proposals/0051-tool-result-viewer-registry-llm-template.md`) —
   content-type → viewer → Rich renderable, with a safety-fenced LLM-generated template
   fallback. **Status (confirmed 2026-07-08, docs-maintainer exhaustive grep):**
   FP-0051 is **superseded** — the Textual TUI (right panel included) and the entire
   viewer-registry module (`register_viewer` / `render_tool_result` / content-type
   dispatch) were **deleted with no relocation**. The current terminal UI is the
   **inline-CUI** (`src/reyn/interfaces/inline/`), whose only tool-result rendering is
   `summarize_tool_result` — a **single-line summary with no content-type dispatch and
   no rich rendering**. FP-0051's *design* (predicate registry, template fence, escape
   idioms, fallback chain) remains the lineage this proposal draws on, but there is **no
   live rendering surface to generalize — it is rebuilt fresh.**

**This sharpens the problem.** Today bulk tool data reaches the user via exactly two
paths: a `summarize_tool_result` one-liner (maximal fidelity loss) or full LLM output
reproduction (maximal token cost). Axis B and C are not merely suboptimal — the rich
middle ground does not currently exist at all.

This proposal introduces that middle ground: a `present` op (Control IR) + a
surface-agnostic presentation model, building rich, cross-surface presentation fresh on
the inline-CUI, along the FP-0051 design lineage.

## Standards landscape (2026) and the architectural choice

The agent-UI presentation problem has crystallized into a protocol layer industry-wide.
Two fundamental approaches:

| | **MCP Apps** (Anthropic + OpenAI + MCP-UI, 2026-01) | **A2UI v1.0** (Google, Candidate) |
|---|---|---|
| Mechanism | tool declares `ui://` resource → host renders bundled HTML/JS in sandboxed iframe; tool-result data flows to the iframe directly, bypassing LLM tokens | agent emits a **declarative component blueprint** (no code); data bound by **JSON Pointer (RFC 6901)** paths; `updateComponents` (structure) and `updateDataModel` (data) are separate messages |
| Security | executable-but-sandboxed (iframe + JSON-RPC audit) | **non-executable by construction** — catalog components + path bindings only |
| Terminal-renderable | ❌ impossible (no iframe in a terminal) | ✅ abstract components map to any surface |

Related: AG-UI (CopilotKit) is the transport pipe (typed SSE events), complementary to
either. Vercel Generative UI routes `toolName → pre-built component`; the model never
generates UI code.

**Decision (owner-approved): approach = declarative blueprint, architecture = hub-and-spoke
"option C".** The internal model is reyn-native but **structurally isomorphic to A2UI**
(declarative component tree · path-based data binding · vetted non-executable catalog).
Rationale:

1. The terminal (inline-CUI) is reyn's first-class surface; the iframe-webapp approach
   cannot reach it.
2. "Non-executable by construction" is the same philosophy as reyn's structural
   write-gate: safety from the primitive's *shape*, not from policy layered on top.
   FP-0051's template fence (LLM picks labels + key names only, allowlist + double
   escape) is already this shape.
3. Isomorphism keeps future wire adapters thin — external A2UI / MCP Apps emit
   (`reyn mcp serve` / A2A producer side) and ingest (reyn-as-MCP-host consumer side)
   become boundary adapters — while keeping internal contracts sovereign and insulated
   from a still-Candidate external spec.

```
   internal surfaces            hub                       boundary adapters (future)
  inline-CUI / web / A2A ◀── present op + declarative ──▶  A2UI / MCP-Apps emit (producer)
  (know nothing of A2UI)      model + renderers        ──▶  A2UI / MCP-UI ingest (consumer)
```

Wire-level protocol conformance is explicitly deferred until the standards stabilize;
only the internal *shape* is aligned now.

## Design principles (invariants)

1. **LLM sees shape, not content; the user sees everything.** The LLM works from the
   offload schema + preview and binds paths; the renderer joins the template against the
   full data the LLM never ingested. This asymmetry is the designed contract, not a
   defect.
2. **Display is free; computation costs.** Presenting N rows costs ~0 LLM tokens. The
   moment the agent must *transform* the data (sort, filter, answer questions about it),
   it pays to read the ref. Documented so the asymmetry is never mistaken for a bug.
3. **Declarative, never executable.** Templates are catalog components + path bindings.
   No markup, no HTML, no code ever crosses from LLM to renderer.
4. **Degrade, don't fail; degradation is audited.** Binding misses soft-skip per
   binding; drops are recorded in the audit event; full miss falls back to the generic
   viewer. A broken template can never lose the user's access to the data (the ref
   remains readable).
5. **Audit-first.** Every presentation emits a P6 event carrying refs and stats — never
   content bytes (data is already durable in the ref file; events stay light).

## Design

### 1. `present` op (Control IR)

```yaml
present:
  data_ref: <path>            # XOR data_inline; any zone-readable file
  data_inline: <small dict>   # optional convenience for small already-in-context data
  template: <registered name> # XOR blueprint
  blueprint: <inline declarative component tree with path bindings>
```

- **Tier 0** (`ask_user`'s sibling): presenting to the user is not an exfiltration
  channel — the user is the trust root. The only gate: **`data_ref` read authority is
  resolved identically to `file.read`** — `present` can never read more than the agent's
  file ops can. Not limited to tool-result refs: artifacts, agent-written files, any
  zone-readable path qualifies (works even with `offload.enabled: false`).
- **Fire-and-continue**: unlike `ask_user`, does not pause the run.
- **Op result (ack)** — the LLM's only feedback, deliberately compact:
  ```yaml
  ok: true
  bindings_resolved: 3
  bindings_dropped: ["/results/0/author"]
  rows: 500
  ```
  This closes the self-correction loop for blind presentation: the LLM detects a
  mismatched template for tens of tokens and can re-present with corrected paths.
- New op kind ⇒ **`OP_KIND_MODEL_MAP` + `docs/reference/runtime/control-ir.md` section
  in the same PR** (CLAUDE.md hard rule #1983).

### 2. Declarative UI model (v1 catalog — display-only)

Component catalog, all read-only:

| Component | Binding slots |
|---|---|
| `text` / `markdown` | `text` (bind or literal) |
| `code` | `text`, `language?` |
| `diff` | `text` |
| `keyvalue` (card) | `rows: [{label, value: bind}]` |
| `table` | `rows: <bind → array>`, `columns: [{header, path (row-relative)}]` |
| `list` | `items: <bind → array>`, optional per-item template |
| `image` | routes to the existing multimodal delivery path |

- Bindings are **JSON Pointer (RFC 6901)**; table/list column paths resolve relative to
  the iterated row. Structured refs → pointer bindings; plain-text refs → whole-body
  binding into text-family components (`text`/`code`/`diff`/`markdown`) only.
- **v1 is display-only: no interactive components** (no buttons, no forms). UI-spoofing
  (fake consent dialogs etc.) only becomes harmful with interactivity; MCP Apps spends
  its sandbox complexity there. reyn avoids the class structurally. Interactivity is v2,
  and must route through the existing intervention bus (the consent path) when it comes.
  **← the one headline owner decision this doc requests.**

### 3. Template sources — 4-stage fallback

```
registered template (operator-owned) → inline blueprint (LLM-authored, catalog-constrained)
  → content-type default viewer (FP-0051 registry) → generic YAML/text
```

- Named templates live in **`.reyn/config/presentations.yaml`** — same registration +
  hot-reload-seam pattern as `skills.entries` / `pipelines.entries`. Registering named
  templates is an **operator/config action**; the LLM authors inline blueprints only
  (write-gate culture).
- Inline blueprints are structurally gated at op validation: catalog components only,
  bindings are path expressions only, labels escaped at parse (FP-0051's fence,
  generalized).

### 4. Binding semantics

- Path hit → bind (escape at render, per surface).
- Path miss → **soft-skip that binding/component**, record in `bindings_dropped`.
- Type mismatch → renderer coercion rules (scalar into table binding → 1 row; etc.).
- All bindings miss → fall back to generic viewer; never a hard failure.

### 5. Presentation-guard (output seam)

Mirror of the input-side content-guard, at the output boundary:

- Threat scan + neutralization of rendered leaf strings: terminal escape sequences,
  Rich markup, HTML — per target surface. Runs **unconditionally**, including (and
  especially) for never-ingested data.
- **Per-binding size caps** — prevents `/` (root) bound into a `text` component from
  dumping an entire file.
- **Default output cap (present-specific — not a scrollback pager).** The inline-CUI
  convention (confirmed with tui-coder) is that conversation output flows freely into
  terminal scrollback *uncapped*, because normal output is already bounded by LLM
  *output tokens*. `present` is unbounded by construction (that is the whole point), so
  it must carry its **own** default cap: render head-N rows/lines + a
  `…N more — full data: <ref>` tail. **Cap before render, not after** (tui-coder review) —
  for `code`/`diff`, Rich syntax highlighting is costly, so truncate the source to the
  row/line budget first and highlight only the survivors. There is no pager in the
  inline-CUI; the **ref is the full-fidelity escape hatch** (re-present with a
  filter/higher cap, or `file__read`).
  This bound is orthogonal to the inline-CUI's live-region caps
  (`_ABOVE_REGION_MAX_HEIGHT` / `_MENU_REGION_MAX_HEIGHT` = 12), which govern persistent
  UI regions, not one-shot conversation output.

### 6. Renderers and surfaces

- `PresentationRenderer` protocol — **built fresh** (the FP-0051 registry was deleted,
  not relocated; its design is the lineage, not a base to extend). Terminal (inline-CUI)
  = **Rich** (confirmed with tui-coder); web = native components; A2A = structured
  message part.
- **Terminal note (confirmed with tui-coder 2026-07-08).** The terminal UI is the
  inline-CUI (`src/reyn/interfaces/inline/`, prompt_toolkit + Rich) — the Textual TUI and
  right panel were deleted in `eff08169`; there is no side panel (the model is elastic
  Regions above / below the input bar). Its only tool-result rendering today is
  `summarize_tool_result` (a one-liner). `present` renders as a **one-shot inline block
  in the conversation scrollback**, riding the existing `repl/renderer.py` pattern: Rich
  `Console` → `StringIO` → `prompt_toolkit.run_in_terminal()` print. This axis is
  separate from the live-region line caps; the present-specific default output cap (§ 5)
  applies instead.
- **Explicit render width (tui-coder review).** Rich's `Console` cannot auto-detect
  terminal width when writing to a `StringIO`; it silently falls back to 80 columns — a
  latent bug already in `repl/renderer.py`'s two existing render sites, and more visible
  for `present` tables. The renderer must read `get_app().output.get_size().columns` and
  pass it explicitly to `Console(width=...)` per render. (Fixing the two existing sites
  is a worthwhile fast-follow, tracked separately — not in this proposal's scope.)
- **Remote surfaces (web/A2A)**: remote clients cannot read local refs. Data delivery is
  the surface adapter's responsibility: materialize via the gateway (size-capped inline
  embed or a served endpoint). v1 ships the terminal (inline-CUI) surface only; the hub
  model records this contract
  now so remote phases don't warp the core.

### 7. Audit — `presented` event (P6)

```yaml
presented:
  data_ref: <path>            # or inline-data marker
  template: <name | inline blueprint hash>
  surface: [inline-cui]
  ingested: none | partial | full   # OS-COMPUTED, not LLM-self-reported
  bindings_resolved: 3
  bindings_dropped: ["/results/0/author"]
```

**Blind-routing is not a permission mode — it is an audit annotation.** Whether the LLM
read the data before presenting is a fact the OS can compute (result was inline, or a
`file__read(ref)` appears earlier in the session). No config gate in v1; the guard runs
regardless; audit records the fact. (The standards world routes blind as the norm; reyn's
differentiator is making blindness *auditable*, not forbidding it.)

### 8. Lifetime, replay, rewind

- `present` does **not** pin `data_ref` into any retention window. Refs keep their
  existing lifecycle class.
- Replay/rewind re-renders best-effort from the `presented` event; a GC'd ref renders as
  an expiry placeholder pointing at the audit event. Presentation is a cache; the event
  is the truth. (No WAL-derived recovery state is introduced ⇒ the recovery-feature PR
  gate does not apply.)
- Conversation history never contains the presented bytes ⇒ nothing new for compaction.

## Relationship to the tool-result-schema-redesign arc

- **Consumer, not competitor**: `present` consumes the arc's offload refs — the
  `structured` / `structured_ref` / `structured_preview` frontmatter fields, plus the
  text-side ref carried inline in the truncation note — i.e. the text/structured stream
  split from the canonical mappers. Pipeline `ctx.<name>.text` / `ctx.<name>.structured`
  (PR-2) expose the same split (no size gate, full value).
- **Zero blocking asks — dependency fully met (arc CLOSED 2026-07-08)**: every arc PR is
  merged — PR-0 #2647, PR-1 #2648 (canonical mappers + legacy deletion), PR-2 #2652
  (pipeline ctx), PR-3 #2651 (`offload:` opt-out), PR-4 #2654 (design doc →
  `docs/deep-dives/proposals/0053-tool-result-schema-redesign.md`, IMPLEMENTED stamp).
  The offload output shape is landed and fixed; nothing gates this proposal.
- **Follow-up (additive, post-arc)**: enrich offload frontmatter with a shape summary
  (key names / types / array lengths) instead of only a head-N-chars preview — improves
  blind template authoring. To be filed as an issue after the arc lands.

## Out of scope (v1)

- Interactive components (v2; via intervention bus).
- Live-updating presentations (A2UI `updateDataModel` diffs) — presentations are
  immutable snapshots; revisit if dashboard-like demand appears (hooks/events could
  drive it).
- Pipelines (`tool:`/`agent:` step surfaces) and CodeAct observation path — same scoping
  as the redesign arc.
- Remote-surface materialization implementation (design contract recorded, § 6).
- Wire-level A2UI / MCP Apps adapters (emit + ingest) — enabled by isomorphism, deferred
  until the specs stabilize.

## Suggested PR sequencing (post owner-GO, post arc PR-1)

- **PR-A** — `present` op + declarative model + binding resolution + presentation-guard
  core (no UI yet; op returns ack against a null renderer). Includes `OP_KIND_MODEL_MAP`
  + control-ir.md section (hard rule), `presented` event type.
- **PR-B** — inline-CUI renderer: conversation inline block + `PresentationRenderer`
  protocol (FP-0051 design lineage), height caps + expand affordance.
- **PR-C** — `presentations.yaml` registration + hot-reload seam + the 4-stage fallback
  chain (built fresh — FP-0051's registry is deleted; §§ 3, 6).
- **PR-D** — replay/rewind placeholder rendering + docs (concepts page + reference).

## Test plan (per testing.ja.md tiers)

- **Tier 1 (contract)**: binding resolution (hit / miss soft-skip / coercion / row-relative
  paths); ack shape (drops reported); blueprint structural gate (non-catalog component
  rejected, non-path binding rejected); guard (escape survival asserted via literal
  bracket, per FP-0051 test idiom); `presented` event field presence incl. OS-computed
  `ingested`; read-authority equivalence (`present` denied ⇔ `file.read` denied).
- **Tier 2 (OS invariant)**: fire-and-continue (run not paused); no content bytes in
  event payloads; ref-expiry placeholder path.
- **No Tier-4 pins**: no exact-render/whitespace assertions; assert content presence and
  drop lists, not layout.

## Open questions (owner)

1. **Ratify: v1 catalog is display-only** (interactivity deferred to v2 via intervention
   bus). This is the single structural decision the rest of the design leans on.
2. Naming: `present` (op), `presentations.yaml` (registry) — bikeshed-level, defaults
   proposed here stand unless overridden.

## Sources

- MCP Apps announcement (MCP blog, 2026-01-26) — https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/
- A2UI v1.0 specification — https://a2ui.org/specification/v1.0-a2ui/
- Google Developers: Introducing A2UI — https://developers.googleblog.com/introducing-a2ui-an-open-project-for-agent-driven-interfaces/
- CopilotKit: The State of Agentic UI (AG-UI / MCP-UI / A2UI) — https://www.copilotkit.ai/blog/the-state-of-agentic-ui-comparing-ag-ui-mcp-ui-and-a2ui-protocols
- The New Stack: Agent UI Standards Multiply — https://thenewstack.io/agent-ui-standards-multiply-mcp-apps-and-googles-a2ui/
- Vercel AI SDK 3.0 Generative UI — https://vercel.com/blog/ai-sdk-3-generative-ui
- FP-0051 viewer registry (superseded — TUI + registry deleted 2026-07, design lineage only) — `docs/deep-dives/proposals/0051-tool-result-viewer-registry-llm-template.md`
- Tool-result schema redesign (IMPLEMENTED) — `docs/deep-dives/proposals/0053-tool-result-schema-redesign.md`
