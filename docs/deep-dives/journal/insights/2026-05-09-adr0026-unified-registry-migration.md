---
title: ADR-0026 Unified Tool Registry — bridge patterns for byte-identity-preserving multi-cluster refactor
discovered: 2026-05-09
session-context: ADR-0026 M1→M4 Accepted (14-commit arc), tool-addition cost from 3-5 touch points to 2-3
related-commits: [edd4c1b, 367b41c, ba4c5fe, 66435d1, 66a068e, a86a246, 37ea8e5, 649a426, ebe5786, 0093667, 2b1fe8d, 3378051, a58c685, 7482b33, 9620310]
related-giveup: []
related-memory: []
status: stable
---

# ADR-0026 Unified Tool Registry — bridge patterns for byte-identity-preserving multi-cluster refactor

## 1. Context

Reyn invokes the LLM in two structurally distinct contexts: **router-style**
(native function calling via `RouterLoop` + `build_tools()`) and **phase-style**
(JSON output Control IR via `ControlIRExecutor`). Before this work, each
capability lived in two parallel implementations — `ToolSpec` literals in
`router_tools.py` for router exposure, `OP_KIND_MODEL_MAP` + `op_runtime/<kind>.py`
for phase exposure. Adding a tool meant 3-5 coordinated touch points (= the
ADR called this the *dual-implementation drift*).

ADR-0026 closed the drift via a single `ToolDefinition` per capability with
two render methods (`render_for_router` / `render_for_phase`), held in a
unified `ToolRegistry`. The closing migration spanned 14 commits across M1
(infrastructure) → M4 Phase 4 (phase-side dispatch). The novel discipline that
made it work is what this insight records.

## 2. The byte-identity invariant

**Refactor scope: external behavior unchanged.** Every cluster migration
preserved LLM-visible output byte-for-byte (= router tool_result envelopes,
phase Control IR results, `_DISPATCH_KIND` semantics, prompt cache identity).
LLMReplay fixtures recorded against pre-migration handlers continued to match
post-migration handlers across all 14 commits, with **zero fixture
re-recording**.

This wasn't a happy accident. It was the migration *gate*: a cluster commit
that broke fixture identity rolled back. The discipline forced finding the
real shape mismatch before landing — not after dogfood spotted it.

## 3. Three bridge patterns on `RouterCallerState`

The 18 router tools migrated in Phase 3.5 needed access to session-scoped
state that wasn't on `ToolContext` (= chain_id, MCPClient cache, agent-aware
memory paths, per-call permissions). Three patterns emerged, each appropriate
for a different shape of state:

### Pattern A — `op_context_factory: Callable | None`

For handlers that delegate to `op_runtime/<kind>.py` and need the operator-
declared `PermissionDecl` + `Workspace`. RouterLoop binds
`host.make_router_op_context` (= the same factory the legacy router branches
used). When unset (= test stubs, phase-side), handlers fall back to minimal
synthesis. Used by file (4 tools), mcp (3 tools), web (2 tools).

**Reuse signal**: any handler that needs an *already-built* OpContext-style
container with permission state.

### Pattern B — `host: Any` duck-typed reference

For handlers that need to call multiple host methods preserving session-level
shared state (= MCPClient cache that must NOT re-handshake per call).
RouterLoop binds the full `RouterHostAdapter` instance. Handlers call
`ctx.router_state.host.mcp_*()` directly. Used by mcp (3 tools).

**Reuse signal**: handler needs *more than one method on the same stateful
object*, and the state lifecycle is session-scoped.

### Pattern C — Per-tool callable bridges

For handlers where binding session-scoped context (= `chain_id`, agent
identity) at population time is cleaner than threading it through every call.
`run_skill_fn` (= chain_id pre-bound), `list_memory_fn` / `read_memory_body_fn`
/ `remember_fn` / `forget_fn` (= agent-aware memory paths via host's
precomputed index). Handler signature stays pure (= just LLM-emitted args).

**Reuse signal**: a single method's call signature is *closed* (no peer
methods needed) and session state can be `functools.partial`-bound at wiring
time.

## 4. The shape adapter at the dispatch boundary

Some registry handlers return op_runtime dict envelopes (e.g.
`{"kind": "file", "op": "read", "status": "ok", "content": "..."}`) but the
legacy router branches returned the bare `content` string (= the host adapter
extracted before returning). Migrating the handlers to match would break
phase-side tests. Migrating the legacy shape to envelope would break
router-side LLM prompt cache.

Resolution: **`RouterLoop._normalise_router_tool_result(name, result)`**
unwraps known envelopes back to legacy shapes for router dispatch only. Phase-
side dispatch (= `ControlIRExecutor`) sees the raw envelope, which matches
the prior `execute_op` return.

The pattern: **adapt at the dispatch boundary, not inside the handler**.
Handlers stay surface-agnostic; the dispatcher knows its surface's contract
and adapts accordingly. This generalised to file's `read_file` / `list_directory`
and mcp's `list_mcp_servers` / `list_mcp_tools`.

**Reuse signal**: when migrating a handler whose return shape differs
between two callers, place the shape adapter at each caller's edge — not in
the handler. Handlers stay invariant; adapters absorb the surface diversity.

## 5. The schema_enricher hook for per-call dynamic schemas

Two router tools needed dynamic enums populated per-session: `invoke_skill.name`
from available_skills, `delegate_to_agent.to` from available_agents. Static
ToolDefinition schemas couldn't express this without coupling the registry to
session state.

Resolution: **`ToolDefinition.schema_enricher: Callable | None`** — when set,
`render_for_router(state=...)` post-processes the static rendered dict by
calling `enricher(rendered, state)`. The enricher receives RouterCallerState
and returns a NEW dict with dynamic enrichment applied. Static schema stays
canonical; per-call data is hook-injected.

**Reuse signal**: any time a tool's exposed schema depends on per-session
data the registry shouldn't statically know about. Use the hook to keep the
ToolDefinition pure.

## 6. The coarse-to-fine prefix-wildcard pattern (`is_op_allowed`)

Phase Control IR currently emits coarse `op.kind` values (= `file`, `mcp`,
`run_skill`). Router-side migrated to fine-grained names (= `read_file`,
`write_file`, `call_mcp_tool`, `invoke_skill`). When phase-side eventually
migrates to fine-grained kinds in a future phase, existing skill frontmatter
declarations like `allowed_ops: ["file"]` must continue to allow the new
`read_file` / `write_file` / etc. kinds — without forcing skill authors to
edit every skill.

Resolution: `op_runtime/registry.py::is_op_allowed(op_kind, allowed_ops)` —
direct match (= legacy 1:1) OR prefix-wildcard via a `COARSE_TO_FINE` table.
Forward-looking: phase still emits coarse today, so the wildcard branch is
exercised only by tests; the rule is in place to absorb the future migration
without backward-compat work.

**Reuse signal**: when a naming scheme is migrating from coarse → fine, ship
the wildcard rule first (= forward-looking, no behavioral change today) so
the schema migration later doesn't carry both a dispatch change AND a
declaration migration.

## 7. The 14-commit migration cadence

Cluster-by-cluster commits (= D / A+C / B-light / B-mid / B-heavy in Phase
3.5) each landed:

1. **Bridge addition** to `RouterCallerState` (= 1-4 typed fields)
2. **Handler activation** for the cluster's tools (= delegation through the new
   bridge, fallback to minimal synthesis for unwired sites)
3. **`_REGISTRY_DISPATCH_TOOLS` expansion** + legacy if/elif branch removal
4. **Test verification** (= 1754 passed / 2 xfailed sustained across all
   commits) before commit

Commits never combined unrelated clusters. When Wave 2c sonnet conflated
delegate handler activation with `NotImplementedError` preservation, the
cleanup commit unwound the cross-cutting and unified the convention. The
discipline: **one commit, one cluster, one verifiable green**.

The branch from `RouterLoop._invoke_router_tool`'s if/elif tree to a
top-branch registry dispatch wasn't a single rewrite — it was 6 incremental
shifts (= each cluster's branch removed when its handler became registry-
served). The interim states were always shippable.

## 8. Outcome

- 26 ToolDefinitions registered in the unified ToolRegistry (= 13 capability
  clusters); 3 additional coarse-name phase-only entries (`FILE_OP` / `MCP_OP`
  / `RUN_SKILL_OP`).
- Both router and phase surfaces dispatch via
  `invoke_tool(get_default_registry(), name, args, ToolContext)`.
- `_DISPATCH_KIND` sidecar removed (Phase 4 step 1). `OP_KIND_MODEL_MAP`
  retained as the coarse-kind reference (= linter `ALL_OP_KINDS`,
  `OP_PURITY` coverage); no longer consulted at dispatch time.
- LLMReplay byte-identity preserved across all 14 commits (= zero fixture
  re-recording).
- 1452 → 1754 tests passed / 2 xfailed (= +302 net new tests).
- Tool-addition cost: 3-5 → **2-3 touch points** at the steady state.

## 9. Reuse checklist (for the next migration)

When the next architectural unification arrives, reach for these tools:

- [ ] Identify the **byte-identity invariant** before starting (= what
      external surface must NOT change). Make it the gate for every commit.
- [ ] Audit per-tool / per-capability gaps (= shape mismatch, state
      propagation, permission gating, defense layers). Categorise by *fix
      shape*, not by *capability*.
- [ ] Choose the bridge pattern per category:
  - factory bound to a host method when the handler needs an opaque
    pre-built container → **Pattern A**
  - duck-typed reference when the handler needs multiple peer methods on a
    single stateful object → **Pattern B**
  - per-method callable when the call signature is closed and session state
    can be partial-bound → **Pattern C**
- [ ] Place shape adapters at the **dispatch boundary**, not in handlers.
- [ ] If a tool's schema depends on per-session data, use a **hook on
      ToolDefinition** (= schema_enricher pattern) to keep the ToolDefinition
      pure.
- [ ] Migrate **cluster-by-cluster**, never crossing cluster boundaries in
      one commit. Each commit must land green.
- [ ] When migrating a naming scheme, **ship the wildcard rule first**
      (forward-looking, no behavior change) before the declaration migration.

## References

- ADR-0026 (Status: Accepted, 2026-05-09):
  `docs/deep-dives/decisions/0026-unified-tool-registry.md`
- Concept doc: `docs/concepts/llm-invocation-surfaces.md` §9
- Feature-verify (M3 sanity check):
  `docs/deep-dives/journal/feature-verify/2026-05-09-adr0026-m3-sanity-check.md`
- Session resume (memory):
  `~/.claude/projects/.../memory/session_resume_2026_05_09.md`
