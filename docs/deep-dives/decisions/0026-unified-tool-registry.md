# ADR-0026: Unified tool registry — single ToolDefinition for both router and phase surfaces

**Status**: Proposed (2026-05-09 → in progress; M1–M3 + M4 Phase 1–4 + Phase 3.5 router-side cluster activations landed)
**Track**: Architecture — closes the dual-implementation drift between
chat router (function calling) and phase Control IR (JSON output)
identified in `docs/concepts/llm-invocation-surfaces.md`.

---

## 1. Context

### Two invocation kinds, two capability catalogs

Reyn invokes the LLM in two structurally distinct contexts (documented in
`docs/concepts/llm-invocation-surfaces.md` §2):

- **Router-style** (`RouterLoop` / `PlanRuntime`): native function calling
  via `build_tools()` in `src/reyn/chat/router_tools.py`. Each capability
  is a `ToolSpec` dataclass (commit `77d6db6`) that renders to an OpenAI
  `tools[]` entry.
- **Phase-style** (`ControlIRExecutor` + `OSRuntime`): JSON output contract.
  Each capability is an op kind registered in `OP_KIND_MODEL_MAP` in
  `src/reyn/op_runtime/registry.py`. The executor derives parameter schemas
  from the corresponding Pydantic `IROp` model at dispatch time.

These two catalogs serve the same underlying capabilities through different
protocols. The OS dispatches both, but the capability definitions live in
separate modules, are authored independently, and are tested in isolation.

### The dual-implementation cost

**Coordinated edits instead of single-source updates.** When `web_search`
received a DuckDuckGo operator hint (commit `8af3444`), the description was
updated in `router_tools.py`'s `ToolSpec`. The phase-side `ControlIROpSpec`
description in `control_ir_executor.py` is a separately maintained string —
changing one does not change the other. Every description update, metadata
addition, or behavioral clarification requires a two-file coordinated change
to keep the surfaces aligned.

**Drift during protocol transitions.** When `_DISPATCH_KIND` was refactored
into a derived alias from `_TOOL_SPECS_STATIC_ASYNC` (commit `77d6db6`),
backward compatibility with a test that pinned `plan` as async required
preserving the sidecar dict shape. The compatibility surface exists only
because router-side dispatch metadata has no counterpart on the phase side —
the two surfaces evolved their own bookkeeping independently.

**Type C gaps widen by default.** `docs/concepts/llm-invocation-surfaces.md`
§4 identifies three convention-drift asymmetries — capabilities present on
the router but absent from the phase Control IR surface:

- **Memory write** (`remember_shared`, `remember_agent`, `forget_memory`):
  available as router function-calling tools; no corresponding phase op.
- **Catalog browse** (`list_skills`, `describe_skill`, `list_agents`,
  `describe_agent`): available to the router; phases receive catalog
  injected at entry (read-only, no mid-phase query).
- **MCP discover** (`list_mcp_servers`, `list_mcp_tools`): available to the
  router; phases using `mcp` must declare server and tool name statically.

Each gap appeared because the capability was added to the router surface and
nothing in the architecture required the author to also add a phase-side
equivalent. Without a single-source structure, the default is drift.

**Future metadata has no home.** `ToolSpec` already carries commented-out
anchors for `cost_weight`, `rate_limit_class`, and `log_redaction` (see
`router_tools.py` lines 77–81). These fields have no equivalent on the phase
side. If operator-level per-capability metadata becomes necessary (cost
budgeting, rate limiting, audit redaction), there is no single place to
declare it.

### The user's stated goal

> "1 tool 実装 = router/phase 両方で使えるようになる、ただし役割によっては片方を遮断する
> というオプションは必要"
>
> One tool implementation means both router and phase can use it; the option
> to gate one surface off per role is required.

The user explicitly rejected a "Tier 2 composite tool" sub-proposal — a
middle abstraction between primitive tool and full Skill DSL that would allow
deterministic chaining of multiple tools without LLM involvement. The
rejection reasoning: Reyn is not a programming language. Deterministic
combination chains belong in scripts (Python, shell). Preprocessor and
postprocessor steps already cover the forced-hook use case (bound to a
specific phase / skill), but they are not standalone reusable workflows.
Adding Tier 2 would invent a third Reyn concept, not unify the existing two.

The resulting architecture is **2 layers only**: Tool (primitive, dual-
protocol) and Skill (multi-phase decision graph). The unified registry
implements Tool.

---

## 2. Decision

Adopt a unified tool registry: one `ToolDefinition` per capability, two
protocol renderers, a single handler per capability.

### Core types (design, not implementation)

**`ToolGates`** — per-capability static surface declaration:

```
ToolGates:
  router: "allow" | "deny"   # whether the router LLM may call this tool
  phase:  "allow" | "deny"   # whether a phase's control_ir may invoke this op
```

**`ToolDefinition`** — the single source of truth for a capability:

```
ToolDefinition:
  name: str                        # canonical capability name
  description: str                 # shared human- and LLM-readable description
  parameters: dict                 # JSON schema (object root)
  handler: ToolHandler             # async callable (see below)
  gates: ToolGates                 # static surface gate
  category: str                    # grouping (for catalog / listing)
  # Future metadata fields (anchored, not yet added):
  # cost_weight: float = 1.0
  # rate_limit_class: str | None = None
  # log_redaction: list[str] = field(default_factory=list)
```

**`ToolHandler`** — the execution callable:

```
ToolHandler = Callable[[dict, ToolContext], Awaitable[dict]]
```

Args are the validated parameter dict; `ToolContext` is the protocol-agnostic
execution context (see below).

**`ToolContext`** — protocol-agnostic execution context passed to every
handler:

```
ToolContext:
  events: EventLog
  workspace: Workspace
  permission_resolver: PermissionResolver | None
  caller_kind: Literal["router", "phase"]
  # Caller-kind-specific sub-objects:
  router_state: RouterStateCtx | None   # chain_id, session metadata
  phase_state: PhaseStateCtx | None     # skill_run_id, current_phase, etc.
```

**`ToolRegistry`** — loaded at startup, immutable at runtime:

```
ToolRegistry: dict[str, ToolDefinition]   # name → definition
```

### Two render methods on ToolDefinition

- **`render_for_router()`** — produces the OpenAI `tools[]` entry shape
  that `build_tools()` currently returns per tool. Output must be
  byte-identical to the current `ToolSpec.to_openai_dict()` output for
  each capability to preserve LLMReplay fixture stability.

- **`render_for_phase()`** — produces the `ControlIROpSpec` shape that
  `ControlIRExecutor.available_ops()` currently returns per op kind,
  and the `_build_phase_tool_catalog` entry used for dispatch-time arg
  validation.

### Two protocol-specific dispatchers, one registry

Both dispatchers look up the capability by name in the same `ToolRegistry`,
verify the relevant gate (`gates.router` or `gates.phase`), validate args
against the `parameters` schema, build a `ToolContext`, and invoke the
handler:

- **`RouterLoop`** (function calling protocol): calls
  `registry["name"].handler(args, ctx)` where `ctx.caller_kind="router"`.
- **`ControlIRExecutor`** (JSON output protocol): calls
  `registry["name"].handler(args, ctx)` where `ctx.caller_kind="phase"`.

### Explicitly NOT in scope for this ADR

- Adding a Tier 2 "composite tool" abstraction (= rejected by user).
- Changing the LLM protocols (= router stays function calling; phase stays
  JSON output Control IR).
- Changing Skill DSL, phase frontmatter, plan mode, preprocessor,
  postprocessor, or `OSRuntime` orchestration logic.

---

## 3. Three-layer gate model

The unified registry introduces one new gate axis; two others already exist.
All three are orthogonal.

| Layer | Scope | Declared in | Example |
|---|---|---|---|
| 1. Role gate | per-capability, registry-level | `ToolDefinition.gates` | `shell` has `gates.router=deny` |
| 2. Phase narrowing | per-phase | skill phase frontmatter `allowed_ops` | a phase declares `allowed_ops: [file]` |
| 3. Permission | per-call, runtime | `skill.permissions` (P5) | file write to specific path |

**Layer 1** is a static capability gate: `shell` is structurally unavailable
to the router surface regardless of operator configuration. New capabilities
default to `router=allow, phase=allow` unless there is a documented role-
separation reason (Type B asymmetry per `llm-invocation-surfaces.md` §4).

**Layer 2** is the existing per-phase narrowing. A phase author declares
`allowed_ops: [file, ask_user]`; the OS enforces this at dispatch time as
defense-in-depth. Layer 2 operates within Layer 1's allow-set — a phase
cannot declare an op that Layer 1 gates at `phase=deny`.

**Layer 3** is the existing runtime permission check. `PermissionResolver`
validates call args (path, MCP server name, etc.) against declared
`skill.permissions`. Layer 3 fires inside the handler, after Layers 1 and 2.

A capability blocked by Layer 1 never reaches Layer 2 or Layer 3. A
capability narrowed out by Layer 2 never reaches Layer 3. The three axes
compose without cross-coupling.

---

## 4. Considered alternatives

### Alternative A: Status quo (= dual implementation, Type C closed piecemeal)

Keep `router_tools.py` and `op_runtime/` separate. Close the three Type C
gaps (memory write phase-side, catalog browse phase-side, MCP discover
phase-side) by adding new code paths on each surface independently.

**Pros:** zero refactor cost; no coexistence period; no migration risk.

**Cons:** drift continues structurally — each new capability still requires
coordinated multi-file changes; descriptions can diverge after landing.
Closing the three Type C gaps under this alternative means three separate
PRs, each touching both surfaces, without addressing the root cause (= no
single source). Future tool metadata (cost_weight etc.) has no clean home.
The `ToolSpec` anchors in `router_tools.py` (lines 77–81) hint toward the
unified design but have no phase-side counterpart to anchor against.

### Alternative B: Tier 2 composite tool

Introduce a middle abstraction between `ToolDefinition` (primitive, atomic)
and `Skill` (multi-phase LLM-decided graph): a "composite tool" that chains
two or more primitive tools deterministically without LLM involvement between
steps.

**Pros:** lightweight; could close some Type C gaps by composition; doesn't
require full registry migration.

**Cons:** re-implements function composition at a Reyn abstraction level
where Python script suffices. Adds a third surface (= does not unify the
existing two). Phase preprocessor / postprocessor steps already cover the
forced-hook deterministic chain use case, but they are bound to a specific
phase and skill — not standalone reusable workflows. The user's explicit
rejection: Reyn is not a programming language; deterministic combinations are
over-engineering here.

This alternative was raised during the doctrine discussion that produced
`llm-invocation-surfaces.md` and explicitly rejected before this ADR was
scoped.

### Alternative C: Unified registry (= chosen)

The design in §2.

**Pros:** structural drift is eliminated by construction — a single
`ToolDefinition` is the only place to change a description, gate, or metadata
field; both surfaces reflect the change. Type C gap closure becomes a gate-
flag flip (set `phase=allow`) instead of a separate PR per gap. Future tool
metadata (cost_weight, rate_limit_class, log_redaction) has a natural home.
Test surface compresses: one set of invariants per capability, not one per
surface. Onboarding is simpler: "to add a capability, write a ToolDefinition
in `src/reyn/tools/<name>.py`."

**Cons:** ~3-week refactor; coexistence period during migration adds
complexity; `render_for_router()` must produce byte-identical output to
preserve all LLMReplay fixtures (= non-trivial constraint); `ToolContext`
design may hit a router/phase mismatch that stops the migration. POC stop
signal (§5) acknowledges this.

---

## 5. Migration plan

The migration has four phases. Each phase is independently landable and must
leave the full test suite passing.

### Phase M1: Infrastructure (~5 days)

Create the new module tree:

```
src/reyn/tools/
  __init__.py
  types.py        # ToolDefinition, ToolGates, ToolContext, ToolHandler
  registry.py     # ToolRegistry load / lookup
  dispatch.py     # shared gate-check + arg-validation logic
```

**Adapter shims (public API unchanged):**

- `chat/router_tools.py` `build_tools()` rewrites its body to consume
  `ToolRegistry` and call `definition.render_for_router()` per entry.
  Return type (`list[dict]`) and parameter signature are unchanged.
- `op_runtime/registry.py` `OP_KIND_MODEL_MAP` becomes a derived view of
  the registry (Pydantic model co-registered alongside `ToolDefinition`
  during migration; see Open question 2 in §7).
- `_DISPATCH_KIND` continues as a derived alias (backward compat for the
  test that pins `plan` as async; see commit `77d6db6`).

**New tests:** `tests/test_tool_registry_invariants.py` (Tier 2) — registry
loads without exception, all entries have required fields, all gate
combinations are valid, `render_for_router()` output matches current
`build_tools()` for zero-arg invocations.

M1 delivers the infrastructure; no capability is migrated yet.

### Phase M2: POC — web_search (~3-4 days)

Migrate a single capability as a proof-of-concept: `web_search`.

Rationale for choosing `web_search`:
- Handler already exists in `op_runtime/web.py` (`handle_web_search`) and
  is relatively self-contained.
- Present on both surfaces (= smallest blast radius for verifying
  `render_for_router()` + `render_for_phase()` output parity).
- No role-gating complication (both surfaces `allow`).

**Deliverable:** `src/reyn/tools/web_search.py` containing the
`ToolDefinition` instance plus handler. Both `build_tools()` and
`ControlIRExecutor.available_ops()` derive `web_search` from the registry.

**Verification gates (all must pass before M3 starts):**

1. All tests pass (= 1459 + new Tier 2 invariants).
2. LLMReplay fixtures byte-identical — `render_for_router()` output for
   `web_search` is identical to the current `ToolSpec.to_openai_dict()`
   output. No fixture re-recording.
3. Drift test: change `web_search.description` in the registry definition;
   verify that `build_tools()` and `available_ops()` both reflect the change
   without further edits.
4. `ToolContext` design validated — handler receives the correct context
   fields for both `caller_kind="router"` and `caller_kind="phase"`.

**Stop signal:** if (a) byte-identity fails and cannot be resolved by
adjusting `render_for_router()`, (b) `ToolContext` design hits a
router/phase mismatch that requires different handler signatures per
protocol, or (c) Pydantic arg validation behaves differently per protocol
in a way that cannot be reconciled in `dispatch.py` — halt M2, write
findings, return to M1 redesign or fall back to Alternative A.

### Phase M3: Rolling migration — 13 capabilities (~7-10 days)

After M2 POC validates the infrastructure, migrate remaining capabilities in
the order below. Each migration is one commit; all tests must pass at each
commit.

| # | Capability | gates | Type C closure? |
|---|---|---|---|
| 1 | web_search (POC) | router=allow, phase=allow | no |
| 2 | web_fetch | router=allow, phase=allow | no |
| 3 | file ops (read/write/glob/delete/grep/edit) | router=allow, phase=allow | no |
| 4 | mcp + list_servers/list_tools extension | router=allow, phase=allow | yes (MCP discover) |
| 5 | run_skill / invoke_skill | router=allow, phase=allow | no |
| 6 | shell | router=deny, phase=allow | no |
| 7 | lint | router=deny, phase=allow | no |
| 8 | ask_user | router=deny, phase=allow | no |
| 9 | memory ops (remember_shared, remember_agent, forget_memory, list_memory, read_memory_body) | router=allow, phase=allow | yes (memory write) |
| 10 | delegate_to_agent | router=allow, phase=deny | no |
| 11 | plan | router=allow, phase=deny | no |
| 12 | reyn_src_list, reyn_src_read | router=allow, phase=deny | no |
| 13 | catalog ops (list_skills, describe_skill, list_agents, describe_agent) | router=allow, phase=allow | yes (catalog browse) |

The three Type C gaps (#4 MCP discover, #9 memory write, #13 catalog browse)
close as a natural side effect of migration: adding `phase=allow` in the
gate and wiring the handler to the phase dispatch path is the entire gap
closure. No separate PRs needed.

**Migration semantics note.** Capabilities #3 (file ops),
#4 (mcp ops), #9 (memory ops), #13 (catalog ops) involve
coarse-to-fine name unbundling on the phase side. Each unbundling
forces a decision on Open Questions #6 (canonical naming) and #7
(`allowed_ops` migration). The first such migration (= file ops,
step 3) is the deciding wave; recommendation defaults from those
Open Questions are applied unless the wave surfaces conflicting
requirements.

### Phase M4: Cleanup (~2-3 days)

- Remove the `router_tools.py` `ToolSpec` list and all inline tool dict
  literals; `build_tools()` body is now only registry-driven.
- Remove `OP_KIND_MODEL_MAP` or retain as a `Mapping[str, type[BaseModel]]`
  derived view if Pydantic models are still used internally (see Open
  question 2 in §7).
- Remove `_DISPATCH_KIND` sidecar and the backward-compat alias. Update
  `get_dispatch_kind()` to delegate to `registry[name].gates` or a new
  `dispatch_kind` field on `ToolDefinition`.
- Remove `op_runtime/<kind>.py` handler functions that have moved to
  `tools/<kind>.py`. Keep `op_runtime/context.py` (`OpContext`) and shared
  utilities that `ToolContext` or handlers still reference.
- Update `CLAUDE.md` hard rule to name `src/reyn/tools/` as the location
  for new capabilities.
- Update `CHANGELOG` with the architectural change.
- Update `docs/concepts/llm-invocation-surfaces.md` Section 9 (or a new
  section) with "Implementation: unified registry" content noting that the
  dual-implementation architecture described in the document is the
  historical state, and the registry is the current implementation.

---

## 6. Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| LLMReplay fixture drift | medium | high (every fixture re-record) | byte-identity verify gate at M2; adjust `render_for_router()` until output is identical before proceeding |
| Pydantic model coexistence | medium | medium | M1 co-registers existing `IROp` models alongside `ToolDefinition`; M4 cleanup decides whether to derive from JSON schema or keep co-registered |
| ToolContext design mismatch | medium | high (showstopper) | M2 POC catches early; explicit stop signal; fallback to Alternative A documented |
| Permission resolver path change | low-medium | medium | M1 keeps `PermissionResolver` unchanged; `ToolContext` delegates the same way `OpContext` does today |
| Migration coexistence breakage | high | medium-high | every commit must pass the full test suite; capability migrations are independent and can be reverted individually |
| Existing skill `allowed_ops` migration | high | medium | hybrid prefix-wildcard + deprecation warning preserves backward compat; explicit migration deferred to a later minor release |
| Test count growth | low | low | ~60 new Tier 2 invariants across 13 capabilities (approximately 5 per capability); expected and proportionate |
| Showstopper at M2 | 10-15% estimate | high | explicit stop signal; invested M1 work has documentation value (registry types + adapter shims) regardless of M2 outcome |
| Gate misconfiguration for Type B asymmetries | low | medium | Tier 2 invariant asserts that `shell`, `lint`, `ask_user` have `router=deny`; `delegate_to_agent`, `plan`, `reyn_src_*` have `phase=deny` |

---

## 7. Open questions

Questions that remain open and require design judgement during
implementation. Resolved at the phase indicated.

**1. ToolDefinition file format.** Python instances per file (=
`src/reyn/tools/web_search.py` contains the `ToolDefinition` + handler) vs
a centralized YAML manifest with handler references.

Recommendation: **Python per-file**. Allows type-checking and refactoring
tooling to follow the definition and handler together; mirrors the current
`op_runtime/<kind>.py` pattern. YAML would require a separate loader and
lose static analysis coverage of handler signatures. Resolve at M1.

**2. Pydantic model for args validation.** Keep existing `IROp` Pydantic
classes co-registered with `ToolDefinition` vs generate Pydantic models from
the `parameters` JSON schema dynamically.

Recommendation: **co-register during migration; decide at M4 cleanup.**
Co-registration preserves backward compat with code that currently imports
`FileIROp`, `WebSearchIROp`, etc. directly. If those importers are removed
by M4, dynamic generation from JSON schema becomes viable. If they survive,
co-registration is the safer steady state.

**3. ToolContext field set — universal vs caller-kind-specific.** Fields
that are meaningful only in one caller context (e.g., `chain_id` is router-
specific; `skill_run_id` and `current_phase` are phase-specific) should not
bloat the universal context signature.

Recommendation: **universal fields explicit** (`events`, `workspace`,
`permission_resolver`, `caller_kind`); caller-kind-specific fields accessed
via `ctx.router_state` and `ctx.phase_state` sub-objects, each `None` when
not in the relevant caller context. Handlers that need phase-specific fields
check `ctx.phase_state is not None`. Resolve at M1.

**4. Backward-compat sunset timing.** `OP_KIND_MODEL_MAP` and
`_DISPATCH_KIND` derived aliases are needed during migration. When do they
sunset?

Recommendation: **sunset at M4** plus a deprecation warning at module import
for one minor release before removal, so any external code that imports these
symbols (operator plugins, future third-party integrations) has a migration
window. Resolve at M4.

**5. Documentation auto-generation.** Can `ToolDefinition` metadata
auto-generate `docs/reference/runtime/control-ir.md` and a new
`docs/reference/runtime/router-tools.md`?

Recommendation: **not in M-phase**. Auto-generation adds a build step,
requires doc-rendering logic in the registry or a separate script, and the
payoff depends on documentation maintenance discipline that should be
established with the first few manually-maintained entries. Defer to an M5+
enhancement after the migration stabilizes. Resolve post-M4.

### 6. Naming canonicalization

**Question.** Each capability has divergent names across surfaces today
(= `read_file` router tool vs `file` op + `action: read` phase op). The
unified ToolDefinition requires ONE canonical name. How is it chosen?

**Options.**
- (a) Adopt router-side names as canonical (= `read_file`, `write_file`,
  `list_directory`, `delete_file`, `list_mcp_servers`, etc.). Phase-side
  coarse-grained ops (= `file` with action) get unbundled into
  fine-grained ToolDefinitions. Existing skill phases referring to
  `file` op need migration.
- (b) Adopt phase-side names as canonical (= `file` op with `action`,
  `mcp` op with polymorphic args). Router-side fine-grained tools get
  re-exposed as polymorphic tools with action argument. LLM affordance
  on router side may degrade (= function calling convention prefers
  one-tool-one-purpose).
- (c) Logical-capability layer with surface-specific name aliases.
  ToolDefinition holds a `logical_name` plus per-surface `aliases`.
  Existing surface names preserved. Doctrine-level "1 ToolDefinition =
  1 capability" weakened.

**Recommendation.** Option (a) — adopt router-side fine-grained names.
Reasoning: (i) function calling convention prefers fine-grained tools
for LLM affordance; (ii) Type C closure naturally adopts fine-grained
phase ops; (iii) `allowed_ops: [file_read, file_write]` is more
expressive than `allowed_ops: [file]` for skill-author intent.

**Resolution phase.** M2 POC selects `web_search` which has a 1:1 name
match across surfaces — does not exercise this question. M3 first
file-op migration (= step #3 in capability table) is the resolution
trigger; ADR amendment recording the choice + migration semantics
required before that step.

### 7. `Phase.allowed_ops` semantic migration

**Question.** Today `Phase.allowed_ops: list[str]` lists op kinds at
coarse granularity (= `["file", "ask_user"]` is the default).
Migration to fine-grained ToolDefinitions (= 4 file_* tools instead of
one `file` op) breaks this semantic.

**Options.**
- (a) Prefix-wildcard interpretation: `allowed_ops: [file]` matches
  `file_*` tool prefix. Backward compat without skill-author action.
- (b) Explicit migration: rewrite all stdlib skills' `allowed_ops` to
  enumerate the fine-grained capabilities. ~12 stdlib skills × phases
  to update. Cleaner long-term but high migration cost.
- (c) Hybrid: prefix-wildcard preserved for one minor release, with
  deprecation warning emitted at lint time when a coarse-grained name
  is used; explicit form required from version N+1.

**Recommendation.** Option (c) — hybrid with deprecation. Preserves
backward compat without locking the project into permanent ambiguity.
The deprecation message points skill authors at the migration path.

**Resolution phase.** Same trigger as Question #6 — M3 first
file-op migration. Lint message wording + deprecation timeline
recorded in ADR amendment at that point.

---

## 8. Consequences

### Positive

- **Drift structurally impossible.** A single `ToolDefinition` is the only
  place a capability is described; both surfaces derive from it. Description
  divergence requires actively working against the design.
- **New capability cost halved.** Write one `ToolDefinition` in one file;
  both router and phase surfaces are available immediately (subject to
  `ToolGates`). Today this requires: `ToolSpec` in `router_tools.py`,
  `IROp` Pydantic model in `schemas/models.py`, entry in `OP_KIND_MODEL_MAP`,
  handler in `op_runtime/<kind>.py`, and dispatch registration — four
  separate files.
- **Type C gaps close as side effect.** Memory write phase-side, catalog
  browse phase-side, MCP discover phase-side — all three close by setting
  `phase=allow` during M3. No separate PRs. No separate design work.
- **Future tool metadata has a home.** `cost_weight`, `rate_limit_class`,
  `log_redaction`, and any operator-level per-capability policy declarations
  have a single canonical field location.
- **Role gating is an explicit declaration, not convention.** `shell` having
  `router=deny` is a machine-readable field, not a comment in a doc. The
  constraint is enforced at dispatch time and verified by Tier 2 invariants.

### Negative

- **~3-week implementation cost upfront.** M1–M4 with the migration order
  in §5 is a substantial but time-bounded refactor.
- **Coexistence period.** During M1–M3, both the old `ToolSpec` / `IROp`
  paths and the new `ToolDefinition` path are active. This adds short-term
  complexity to `build_tools()` and `ControlIRExecutor`.
- **POC stop signal risk.** If M2 halts, one week of M1 infrastructure work
  is spent. That work has documentation value (the registry types exist,
  the adapter shims exist) but no shipped product improvement. The 10-15%
  showstopper estimate is an explicit acknowledgment of this cost.

### Neutral

- **LLM behavior unchanged.** Both protocols (function calling and JSON
  output Control IR) stay byte-identical. Prompts, tool schemas, and
  phase instructions are unaffected.
- **All existing tests continue to pass at each migration commit.** No test
  deletions; new Tier 2 invariants are additive.
- **Plan mode, Skill DSL, preprocessor, postprocessor unchanged.** This ADR
  is scoped to the tool/capability layer only. The phase execution engine,
  skill authoring contract, and plan decomposition logic are not touched.

---

## 9. Acceptance criteria

### M1 deliverables — **completed (commit `edd4c1b`)**

- [x] registry, ToolDefinition, ToolGates, ToolContext, dispatch.py
- [x] adapter shims (build_tools / OP_KIND_MODEL_MAP migration-note docstrings)
- [x] 27 Tier 2 invariants

### M2 POC deliverables — **completed (commit `367b41c`)**

- [x] web_search migrated as POC
- [x] zero LLMReplay fixture re-recording (= byte-identity gate green)
- [x] +14 Tier 2 invariants
- [x] drift test green
- [x] ToolContext design validated for web_search shape

### M3 Wave 1 + Wave 2 — **completed (commits `ba4c5fe`, `66435d1`)**

- [x] 24 additional capabilities migrated (= 25 total + invoke_skill)
- [x] All 3 Type C gaps closed declaratively (= memory I/O, catalog browse, MCP discover)
- [x] Open Q #6 (naming) and #7 (allowed_ops) doctrine resolutions applied
- [x] +226 Tier 2 invariants
- [x] LLMReplay fixtures preserved
- [x] Sanity check via live `reyn web` A2A endpoint passed (= no real-LLM regression)

### M4 Phase 1 — **completed (commit `66a068e`)**

- [x] 18 router_tools.py inline `ToolSpec` literals (= the static-schema
      tools) replaced with registry consumption via `render_for_router()`
- [x] 2 inline literals (`invoke_skill`, `delegate_to_agent`) deferred to
      Phase 3 step 1 because of dynamic enum injection requirements

### M4 Phase 2 — **completed (commit `a86a246`)**

- [x] `RouterCallerState` / `PhaseCallerState` typed sub-objects on
      `ToolContext` (Open Q #3 resolved at the structure level)
- [x] All fields default to `None` for gradual population
- [x] +7 Tier 2 invariants

### M4 Phase 3 step 1 — **completed (commit `37ea8e5`)**

- [x] 6 `NotImplementedError` design-revisit stubs activated
      (catalog ×4 + `delegate_to_agent` + `plan`); they delegate via the
      typed `RouterCallerState` callable fields
- [x] `RouterCallerState` gains 4 catalog `_fn` callable fields
- [x] `ToolDefinition.schema_enricher` per-call hook + `render_for_router`
      accepts `state=...` to invoke the enricher
- [x] Last 2 inline `ToolSpec` literals (`invoke_skill`,
      `delegate_to_agent`) migrated to registry consumption with
      `schema_enricher` injecting per-session enums (= Phase 1 closeout)
- [x] +29 Tier 2 invariants
- [x] LLMReplay byte-identity preserved (existing test_router_tools.py
      tests pass without modification)

### M4 Phase 3 step 2 — **completed (commit `649a426`)**

- [x] `RouterLoop._invoke_router_tool` dispatches the 6 activated tools
      through `invoke_tool(get_default_registry(), ...)` instead of the
      legacy if/elif tree (= the architectural goal: handlers live in one
      place, dispatcher is thin)
- [x] `RouterLoop._build_router_caller_state` populates a
      `RouterCallerState` with catalog `_fn` callables, `send_to_agent` /
      `dispatch_plan_tool` with session state pre-bound, and forward-
      looking fields (available_skills / available_agents / chain_id /
      budget / router_model / available_tool_names) for schema_enricher
      consumers
- [x] Catalog list-handler return shape relaxed to bare list (= LLMReplay
      byte-identity with legacy router branches preserved)
- [x] Legacy A1–A4 / B2 / G branches in `_invoke_router_tool` removed
- [x] 1754 passed / 2 xfailed (no net change; byte-identity verified)

### M4 Phase 4 step 1 — **completed (commit `ebe5786`)**

- [x] `_DISPATCH_KIND` sidecar dict / `_TOOL_SPECS_STATIC_ASYNC` removed
      from `router_tools.py`; `get_dispatch_kind(name)` now consults
      `get_default_registry().lookup(name).dispatch_kind` directly. The
      registry's `ToolDefinition.dispatch_kind` is canonical.
- [x] `tests/test_plan_async_dispatch.py` updated to use
      `get_dispatch_kind()` only (= no direct sidecar access)
- [x] `planner.py` / `router_loop.py` comment references to
      `_DISPATCH_KIND` updated to point at the helper / registry

### M4 Phase 3.5 router-side cluster activations — **completed**

The 18 remaining router tools dispatched via the legacy if/elif tree
in `RouterLoop._invoke_router_tool` migrated cluster-by-cluster to
unified registry dispatch. Per-tool design issues identified in the
migration audit (= shape mismatch / state propagation / permission
gating) addressed via three bridge patterns on `RouterCallerState`.

- [x] **Phase 3.5-D — reyn_src + web (commit `0093667`)** — zero-diff
      handlers (= already byte-equivalent to legacy router branches);
      added to `_REGISTRY_DISPATCH_TOOLS`.
- [x] **Phase 3.5-A+C — file cluster (commit `2b1fe8d`)** — 4 tools
      (read_file / write_file / delete_file / list_directory).
      `RouterCallerState.op_context_factory` bound to public
      `host.make_router_op_context` (renamed from `_make_router_op_context`)
      so file handlers receive operator-declared PermissionDecl +
      Workspace. `_normalise_router_tool_result` unwraps read_file
      `{...,content,...}` → bare string and list_directory
      `{...,entries,...}` → bare list to preserve LLM-visible shape.
- [x] **Phase 3.5-B-light — invoke_skill (commit `3378051`)** —
      `RouterCallerState.run_skill_fn` callable bridge bound with
      chain_id pre-applied so PR14 multi-hop chain semantics propagate
      into nested run_skill / delegate_to_agent paths. Defense Layer B
      (skill-name validation) ported to handler.
- [x] **Phase 3.5-B-mid — mcp cluster (commit `a58c685`)** — 3 tools.
      `RouterCallerState.host: Any` field added as a duck-typed
      RouterHostAdapter reference; MCP handlers preserved their
      original `ctx.router_state.host.mcp_*` access pattern with the
      session-level MCPClient cache intact (= no per-call re-handshake).
      `_normalise_router_tool_result` extended for list_mcp_servers /
      list_mcp_tools dict-envelope unwrap.
- [x] **Phase 3.5-B-heavy — memory cluster (commit `7482b33`)** — 5
      tools. `RouterCallerState.{list_memory_fn, read_memory_body_fn,
      remember_fn, forget_fn}` callable bridges bound to RouterLoop's
      private helpers which consume `host.get_memory_index()` (=
      agent-aware combined index), preserving per-agent memory privacy
      that the registry handlers' filesystem-direct fallback couldn't
      guarantee.

After Phase 3.5, `RouterLoop._invoke_router_tool` is a thin top-branch
(= registry dispatch via `_invoke_via_registry`) plus a placeholder
comment for future clusters; all 24 router-active ToolDefinitions
exercise their canonical `src/reyn/tools/<name>.py` handler in
production. LLMReplay byte-identity preserved end-to-end (= 1754
passed / 2 xfailed across all 5 cluster migrations, no fixture
re-recording).

**Note on ADR status:** the status remains "Proposed" because phase-side
migration is the closing work. Promotion to "Accepted" is gated on the
remaining items below, which involve design decisions (phase-side
dispatch consuming registry; obsolete `op_runtime/<kind>.py`
consolidation; `allowed_ops` prefix-wildcard semantics).

**The ADR is closed (= Accepted) when:**

- **Phase 4 step 2 — phase-side dispatch consumes registry.**
  `ControlIRExecutor` switches from `OP_KIND_MODEL_MAP` lookup to
  `get_default_registry().for_phase()`. `allowed_ops` prefix-wildcard
  semantics (= `["file"]` matches `read_file` / `write_file` / etc) lands
  in the phase dispatcher.
- **Phase 4 step 3 — alias sunset.** `OP_KIND_MODEL_MAP` removed
  (Open Q #2). Obsolete `op_runtime/<kind>.py` handler files removed or
  consolidated into `src/reyn/tools/<name>.py`.
- `docs/concepts/llm-invocation-surfaces.md` updated to reflect the
  unified registry as the implementation (= rolling updates land with
  each phase commit; final Accepted update describes the steady state).
- `CHANGELOG` records the architectural change.

---

## 10. References

- `docs/concepts/llm-invocation-surfaces.md` — the doctrine doc this ADR
  resolves; commit `1423f85`.
- `docs/concepts/principles.md` — P3, P4, P5, P6, P7 invariants preserved
  by this design.
- ADR-0001 (`0001-state-model-wal-snapshot.md`) — prior structural ADR;
  format and consequence structure reference.
- Commit `8af3444` — `web_search` DuckDuckGo hint added to `router_tools.py`
  `ToolSpec`; exemplifies the coordinated-edit cost of the current dual
  implementation.
- Commit `77d6db6` — `ToolSpec` dataclass introduced; `_DISPATCH_KIND`
  preserved as derived alias for backward compat; partial step toward this ADR.
- Commit `1423f85` — `llm-invocation-surfaces.md` concept doc; the doctrine
  analysis that identified the dual-implementation drift this ADR closes.
- `src/reyn/chat/router_tools.py` — current `ToolSpec` / `build_tools()` /
  `get_dispatch_kind()` shape (= today's PR-I baseline).
- `src/reyn/op_runtime/registry.py` — `OP_KIND_MODEL_MAP` (8 op kinds) +
  `OP_PURITY` classification.
- `src/reyn/op_runtime/web.py` — `handle_web_search` / `handle_web_fetch`;
  the M2 POC handler shape that `ToolHandler` must accommodate.
- `src/reyn/kernel/control_ir_executor.py` — `_build_phase_tool_catalog`;
  the shape `render_for_phase()` must produce.
