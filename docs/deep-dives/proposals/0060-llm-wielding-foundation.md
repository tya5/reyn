---
status: proposal (idea-stage draft — owner review pending; NOT ratified)
author: architect
date: 2026-07-12
---

# 0060 — LLM-Wielding Foundation: making the agent actually use what it can build

## 0. Problem statement (owner framing)

The agent-runtime mechanisms are essentially complete: **hook-event, skills,
pipeline, MCP, present/render_template** are all LLM-self-authorable. Each part
is simple; composed, they should multiply the LLM's capability.

But **providing a part ≠ the LLM wielding it**. This is the capability-adoption
meta-version of "complete means reachable-for-purpose": every mechanism exists
and is individually reachable, yet nothing teaches the model *that* they exist,
*when* to pick which, *how* to compose them, or *how* to turn a successful
improvisation into a reusable asset.

The next arc is the foundation that closes this gap, using **SP (system
prompt), builtin skills/pipelines/mcp/present, and a deliberate discovery
policy** as the levers.

## 1. Grounded current state (all file:line-verified on main, 2026-07-12)

1. **SP** is a mature slot-injector (`router_system_prompt.py` +
   `router_frame.py` + `universal_slots.py`; static/dynamic split, ~60%
   cache-prefix). It has `## Capabilities (routing guide)` and
   `## Action categories` — but **no mechanism-selection guidance**
   (skill-vs-pipeline-vs-mcp-vs-hook), **no author-vs-reuse guidance**, and
   **hooks are entirely absent from SP text**.
2. **Builtins are empty across every part-type.** reyn ships zero
   skills/pipelines/MCP servers/present views by default (test fixtures,
   one cookbook example, one demo `hello.yaml` only). The "show" layer is a
   void.
3. **Discovery is split and partially non-scaling**: skills = full-menu SP
   prompt-stuff (`build_skills_slot`) that grows with every authored skill;
   pipeline/mcp = category one-liner + lazy `list_actions`; **hooks and
   present-views have no SP category line and composers no catalog
   surface**; no unified cross-part capability query exists.
4. **Four tool-use schemes** (`scheme.py:295` registry) present ONE shared
   action catalog through four different lenses:
   - `enumerate-all` (DEFAULT) — every action flattened into native
     `tools=`. Chosen as default (#1657) because wrapper indirection caused
     name-hallucination: non-hot-list tool-use went **30% → 100%** on
     flattening. Pull costs fidelity, not just initiative.
   - `universal-category` — `list_actions`/`describe_action`/`invoke_action`
     wrappers (lazy pull) + `search_actions` when embedding is configured.
   - `codeact` — the full catalog rendered as Python signatures in the SP;
     the model writes code; per-call OS gate.
   - `retrieval` — `search_actions`-only + RePresent loop (opt-in semantic).
5. **Embedding availability already auto-gates `search_actions`**
   (`is_search_available`, `universal_catalog.py:212`): configured → the tool
   appears in enumerate-all/universal-category; absent → hidden, with an
   enable-hint injected. The floor-plus-auto-enhancement pattern this
   proposal generalises **already exists in miniature**.
   - **Defect found while grounding**: the `retrieval` *scheme* is manual
     config, not embedding-gated — selecting it without embedding silently
     degrades to a base-tools-only dead session (empty search → terminal on
     first call, `retrieval.py:144-147`; no fallback). Fail-visible
     violation; independent fix (fail-loud at config load, or auto-fallback
     to `enumerate-all`).
6. **`judge_output` exists** (generic rubric scorer) but nothing wires
   author → evaluate → iterate for self-authored parts.
7. **present is the one axis with authoring power but no self-authorable
   persistent registry**: `presentations.yaml` is operator-only (no LLM
   install op, not SP-surfaced, view names not model-discoverable);
   `render_template` has no named store (inline or file-path only). For the
   model, output is purely inline-per-call — the only axis where a good
   composition cannot become a named, rediscoverable asset.
8. **Docs role split is real but unratified**: `reference/` = precise
   contract, `concepts/` = mental model — observed de facto via per-page
   `type:` frontmatter (the same subject, permissions, has both kinds), but
   the convention doc is still `status: proposal`
   (`docs-restructure-proposal.md`).
9. **feature-map has no "capability-discovery / agent-guidance" row** — the
   gap this proposal addresses is present in the map *by absence*.

## 2. Frame

### 2.1 Part × role matrix (not exclusive bins)

Parts play roles on the **input / workflow / output** axes; several parts play
more than one (`task` is excluded — deprecating):

| part | input | workflow | output |
|---|---|---|---|
| hook-event | ✓ (reactive ingress) | (trigger glue) | — |
| mcp | ✓ (pull external) | ✓ (mid-flow call) | ✓ (external write) |
| retrieval / semantic_search | ✓ (context) | — | — |
| skill | — | ✓ (instructions) | — |
| pipeline | — | ✓ (orchestration) | — |
| present / render_template | — | — | ✓ (operator-facing, zero-token) |

Teach by role: *need input → hook | mcp | retrieval; need processing → skill |
pipeline | mcp-step; need output → present | render | mcp-write.*

### 2.2 Push vs pull (the real axis behind "retrieval is opt-in")

- **push** (SP / flat `tools=`): always visible, guaranteed, expensive,
  non-scaling.
- **pull** (catalog / semantic search): scalable, cheap, but requires model
  *initiative* — and (per the #1657 datum) indirection costs *fidelity*.

Embedding is not guaranteed per-user, and the retrieval scheme is one of four.
**Therefore the foundation must not be founded on the semantic layer.** The
foundation = an always-on floor; semantic retrieval = an enhancement that
auto-lights-up when embedding is available (generalising the existing
`is_search_available` pattern).

### 2.3 Catalog as the scheme-independent SSoT

All four schemes are lenses over one action catalog. **Fix the catalog and the
fix radiates through every scheme automatically**; the `ToolUseScheme`
protocol is the per-scheme adapter seam. Scheme-independent prose (the routing
model) belongs in the OS-frame (`router_frame.py`), not scheme-owned slots.
*(Implementation verify: confirm which slots each of the four schemes actually
injects; codeact/retrieval build their own tool-use SP.)*

### 2.4 Three distinct discovery objects (self-review correction)

An earlier draft said "add hooks to the catalog" — a category error. Hooks are
not invocable actions; skills are instructions, not executables. Discovery
splits into three objects with different loci:

| discovery object | question answered | locus |
|---|---|---|
| **part-type routing** | "which mechanism should I use?" | SP routing model (push) |
| **instance discovery** | "what is already installed/registered?" | enumeration verbs in the catalog (`hooks_list`, view listing, skills listing) |
| **management verbs** | "how do I install/author one?" | install/add ops in the catalog (already mostly present; missing SP category lines) |

### 2.5 Discovery-locus table (who teaches what, where)

| info type | floor locus (always-on) | enhancement (embedding-gated) |
|---|---|---|
| routing model / axes / author-vs-reuse | SP OS-frame (compact, cache-static) | — |
| pull-triggering standing instruction | SP discovery-mandate (strengthen; currently weak-tier-gated) | — |
| "what exists" (instances, cross-part) | catalog enumeration verbs | `search_actions` (semantic) |
| "how to call X" | tool descriptions (`describe_action`) | — |
| "how to perform X" (procedures) | SKILL.md lazy read (L2) | — |
| "how to author a part" | **builtin worked-examples** (registered ⇒ always discoverable) | reference-docs corpus via `semantic_search` |
| amortization | authored part **registers into the catalog** | also lands in the semantic index |

### 2.6 LLM journeys (動線) — the design validated against concrete paths

The locus table (§2.5) is static; the design must also hold along the
model's actual step-sequences. Each journey step names the information
needed at that moment, the locus that serves it, and the drop-out risk
(every pull step is a place a weaker model falls off — the #1657 lesson).
These journeys ARE the evaluation scenarios of §5.

**J-A. Reuse (the most frequent path — must be near-zero friction)**
task → decompose by role (SP map) → "does an instance exist?" (instance
discovery: flat `tools=` under enumerate-all = 0 extra calls; enumeration
verb under other schemes = 1 call) → how to call it (`describe_action` /
tool desc) → invoke. *Risk: instance not surfaced → model re-authors a
duplicate → catalog junk (L3 pressure).*

**J-B. Author-new (no instance exists)**
task → SP map → instance discovery returns nothing relevant →
author-vs-reuse heuristic (SP) says author → pick part-type (SP decision
tree) → worked-example lookup (**builtin exemplar via catalog — floor**;
reference corpus via semantic_search — enhancement) → management verb
(install op) → verify it appears in the catalog → use it. *Risk: no
exemplar → malformed part + trial-and-error token burn; this step is why
F3 outranks E2.*

**J-C. Compose (the thesis path)**
reactive requirement → SP map decomposes input/workflow/output → through-
chain builtin (F3) as the reference composition → author/reuse each part →
wire (hook `on:` → pipeline → present) → test-fire → observe via audit
events. *Risk: no through-chain exemplar = the composition idea itself
never occurs to the model; this is the single highest-leverage builtin.*

**J-D. Promote (amortization, cross-session)**
inline composition succeeds → SP idiom nudge (L1) → optional judge_output
gate (L2) → install via existing op → **next session**: J-A finds it as an
instance. *Risk: friction at inline→install (file-write ergonomics, open
question) silently kills the loop; nothing amortizes.*

**J-E. Degraded floor (no embedding, weak tier)**
Every journey above must complete **without** `search_actions`/corpus:
J-A/B via flat tools= or enumeration verbs, J-B's exemplar via catalog-
discoverable builtins. *This is the definition of "floor".*

**J-F. Enhancement upgrade (embedding configured)**
same journeys, with pull steps shortened: instance discovery → semantic
`search_actions`; exemplar lookup → corpus retrieval at authoring time.
The upgrade changes **cost/hit-rate, never reachability**.

Journey friction budget: J-A must fit in ≤1 discovery step on every
scheme; J-B/C in ≤3. If a design choice adds a pull step to J-A, it is
wrong regardless of its elegance.

## 3. Proposed architecture

### F — the always-on floor (embedding-independent, holds across all 4 schemes)

- **F1. Catalog completion, structured by §2.4**: SP category lines +
  enumeration verbs for the reactivity axis (hooks/composers) and the
  presentation axis (views); role-structured category taxonomy.
  *Completeness gate*: a registry-derived CI check — every registered
  part-type has a category row; no curated subset (same shape as the
  `OP_KIND_MODEL_MAP` ↔ `control-ir.md` hard rule).
- **F2. SP routing model** in the OS-frame: the part×role map, a
  mechanism-selection decision tree (hooks made visible for the first time),
  author-vs-reuse heuristics, and authoring-quality one-liners
  (typed/permissioned/evaluate-before-promote). **Hard char budget**,
  cache-static placement; the map carries *model + decision rules only* —
  details stay pull-side.
- **F3. Builtin exemplar set** (the "show" layer): one canonical exemplar
  per axis + **one through-chain** (hook input → pipeline/skill processing →
  present output) that demonstrates the composition thesis end-to-end.
  Builtins use the established two-layer pattern (code-shipped builtin layer,
  operator/LLM extension layer) and **ship inert**: discoverable in the
  catalog, never auto-enabled (a builtin hook firing by default would be a
  surprise-execution surface). Candidate exemplars exercise reyn's own
  idioms: retrieve-then-synthesize (semantic_search), self-review step
  (judge_output), zero-token status card (present).
- **F4. `presentation_management__install_*`** — the LLM-authorable
  present-view registry op, mirroring skill/pipeline install (gated,
  threat-scanned, generation-recorded). Closes the output-axis asymmetry
  (§1.7) and lets output compositions become named, catalog-discoverable
  assets. `render_template` stays inline/file-ref (the named store on this
  axis is the view registry).

### E — the enhancement layer (auto-on when embedding is configured)

- **E1.** Keep/strengthen the existing `search_actions` auto-gate.
- **E2. Authoring corpus**: index reference-layer docs for
  `semantic_search`, membership **machine-derived from `type: reference`
  frontmatter** (the frontmatter is the registry; no curated list). Requires
  ratifying the reference-vs-concepts convention (promote the
  docs-restructure proposal or a small docs README). `concepts/` is the
  compression source for F2's SP model, not corpus material.
  **Blocker-class verify first**: are docs packaged into the wheel at all?
  If not: package them, or ship a distilled authoring-guide bundle instead.
- **E3.** Fix the `retrieval`-scheme silent degrade (§1.5 defect) —
  independent of this arc, file immediately.

### L — the closing loop (assetization, on top of the floor)

- **L1. Promotion as idiom, not mechanism**: a successful ad-hoc composition
  (inline pipeline, inline blueprint) is promoted by *calling the existing
  install ops* — no new op. SP teaches the idiom ("after a composition works,
  consider installing it"). Open question: ergonomics of inline→install
  (file-write friction).
- **L2. Eval-gated promotion**: `judge_output` as the promotion gate —
  a part earns catalog registration by passing a rubric.
- **L3. Catalog hygiene**: self-extension accumulates junk; unused parts
  decay discovery precision (attention dilution, not just tokens). P6 already
  records invocations → usage-informed pruning/archival:
  authored → used → promoted OR expired.

## 4. Cross-cutting notes

- **Security — self-influence loop**: LLM-authored descriptions re-enter the
  model's own future context via the catalog (SP menus, tools=, signatures).
  Existing install threat-scans mitigate; the surface should be named in the
  security review of each F/L PR, and description length/content constraints
  considered.
- **Present teaches the operator, not the model** (design trap, hit once in
  drafting): `present` renders to the operator UI at zero token cost — the
  model never sees it. Any "orientation card" builtin is an *operator
  legibility* feature; the model's orientation lives in the SP only.
- **Per-agent capability filtering**: the SP routing model must degrade
  gracefully when a capability profile denies a part-type (don't recommend
  `hooks_add` to an agent whose profile denies it). Verify what the SP
  composition already knows about grants.
- **Scaling pressure is the floor's own success**: `enumerate-all` (and
  codeact's signature block) grow with every authored part. The floor
  self-pressures toward the enhancement layer as the catalog grows — record
  the migration pressure as a function of catalog size; hot-list/curation
  and semantic search become mandatory at scale, not nice-to-have.
- **Out of scope**: the knowledge-side symmetry (self-authored *knowledge*
  via `index_update`/memory, vs self-authored *capability* here) — same
  floor/enhancement logic likely applies; separate arc. Scheme redesign is
  also out of scope: this proposal treats the four schemes as fixed lenses.

## 5. Evaluation of the foundation itself

The #1657 datum (30%→100% on flattening) proves wielding is measurable.
**The evaluation scenarios are the journeys of §2.6**: J-A discovery ("does
the model find an installed part it didn't create?"), J-B selection+authoring
("does it pick pipeline over skill when orchestration is needed, and author a
well-formed one?"), J-C composition ("can it chain input→workflow→output?"),
J-D promotion ("does a working improvisation get installed and rediscovered
next session?"), J-E floor-degradation (all of the above with embedding off),
J-F enhancement delta (hit-rate/cost improvement with embedding on). Score
via `judge_output` rubrics + journey friction counts (discovery steps per
journey). Capture the baseline BEFORE F-work lands; re-measure after each
phase. No "we shipped it, trust us".

## 6. Phasing sketch (dependency order, not a schedule)

1. **Phase 0**: E3 defect fix; E2 packaging verify; baseline scenarios (§5).
2. **Phase 1 (floor)**: F1 catalog completion + taxonomy gate → F2 SP routing
   model → F4 present install op.
3. **Phase 2 (show)**: F3 builtin exemplars + through-chain.
4. **Phase 3 (enhancement)**: E2 corpus (post-verify), docs-convention
   ratification.
5. **Phase 4 (loop)**: L1 idiom + L2 judge gate + L3 hygiene.

Each phase re-runs the §5 measurement; a phase that doesn't move a wielding
metric is re-examined before the next lands.

## 7. Open forks (owner decisions)

- **(a)** retrieval default-promotion: keep opt-in (floor-first, this
  proposal's stance) vs promote the retrieval scheme to default once E-layer
  matures — revisit after Phase 3 with §5 data.
- **(b)** E2 corpus shape if docs aren't packaged: package `reference/` vs
  distilled bundled guide.
- **(c)** builtin exemplar curation: which concrete exemplars ship (proposal:
  minimum viable = the through-chain + one per axis; resist builtin sprawl —
  every builtin must earn its place as a teacher).
