---
status: accepted (owner GO 2026-07-12) — phased execution per §6; §7 forks (a/b/c) remain open, they gate Phase 2-3 not Phase 1
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

**J-D. Promote (amortization, cross-session) — provenance-split (§2.7)**
inline composition succeeds → SP idiom nudge (L1) → **branch on provenance**:
*user-directed* (the user asked for this) → standard gate → install → active;
*auto-improvement* (the model chose to promote) → **mandatory** judge_output
gate (L2) → install **inert/proposed** → operator/next-turn ratification →
active. Either way **next session**: J-A finds it as an instance. *Risk:
friction at inline→install silently kills the loop (nothing amortizes); and
conflating the two provenances either over-gates user work or under-gates
autonomous self-modification.*

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

### 2.7 Two provenances of self-extension (governance boundary)

Self-extension has two triggers that **must not be governed identically**:

- **User-directed** — the operator explicitly asks for a part ("build a
  skill that…", "add a hook that…"). Intent is human-authorized; the human
  turn boundary IS the check; the standard permission gate on the install op
  suffices.
- **Auto-improvement** — the model decides *on its own* to author or promote
  a part (the §3-L amortization loop: a successful improvisation becomes a
  named asset with no human ask). Higher autonomy, **no turn-boundary check**.

Why the split matters (the band — *agency bounded by construction*):

1. Auto-improvement has no human authorizing intent → it needs a
   construction-level bound, not merely the same gate a user request passes.
2. It compounds the self-influence loop (§4): an auto-authored description
   re-enters the model's own future context with no human in between.
3. It can form self-modifying feedback loops — the same concern class as the
   hook-event loop-valve / `emit_hook_event` autonomy boundary (0059). An
   auto-improvement that installs a hook that triggers more auto-improvement
   is the runaway case.

Policy (enforced in §3-L, and this is the load-bearing part of the loop):

- **Provenance is a first-class, audited, OS-authoritative attribute.** Every
  authored/promoted part records `provenance ∈ {builtin, user_directed,
  auto_improvement}` in P6. **The value is set structurally by the OS from the
  actor + turn-context that drove the install — never self-reported by the
  LLM/action** (isomorphic to `emit_hook_event`'s ctx-side kind construction,
  0059 ②B): an auto-improvement must not be able to self-declare
  `user_directed` to bypass the Phase-4 gate. `builtin` is OS-stamped by the
  builtin-tier loader at load, so it cannot be forged either. Non-negotiable:
  a runaway self-improvement loop is indistinguishable from legitimate user
  work without it.
- **Auto-improvement proposes; it does not activate.** Default: an
  auto-improved part is authored **inert/proposed** (mirroring builtin-inert,
  F3), requiring an explicit operator — or next-user-turn — ratification to
  become active. User-directed parts activate under the standard gate.
- **`judge_output` gate is MANDATORY for auto-improvement**, optional for
  user-directed (the human already judged). The automated rubric is the
  substitute for the missing human check — so auto-improvement without an
  eval gate does not ship.
- **The valve/bound is provenance-aware.** Auto-improvement volume is
  itself bounded (rate/count) so a self-authoring loop force-closes, exactly
  as hook-driven turns are valve-bounded in 0059.

The SP routing model (F2) must teach the model *which mode it is in* and that
auto-improvement is propose-only. This distinction is a **Phase-4 (L-layer)
design gate**, but F2/F4 must not preclude it — e.g. the present-view install
op (F4) and the catalog registration path must carry the provenance field
**and its OS-authoritative source** from the start (cheap now, expensive to
retrofit; a field without a pinned source is a Phase-4 hole — Addendum A).

## 3. Proposed architecture

### F — the always-on floor (embedding-independent, holds across all 4 schemes)

- **F1. Catalog completion, structured by §2.4**: SP category lines +
  enumeration verbs for the reactivity axis (hooks/composers) and the
  presentation axis (views); role-structured category taxonomy.
  *Completeness gate*: a registry-derived CI check — every registered
  part-type has a category row; no curated subset (same shape as the
  `OP_KIND_MODEL_MAP` ↔ `control-ir.md` hard rule). **SSoT finding
  (grounded)**: no single part-type registry exists today
  (skills/pipelines/presentations are separate registries with no unified
  enumeration), so the gate needs a **thin part-type meta-registry** as its
  walk source — the same SSoT the builtin tier (F3) populates and the catalog
  reads (Addendum A).
- **F2. SP routing model** in the OS-frame: the part×role map, a
  mechanism-selection decision tree (hooks made visible for the first time),
  author-vs-reuse heuristics, and authoring-quality one-liners
  (typed/permissioned/evaluate-before-promote). **Hard char budget**,
  cache-static placement; the map carries *model + decision rules only* —
  details stay pull-side.
- **F3. Builtin exemplar set** (the "show" layer): one canonical exemplar
  per axis + **one through-chain** (hook input → pipeline/skill processing →
  present output) that demonstrates the composition thesis end-to-end.
  Builtins **ship inert**: discoverable in the catalog, never auto-enabled (a
  builtin hook firing by default would be a surprise-execution surface —
  feasible today via skills' `auto_invoke=False` and pipelines'/views'
  invoke-by-name inertness). Candidate exemplars exercise reyn's own idioms:
  retrieve-then-synthesize (semantic_search), self-review step (judge_output),
  zero-token status card (present). **Feasibility (grounded, corrects an
  earlier assumption)**: unlike hooks (`BUILTIN_HOOK_SCHEMAS`),
  skills/pipelines/present-views have **no code-shipped builtin layer today** —
  registration is operator-config-only. So F3's prerequisite is a **new
  builtin tier** in the config loader (mirror the hook-schema pattern),
  physically shipped by repurposing the dead `stdlib/**` package-data glob to
  `builtin/**` (Addendum A). This is plumbing, not just config-authoring. The
  named "builtin" is deliberate — `BUILTIN_HOOK_SCHEMAS`-consistent; **`stdlib`
  is abolished** (Addendum A).
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

- **L0. Provenance split (§2.7) is the governing invariant of this layer.**
  Every promotion path carries `provenance ∈ {builtin, user_directed,
  auto_improvement}` (OS-set, unspoofable — §2.7), recorded in P6. The two paths diverge on gate,
  activation default, and eval requirement (below). Design this before L1/L2
  mechanics — it is what keeps auto-improvement bounded-by-construction.
- **L1. Promotion as idiom, not mechanism**: a successful ad-hoc composition
  (inline pipeline, inline blueprint) is promoted by *calling the existing
  install ops* — no new op. SP teaches the idiom. **User-directed**: activates
  under the standard gate (human asked). **Auto-improvement**: authored
  **inert/proposed**, needs operator/next-turn ratification to activate. Open
  question: ergonomics of inline→install (file-write friction) — worse for
  auto-improvement since the human isn't present to resolve it.
- **L2. Eval-gated promotion**: `judge_output` as the promotion gate — a part
  earns catalog registration by passing a rubric. **Mandatory for
  auto-improvement** (the rubric replaces the absent human judgment);
  optional for user-directed. The rubric threshold + `on_fail` policy are the
  automated quality bar.
- **L3. Catalog hygiene**: self-extension accumulates junk; unused parts
  decay discovery precision (attention dilution, not just tokens). P6 already
  records invocations → usage-informed pruning/archival:
  authored → used → promoted OR expired.

## 4. Cross-cutting notes

- **Security — self-influence loop**: LLM-authored descriptions re-enter the
  model's own future context via the catalog (SP menus, tools=, signatures).
  Existing install threat-scans mitigate; the surface should be named in the
  security review of each F/L PR, and description length/content constraints
  considered. **The dangerous quadrant is auto-improvement (§2.7)**: a
  self-authored description with no human in the loop feeding the model's own
  future prompt — this is why auto-improvement is propose-not-activate +
  eval-gated + provenance-audited, not merely threat-scanned.
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

1. **Phase 0**: E3 defect fix (#2895); E2 packaging verify (**done —
   docs NOT packaged, Addendum A/§7b**); baseline scenarios (§5); **`stdlib`
   abolition** (cheap dead-code removal, Addendum A2 — clears the packaging
   glob for the builtin tier).
2. **Phase 1 (floor)**: F1 catalog completion + taxonomy gate (on the part-type
   meta-registry SSoT, Addendum A6) → F2 SP routing model → F4 present install
   op (carrying OS-authoritative provenance, Addendum A5).
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
- **(b)** E2 corpus shape — **docs are confirmed NOT packaged in the wheel**
  (grounded: package-data ships only `py.typed` / `environment/*.Dockerfile` /
  the empty `stdlib/**` glob; `docs/` sits outside `src/reyn`). So the fork is
  *how* to ship the corpus, not *if*: a `reference/` subset packaged into the
  wheel vs a distilled bundled guide. Corollary: wheel-only installs have no
  `docs/` — dev-only doc-grep features degrade; make dev-deploy-vs-installed
  explicit (Addendum A).
- **(c)** builtin exemplar curation: which concrete exemplars ship (proposal:
  minimum viable = the through-chain + one per axis; resist builtin sprawl —
  every builtin must earn its place as a teacher).

## Addendum A — grounded feasibility (2026-07-12, post-ratification)

Verified against main after #2894 landed; records the builtin-set feasibility,
the provenance-source structural rule, and the packaging reality that the
ratified body now references. These sharpen the ratified design; they do not
change its direction.

**A1. Placement — the builtin set needs new plumbing (small, well-scoped).**
Skills/pipelines/present-views have **no code-shipped builtin layer** — the
config loader's tier order is nine operator-config tiers with no
package-shipped tier below `reyn.yaml` (each registry's docstring states
"registered PURELY via explicit `entries`… clean break" from the old
directory-scan model). Only hooks have a compiled builtin
(`BUILTIN_HOOK_SCHEMAS`). To ship builtins present-by-default, add a **builtin
tier** (mirror the hook-schema pattern) as the lowest merge tier. Physical
shipping is half-wired already: `pyproject.toml` package-data has a
`stdlib/**/*` glob that currently matches **zero files** — repurpose it to
`builtin/**/*` over a new `src/reyn/builtin/` dir.

**A2. `stdlib` abolition = the same move, and it is cheap.** `stdlib` is a
legacy old-skill-feature remnant with **zero load-bearing footprint** today:
no `src/reyn/stdlib/` package, no config key, no registry populated through
it, no runtime import. Remnants to delete/rename: the dead package-data glob,
two doc-stub pages (`docs/reference/stdlib/`), a stale `scan_dirs` comment, a
misleadingly-named permission test, and one possibly-stale dogfood scenario
(`stdlib_skills_core.yaml`). Abolishing `stdlib` and creating the `builtin`
tier are one rename/repurpose.

**A3. Inert shipping is representable per-type.** Skills carry two axes
(`enabled`, `auto_invoke`) → `enabled=True, auto_invoke=False` =
registered-but-not-auto-invoked. Pipelines and present-views have only
`enabled`, but are invoke-by-name (a pipeline runs when launched; a view
renders only when a `present` op names it) → inherently inert until
referenced. So builtins ship discoverable-but-dormant without new state.

**A4. Through-chain wireability — WIREABLE with one nuance.** (a) hook →
pipeline: ✓ (`HookDef.pipeline_launch`). (b) pipeline step → any Control-IR op
(`judge_output` / `present` / `semantic_search`): ✓ (`ToolStep` dispatches any
registered op by name). (c) present reading a prior step's output: **partial**
— pipeline step output is in-memory only, never auto-written to the workspace,
while `present`'s `data_ref` reads the workspace file tree. **Resolution**: the
flagship through-chain builtin should render `present` from **`data_inline`**
(the step's value bound as an arg), avoiding a workspace round-trip; the
`data_ref` path would need an explicit `write_file` step. Verify the exact
inline-binding form during F3.

**A5. Provenance source (the load-bearing security rule).** Provenance is
OS-authoritative and unspoofable: `builtin` is stamped by the builtin-tier
loader at load; `user_directed` vs `auto_improvement` is derived by the OS from
the actor + human-turn-boundary that drove the install — **never self-reported
by the LLM or the authoring action** (isomorphic to `emit_hook_event` ②B).
Applies to **all** install paths (skills/pipelines/present-views), not just
present. Locking this in Phase-1 (field + source) is what makes the Phase-4
auto-improvement gate structural rather than advisory.

**A6. The SSoT trinity.** One **part-type meta-registry** should be the single
source for three consumers that today have none in common: the taxonomy
completeness CI gate (walks it), the builtin tier (populates it), and the
catalog (reads it). Deciding this SSoT in Phase-1 keeps F1 (taxonomy), F3
(builtin tier), and catalog completion coherent instead of three parallel
enumerations.

## Addendum B — Layer A design (settled 2026-07-12, grounded + pressure-tested)

Layer A = the provenance plumbing + the present-view install op (the §2.7
provenance split made structural). Grounded against main after #2899; the
sub-agent-turn ruling below is lead-adjudicated. This is the design record the
Layer-A implementation follows. (Layer C — the SP part×role routing frame — is
a **separate small PR**, dispatchable in parallel; SP prose is a different kind
of change from this security-sensitive plumbing.)

**A7. Turn-origin seam — mirror the proven `_current_task_id` pattern.** The
turn `kind` (user / hook / pipeline_result / wake / …) is a local in
`run_one_iteration`, but `_stamp_execution_context(kind, payload)`
(session.py:4651-4699) is already the single OS-side seam that classifies the
turn kind into a persisted field (`_current_task_id`), threaded into
`OpContext.current_task_id` at both ctx-build sites (session.py:6089 +
router_host_adapter.py:2088 via a live callback). `turn_origin` mirrors this
exactly: derive `self._current_turn_origin` from `kind` inside
`_stamp_execution_context`, thread it through `build_router_op_context` into a
new `OpContext.turn_origin` field at both sites. No new mechanism — a
duplicate of an existing, proven seam.
- **kind → origin map (load-bearing semantic):** `user` → `user_directed`;
  **everything else** (hook, pipeline_result, wake kinds, and any *unmapped*
  kind) → `auto_improvement`. Only an explicit `user` turn grants
  `user_directed`; the default is the stricter `auto_improvement`.
  **Silent-default-to-`user_directed` is forbidden** — it would let an
  unmapped/autonomous turn bypass the Phase-4 auto-improvement gate (§2.7).
- **Sub-agent turns (`agent_request`/`agent_response`) = `auto_improvement`**
  (lead-adjudicated, conservative). Rationale: "a human directed the parent
  task" ≠ "a human directed *this* install action" — the same principle as a
  human-configured hook (A human configuring the hook is the condition that
  produces the turn, not the direction of the action). `auto_improvement` is
  the stricter gate = safe side, within §2.7's intent. An owner may later relax
  sub-agent installs to `user_directed`; the safe default is `auto`.

**A8. Present-view install op — mirror skill-install structure; threat is
LOWER than `emit_hook_event`, not emit-class.** Present has no install op today
(inline-only; `presentations.yaml` is operator-only). The new
`presentation_management__install_*` mirrors skill-install's **structure**:
tool def with gates, a `file_write` permission gate on
`.reyn/config/presentations.yaml`, `record_config_generation` after the write,
and hot-reload of a pure addition. But the **threat-scan is asymmetric**:
skill/pipeline install scans only the free-text `description` via
`scan_for_threats`; a present blueprint is **structurally non-executable by
construction** (catalog.py: 8 fixed components; every non-literal value is a
`$bind` RFC-6901 JSON-Pointer; no template-ref / eval / exec surface;
`image.src` renders as a label, no fetch/SSRF). `validate_blueprint` **already
fills the role** `scan_for_threats` fills for skill/pipeline description text.
So present-install is ≈ skill-install in plumbing, **lower** in payload-threat
(a non-executable blueprint vs free-text the model reads), and **not**
`emit_hook_event`-class (which is a live-effect op). `record_config_generation`
inherits the existing config crash-recovery — **no new recovery-gated
obligation** (no truncate-falsify test owed for this op).

**A9. Uniform provenance = uniform SOURCE, not a uniform write-site.** There is
no shared install helper (skill/pipeline/present handlers each orchestrate,
sharing only three primitives: `record_config_generation`, hot-reload,
`scan_for_threats`). The security property (unspoofable) therefore lives in the
**source**: every handler stamps `entry["provenance"] = ctx.turn_origin`
(OS-set, A7) at entry-construction — a per-handler write whose *value* is
single-sourced and cannot be supplied by the LLM. (Wrapping
`record_config_generation` was considered, but it does not touch entry shape
today; per-handler stamping from the shared OS-set source is cleaner.) The
`builtin` value is stamped on a **different** seam — the registry-build loader
path (`build_*_registry` at session-factory construction), where the future
builtin tier (F3) loads — never via the install-op path.

**Co-vet pins for Layer A (isomorphic to `emit_hook_event` ②B falsify):**
1. **Provenance is ②B-structural.** The install op schema has **no**
   `provenance` field; the handler stamps only from `ctx.turn_origin`. Falsify:
   add a `provenance` field to the op schema and read it in the handler → an
   `auto` turn supplies `user_directed` → if the spoofed value survives, RED.
2. **`turn_origin` completeness is fail-safe.** Every turn kind maps; an
   unmapped kind resolves to `auto_improvement`, never `user_directed`.
   Falsify: introduce a new turn kind with no mapping → if it resolves to
   `user_directed`, RED.
3. **Present-install threat gate is `validate_blueprint`.** Strip
   `validate_blueprint` from the install path → a malformed / non-catalog
   blueprint installs → RED.

## Addendum C — Layer C design (part×role routing frame; settled 2026-07-12)

Layer C = F2's structural core: the **part×role routing model** placed in the
SP so the model learns *which mechanism to reach for*. It is a separate,
small, security-surface-free PR, dispatchable in parallel with F3 onward.

**C1. Placement — `router_frame.py`, the scheme-independent OS-frame.** The
routing model is scheme-independent prose (§2.3): it goes in the OS-frame, not
a scheme-owned tool-use slot, so it holds across all four tool-use schemes
(grounding: schemes own the tool-use SP, the OS owns the frame). It sits in
the **cache-static prefix** (§1's ~60% cache coverage), under a **hard char
budget** — the frame carries the *model* (the map + decision rules), never the
*catalog* (which is pull-side, §2.5).

**C2. Content.** (1) The part×role map (input / workflow / output ×
part-type, §2.1). (2) A mechanism-selection decision tree: *need input →
hook | mcp | retrieval; need workflow → skill | pipeline | mcp-step; need
output → present | render | mcp-write*. (3) **Hooks made visible** — they are
today entirely absent from the SP (§1.1); this is where the reactive/input
axis first becomes wieldable. (4) An author-vs-reuse heuristic +
authoring-quality one-liners (typed / permissioned / evaluate-before-promote).

**C3. Load-bearing decision — the part×role map is DERIVED from the Layer B
meta-registry, not a hand-written parallel table.** Each row derives from a
part-type's `roles` frozenset in `PART_TYPE_REGISTRY`. This makes Layer C ride
the SSoT: a new part-type (a marker dropped into `reyn.core.part_types`)
auto-appears in the SP routing map, with zero drift — the same completeness
discipline that made Layer B itself derived (Addendum A6, #2899). A
hand-written SP table would silently drift from the registry exactly as a
hand-listed meta-registry would have.

**Co-vet pins for Layer C:**
1. **Registry-derivation (no drift).** Add a marked part-type to the
   meta-registry → it appears in the SP frame's part×role map (mirror of the
   #2899 auto-appear witness). Strip the derivation to a hand-written table →
   the new part-type does **not** appear → RED (the drift the derivation
   prevents is now observable).
2. **Scheme-independence.** The routing model appears under all four
   tool-use schemes. Move it from the OS-frame into a scheme-owned slot →
   it is missing under ≥1 scheme → RED.
3. **Char-budget / cache-static — bounded per part-type.** The frame sits in
   the static cache-prefix, not the dynamic tail; it carries the model, not the
   catalog. Because the part×role map is *derived* (C3), it **grows with the
   meta-registry** — so the budget is not just a fixed total but a **per-part-
   type derived-row cap**: adding the Nth part-type must keep the frame within
   its cache-static budget (a verbose per-type row would bloat the every-turn
   SP as the SSoT grows). 5 part-types is within budget today; the pin is that
   the per-type derived-row cost stays bounded (cost-discipline lens).

**Scope:** small — SP prose in `router_frame.py` derived from the meta-registry;
no security surface (unlike Layer A). Lower risk. Layer C completes F1's floor
(catalog SSoT + provenance + routing model); F2's remaining pieces (discovery-
mandate strengthening, the fuller author-vs-reuse guidance) and F3 (builtin
tier + `stdlib` abolition) follow.

## Addendum D — Information-placement design: the learning 動線 (settled 2026-07-12, owner-driven)

**This addendum is the design the wave was founded to produce** (owner: "これを
設計して欲しいというのが土台作り wave の発端 — 動線を考慮して、どの情報を
どこに配置するか"). It decides, for every kind of information the LLM needs to
wield reyn, **whether it lives in the SP, in a skill, or in docs** — and makes
the placement drift-proof and production-reachable. It reframes and completes
F2/F3b. Self-sufficient: a fresh session can execute from this record.

### D0. Problem: reyn-specific knowledge has no guided path

reyn's parts are not in any base model's training. **If the detailed spec of a
part is not reachable from an LLM-facing surface, the model will not use the
part** — it falls back to base-model defaults, or hallucinates the format. The
動線 (learning line) = the guided path from "I need to use/author part X" to
"the spec that makes me use it correctly." A locus table on paper (§2.5) is
not a design until reachability is *audited* — that audit (D2) is what
grounded this addendum, and it is the normative check for any future
part-type.

### D1. The three-layer doc model, and skill as the gap-filler

| layer | documents | nature |
|---|---|---|
| `concept` | why / mental model | finite, formal, shipped |
| `reference` | mechanism spec (how to call/author each part) | finite, formal, shipped |
| **skill** | **task procedures + composition know-how** ("when to use which part, how to wire them together for a task") | **infinite long-tail, LLM-authorable, pulled just-in-time** |

concept/reference fully describe the *mechanisms*; the infinite space of "how
do I achieve task X with these parts" is the gap **skills exist to fill**
(owner: "concept/reference から漏れる隙間を埋めるのが skill"). Corollaries:
- **Composition guidance belongs in the skill layer, NOT the SP** — it is
  long-tail judgment knowledge; pushing it into the SP bloats every turn and
  still can't cover the tail.
- The SP shrinks to a minimal bootstrap: the map (Layer C), the discipline
  (discovery-mandate, author-vs-reuse invariant), and **a named pointer to the
  cheat-sheet skill** (D4).
- **The flagship builtin is a "reyn cheat-sheet" skill** — a quick-reference
  for reyn-specific usage: when to use which part, composition idioms, op
  essentials, and pointers to full specs. This is the single artifact that
  turns "reyn has features" into "the LLM uses reyn's features."

### D2. Reachability audit (grounded 2026-07-12 — the 動線 check)

For each part, can the LLM reach the detailed spec from an LLM-facing surface?

| part | spec doc | tool-desc teaches/points? | doc pointer? | verdict |
|---|---|---|---|---|
| skill | concepts/tools-integrations/skills.md | install params only; no SKILL.md format | none | **PARTIAL** (use-existing OK via L2 read; author-new has no format path) |
| pipeline | reference/runtime/pipeline-dsl.md | says "Appendix B" — never names the doc; zero grammar | none | **BROKEN** |
| mcp | concepts/.../mcp.md | params + `describe_mcp_tool` live round-trip | none | **PARTIAL** |
| present | concepts/runtime/present.md + control-ir.md | install-op enumerates components/`$bind`; the everyday inline `present` does NOT | none | **PARTIAL** |
| render_template | control-ir.md § | fully self-teaching in-op | none | **REACHABLE** (self-sufficient) |
| hook | concepts/runtime/hooks.md | **explicit named pointer in the op description** ("see … docs/concepts/runtime/hooks.md") | yes (descriptions/hooks.py:34,44) | **REACHABLE** — the exemplar pattern |
| Control-IR ops (class) | reference/runtime/control-ir.md | no op names it; no op→doc convention exists | only a human-`/help` `see_also` | **BROKEN as a class** |

**Hard production fact**: `docs/` is not packaged in the wheel, and
`resolve_reyn_root()` raises in a wheel install — so in a **production deploy
every docs path is unreachable regardless of pointers**. 2/7 reachable, and
even those only in dev checkouts. Also: the audit checked 4 channels
(tool-desc / SP / doc-pointer / packaging) — it missed a 5th, **error
messages**, adopted as a rail in D5c.

### D3. Governing rules

1. **Production-reachability constrains placement.** The always-reachable
   floor = SP + tool-descriptions + **packaged skills** (all wheel-shipped).
   Docs are the source-of-truth but may never be the *sole* home of
   information required to use a part correctly in production.
2. **Single-home content, multi-home pointers.** Redundant *pointers* (SP,
   op-desc, error message) are cheap and good; redundant *content* is the
   drift source. Every piece of content has exactly one authoritative home;
   every other surface points or mirrors mechanically (D5).
3. **Tool-description budget**: under `enumerate-all` every description rides
   `tools=` each turn → op-desc carries the **minimum not to mislead** (param
   shapes) **+ a pointer**; grammar bodies live pull-side.

### D4. The placement map (the design)

```
SP         = map (registry-derived, Layer C) + discipline + the cheat-sheet
             skill named explicitly (existence CI-gated, D5e)
op-desc    = minimal params + doc_ref (structured pointer field, D5d)
error msg  = on parse/validation failure: pointer to cheat-sheet / doc_ref
             (failure-driven rail, D5c)
skill L2   = the cheat-sheet body: composition know-how + per-part usage
             essentials; hard token budget (~1-2k); examples CI-executable (D5a)
skill L3   = build-time byte-mirror of the reference docs (production-
             reachable full specs; E2's shipped corpus source) (D5b)
docs       = source-of-truth (the single upstream for L3 mirrors, derived SP
             rows, and cheat-sheet content; CI enforces the sync)
```

### D5. Drift-proofing mechanisms (what makes this real, not paper)

The cheat-sheet duplicates knowledge that lives in docs/parsers — the same
hand-list-drift failure mode this arc fought at the registry level (#2899,
Layer C). Placement without these gates is a regression:

- **D5a — Executable cheat-sheet.** Every example in the cheat-sheet is
  CI-validated against the real implementation: yaml pipeline blocks must
  pass `parse_pipeline_dsl`; blueprints must pass `validate_blueprint`;
  op-call examples must validate against `OP_KIND_MODEL_MAP` schemas. A
  wrong example cannot ship. Derived sections (op lists, part-type rows) are
  generated from registries; prose stays authored — the Layer-C C2/C3 split
  applied to content.
- **D5b — Builtin tier as the docs carrier (resolves fork §7b).** The
  cheat-sheet skill's **L3 assets = build-time byte-mirrors of the
  `reference/` docs**, shipped via the already-packaged builtin tier
  (`builtin/**` glob, F3a) and CI-checked byte-identical against `docs/`.
  One mechanism gives: production file-read reachability, a shipped corpus
  for E2's semantic index, and working doc-pointers in production. Docs stay
  the source-of-truth; distribution rides the builtin tier.
- **D5c — Error-message rails.** Parse/validation errors returned to the
  model (`PipelineParseError`, blueprint/schema rejections) carry the
  pointer ("see the reyn cheat-sheet skill / <doc_ref>"). The failure moment
  is the highest-value teaching moment, and the re-prompt loop already
  exists.
- **D5d — `doc_ref` as a first-class field** on ToolDefinition/PartTypeSpec,
  surfaced in `describe_action`, with a registry-walk completeness gate
  (every op whose spec exceeds its description carries one). This fixes
  "Control-IR BROKEN as a class" structurally instead of hand-editing N
  descriptions; `hooks_add`'s hand-written pointer becomes the general rule.
- **D5e — SP-pointer ↔ builtin existence gate.** Builtins stay inert
  (`auto_invoke=False`, F3a) — a skill's menu line is advertising, not
  execution, but relaxing a landed safety default is not needed: the SP
  names the cheat-sheet directly. That name is then a load-bearing contract
  → a CI test asserts the named builtin exists (a dangling SP pointer is the
  silent-never-fire class again).

### D6. Decisions and residual forks

- **§7b: RESOLVED by D5b (recommendation)** — package `reference/` as
  builtin-carried L3 mirrors, not as a wheel-docs tree nor a hand-distilled
  bundle. Needs lead/owner confirm on the build-step mechanics.
- **Inert-vs-hub: ruled** — keep inert-ship; SP-named pointer + D5e gate.
- **F3b reshape**: flagship = the cheat-sheet skill; per-task builtins
  (retrieve-then-synthesize / draft→judge→match→revise / status-card /
  one hook exemplar) secondary. Cold-start/first-hour utility is **one**
  curation criterion (retention gate), not the sole one (owner correction
  recorded): steady-state value (e.g. reactive hooks) and exemplar quality
  count equally. Legacy cookbook SKILL.md (removed phase-graph shape) must
  be fixed/annotated so authors can't copy the wrong template.
- **PARTIAL quick-fixes** riding F3b: inline `present`'s description gains
  the component/`$bind` enumeration (or doc_ref); skill-install's gains the
  SKILL.md live-format essentials (or doc_ref).

### D7. Evaluation

- **J-LEARN journey** (added to §2.6/§5): in a production-shaped deploy (no
  docs dir), a cold model must author a *valid* pipeline/blueprint on first
  attempt by following the 動線 (SP → cheat-sheet → L3/doc_ref). Metric:
  first-try validity rate; falsify: **strip the cheat-sheet → authoring
  quality collapses** (witnesses the placement is load-bearing).
- Gate falsifies: a bad example in the cheat-sheet → D5a RED; L3 mirror
  drift → D5b byte-gate RED; missing doc_ref on a spec-bearing op → D5d
  walk RED; renaming the cheat-sheet → D5e RED.

### D8. Sequencing

D-layer work rides F3b (cheat-sheet = flagship content) plus a small
mechanism slice (D5c error rails, D5d doc_ref field+gate, D5e SP gate,
D5b build-step) — one PR or an F3b sibling; E2 later consumes D5b's shipped
mirrors. F3a (builtin tier plumbing) is unchanged and already in flight.

## Addendum D9 — criticality split; `present` as an SP-critical affordance (owner refinement 2026-07-12)

**Owner observation**: the LLM tends to answer **without reading all available
content each turn**. Two structural consequences that sharpen D3/D4's
placement map.

### D9.1 — The cheat-sheet is PULL, so it may be skipped → split by criticality

A skill (the cheat-sheet, D1) is read just-in-time — the model may act
*without* reading it. So placement gains a criticality axis:

- **SP-resident (push, always seen)** = **critical-or-you-fail affordances** —
  knowledge whose absence causes a *systematic default failure*.
- **cheat-sheet (pull, may be skipped)** = the **fuller** how-to it looks up.

**Criterion — an affordance is SP-critical iff missing it causes a bad
default, not merely a suboptimal lookup.** The SP-critical set is the model's
anti-default-failure countermeasures, and it is enumerable:
- discovery-mandate (miss → doesn't discover, re-invents) — *landed, Layer C*
- author-vs-reuse (miss → re-authors what exists) — *landed, Layer C*
- **`present` (miss → dumps large content into the reply)** — *new, D9.2*
- author-from-spec-not-guess (miss → hallucinates a mechanism) — the SP
  pointer + reach-the-spec rails (D5c/D5d)

The cheat-sheet (curated-5 #1) stays the fuller hub; its *critical fragments*
promote to the SP. D4 refined: `SP = map + discipline + critical affordances
(incl. present) + cheat-sheet pointer`; `cheat-sheet = fuller`.

### D9.2 — `present` is an SP-critical affordance (dump-prevention)

"Answers without reading all" → **content dumping** (pasting large output into
the reply). reyn's `present` **is** the zero-token mechanism (the present
layer, records 0054/0055): show results to the operator instead of spending
them as reply tokens. **The model won't use `present` if it doesn't know it
exists** → the affordance is SP-critical, not cheat-sheet-optional.

- **Scope `present` to OUTPUT only — this is the load-bearing precision**
  (owner correction). `present` sends content to the operator *zero-token; the
  model does not read it back*. So it applies **only to results/reports for the
  operator**, never to content the **model itself must consume** (skill bodies,
  docs, target files) — those must be **read into context**. Presenting
  input-content means the model never sees it (the inverse failure: "present
  what you should have read, then never consume it"). The SP essential is one
  distinction: **"Results for the operator → `present` (zero-token). Content
  you use/follow (skills, docs, the thing you're processing) → read it, don't
  `present` it."** This maps cleanly onto the part×role axes (Layer C):
  `present` is the **output** role; consuming content is the **input** role.
- **No separate "offload" vocabulary** (owner correction — earlier draft
  over-abstracted): `present` *is* the mechanism, extending Layer C's
  mechanism-selection `output → present`.
- **No separate offload builtin**: the pattern is "process in a pipeline →
  terminal `present`." The **flagship (web_search → agent → judge → present)
  is exactly a pipeline-terminal `present` = the exemplar**; the status-card
  present-view (curated-5 #4) exemplifies the simple case. Both already in the
  set — nothing new to add.
- **Full `present` spec** → cheat-sheet + reference via `doc_ref` (D5d).

`present` is thus a *usage* affordance the SP must carry (beyond D2's 7
*authoring* part-types); the reachability discipline (D5) applies to its full
spec. The output/input scope also keeps docs-delivery (D5b) coherent: reyn's
docs are **model-read input** → reached via `doc_ref`/cheat-sheet and read into
context, *not* presented; only the flagship's *result* is operator output →
presented. Different axes, no contradiction. The cheat-sheet's composition
guidance carries the same distinction ("results → present; content to consume →
read").

### D9.3 — Falsifiability

- The SP-critical set is enumerable → a test asserts each critical line is in
  the built SP (strip the `present` line → the anti-dump essential is gone →
  RED).
- The J-LEARN journey (D7) gains a **dump-avoidance check**: a cold model
  handed a large result uses `present` rather than dumping it — witnessing the
  `present` affordance reached the SP-critical floor.

### D9.4 — Dispatch impact

F3b ships **cheat-sheet content (fuller hub) + the SP-essentials slice** (the
critical fragments, incl. the one `present` line) together. The SP-essentials
slice extends the merged Layer C frame (`router_frame.py`) and is small; the
`present` line is the load-bearing new SP content. No new "offload"
axis/vocabulary/builtin. D5e's existence gate still couples the cheat-sheet's
SP pointer to the shipped skill.
