---
type: concept
topic: architecture
audience: [human, agent]
---

# LLM invocation surfaces — the router-style tool contract

> **Status: partially stale.** This page originally compared two invocation kinds:
> the chat router (function-calling tools) and the phase executor inside a
> now-deleted workflow engine (a JSON `control`/`artifact`/`control_ir` output
> contract, deleted in a later engine-deletion arc. The phase-style surface and every
> section that compared against it (the capability matrix, the four divergence
> types, the doctrine options for closing router/phase gaps) described a
> comparison that no longer has two sides — confirmed via direct grep that
> `OSRuntime` and the `control`/`artifact`/`control_ir` envelope do not exist in
> current source. Those sections have been removed. Section 4 (the unified
> `ToolRegistry` implementation log) remains accurate — it documents the still-
> current architecture — except that its `gates(phase=...)` references are now
> vestigial (no phase surface consumes them). §2.1's tool-inventory list (the
> "13 always-present + conditional, 13–22 tools" description) was **also stale
> and has been corrected** — it described the pre-FP-0034 per-kind tool surface,
> not the universal-action-catalog wrapper mode that has been the sole
> production behaviour since Phase 6 (2026-05-16), confirmed via
> `docs/concepts/tools-integrations/universal-catalog.md` and
> `src/reyn/runtime/router_tools.py`'s `_LEGACY_TOOL_NAMES` strip list plus
> `ActionRetrievalConfig.universal_wrappers_enabled: bool = True` (the
> production default).

## 1. Why this matters

Reyn invokes the LLM via native function-calling tools over `RouterLoop` (interactive chat sessions), assembled per-context by a `RouterLoopHost` facade. This document names that invocation surface and its tool inventory.

---

## 2. The router invocation surface

### 2.1 Router-style (chat)

**Used by:** `RouterLoop` (interactive chat sessions), with a `RouterLoopHost` facade that narrows the catalog per context.

**Mechanism:** native LLM function calling via `call_llm_tools` (backed by litellm). Tool definitions follow the OpenAI `tools` array shape; the model replies with `tool_calls` in the assistant message. The OS dispatches each call, appends the `tool_result`, and re-invokes the LLM until it produces a plain text reply.

**Tool surface:** `build_tools()` in `src/reyn/runtime/router_tools.py` assembles the tool list, still returning the OpenAI `tools` array shape, but the *production-default* shape of that list is now the universal action catalog, not a flat per-kind tool list — see [Universal Action Catalog](../tools-integrations/universal-catalog.md) for the full model. In production default config (`action_retrieval.universal_wrappers_enabled: true`, the default since FP-0034 PR-3b-iv):

- **The 3–4 universal wrappers** (`list_actions`, `describe_action`, `invoke_action`, plus `search_actions` when `action_retrieval.embedding_class` is configured and its index is ready) address every category — skill, peer agent, MCP, file, web, memory, RAG corpus, sandboxed exec — through one qualified-name dispatch pattern (`<category>__<entry>`), rather than a separate tool per kind.
- **Legacy per-kind tools are stripped** from `tools=` in this mode (`router_tools.py`'s `_LEGACY_TOOL_NAMES` set) — `list_agents`, `describe_agent`, `delegate_to_agent`, `list_memory`, `read_memory_body`, `remember_shared`, `remember_agent`, `forget_memory`, `recall`, `read_file`, `write_file`, `delete_file`, `list_directory`, `web_search`, `web_fetch`, `list_mcp_servers`, `list_mcp_tools`, `call_mcp_tool`, and more no longer appear in the LLM-visible tool list; their handlers remain registered as the wrappers' backing implementations, dispatched via `universal_dispatch.py`.
- **Optional hot-list direct aliases** may be appended on top of the wrappers for frequently-used actions (`hot_list_aliases`), bypassing the discover step for those specific actions only.
- **Operator opt-out**: setting `action_retrieval.universal_wrappers_enabled: false` restores the pre-FP-0034 flat per-kind tool list (the shape this section used to describe unconditionally) — this is a config escape hatch, not the default operator experience.

**Role:** orchestration — pick the next sub-component (workflow, agent, plan, memory operation, direct text reply).

---

## 3. See also

- [../architecture/care-boundary.md](../architecture/care-boundary.md) — what Reyn does and does not own
- [../../reference/runtime/control-ir.md](../../reference/runtime/control-ir.md) — the OS-dispatched op vocabulary
- [../../reference/cli/chat.md](../../reference/cli/chat.md) — slash commands available in chat (sometimes confused with router tools; they are distinct)
- [../../reference/cli/mcp.md](../../reference/cli/mcp.md) — MCP server side (Reyn-as-MCP-server exposes a third surface that is NOT covered here because it is external clients calling INTO Reyn, not Reyn's internal LLM invocation surface)

---

## 4. Implementation: unified registry (ADR-0026 Accepted)

The dual-implementation architecture this ADR closed (two separate catalogs:
`router_tools.py` / `OP_KIND_MODEL_MAP`, back when a phase-side surface existed
too) is the historical baseline. ADR-0026 closes the structural drift by
introducing a single `ToolDefinition` per capability with two render methods
(one of which — the phase-side render — is now vestigial per the status note
above).

**M1 (landed — commit `edd4c1b`):** The infrastructure module `src/reyn/tools/` is in place:

- `ToolDefinition`, `ToolGates`, `ToolContext`, `ToolHandler`, `ToolResult` — in `src/reyn/tools/types.py`
- `ToolRegistry` — in `src/reyn/tools/registry.py`
- `invoke_tool`, `ToolNotFound`, `ToolGateRefused` — in `src/reyn/tools/dispatch.py`

**M2 POC (landed — commit `367b41c`):** `web_search` is the first capability
migrated to the unified registry. `src/reyn/tools/web_search.py` contains the
`WEB_SEARCH` `ToolDefinition` instance and a thin adapter wrapping the legacy
`handle_web_search` handler. `build_tools()` now derives `web_search` from the
registry via `render_for_router()`, producing byte-identical output to the prior
`ToolSpec` literal (LLMReplay fixtures unchanged). All M2 verification gates
passed: byte-identity GREEN, drift test GREEN, full suite 1500 passed / 2
xfailed, mkdocs strict empty.

**M3 Wave 1 (landed — commit `ba4c5fe`):** 7 capabilities migrated:
`web_fetch`, `shell`, `lint`, `ask_user`, `delegate_to_agent`, `plan`,
`reyn_src_list`, `reyn_src_read`. `ToolDefinition` gains a `dispatch_kind`
field. +99 Tier 2 invariants.

**M3 Wave 2 (landed — commit `66435d1`):** 17 capabilities migrated —
file ops × 4 / MCP ops × 3 / memory ops × 5 / catalog ops × 4 /
`invoke_skill`. All 3 Type C convention-drift gaps identified in §4 are
declaratively closed via `gates(router=allow, phase=allow)`: memory write
phase-side, catalog browse phase-side, MCP discover phase-side. +127 Tier 2
invariants. LLMReplay fixtures preserved across all migrations. Sanity check
via live `reyn web` A2A endpoint confirmed zero real-LLM regression.

All 13 capability clusters (= 26 ToolDefinitions) are registered in the unified
ToolRegistry. Type C convention-drift gaps identified in §4 are declaratively
closed via `gates(router=allow, phase=allow)`. Phase-side Control IR dispatch
wiring to consume the registry is M4 cleanup work.

**M4 Phase 2 (landed):** ToolContext expansion — `router_state` and `phase_state`
are now typed sub-objects (`RouterCallerState` / `PhaseCallerState`) instead of
loose `Any`, resolving ADR-0026 Open Question #3. All fields default to `None`
for gradual migration. +7 Tier 2 invariants.

**M4 Phase 3 step 1 (landed):** handler activation + per-call schema enrichment
hook. The 6 design-revisit `NotImplementedError` stubs (4 catalog +
`delegate_to_agent` + `plan`) are activated to delegate via the typed
`RouterCallerState` callable fields. `RouterCallerState` gains 4 new callable
fields (`list_skills_fn`, `describe_skill_fn`, `list_agents_fn`,
`describe_agent_fn`). `ToolDefinition` gains an optional `schema_enricher` hook
invoked by `render_for_router(state=...)` to inject per-session dynamic data
(canonical use: `invoke_skill.name` / `delegate_to_agent.to` enums). The 2
remaining inline `ToolSpec` literals in `router_tools.py` (= `invoke_skill` +
`delegate_to_agent`) are migrated to registry consumption with the new hook,
preserving byte-identity. Mis-wiring contract: handlers raise `RuntimeError`
with a descriptive message when the dispatcher fails to populate the required
callable. +29 Tier 2 invariants. 1754 passed / 2 xfailed.

**M4 Phase 3 step 2 (landed — commit `649a426`):**
`RouterLoop._invoke_router_tool` dispatches the 6 activated tools (catalog
×4 + `delegate_to_agent` + `plan`) through `invoke_tool(get_default_registry(), ...)`
instead of the legacy if/elif tree. `RouterLoop._build_router_caller_state`
populates a `RouterCallerState` with bound callbacks. Catalog list-handler
return shape relaxed to bare list (= LLMReplay byte-identity preserved).
Legacy A1–A4 / B2 / G branches in `_invoke_router_tool` removed.

**M4 Phase 4 step 1 (landed):** `_DISPATCH_KIND` sidecar dict /
`_TOOL_SPECS_STATIC_ASYNC` removed from `router_tools.py`;
`get_dispatch_kind(name)` consults `ToolDefinition.dispatch_kind` from the
registry directly. The registry is now the canonical source for both schema
rendering AND dispatch posture classification.

**M4 Phase 3.5 (landed — 5 commits `0093667` / `2b1fe8d` / `3378051` /
`a58c685` / `7482b33`):** router-side cluster activations complete.
All 18 remaining tools (file ×4 / mcp ×3 / memory ×5 / web ×2 /
reyn_src ×2 / `invoke_skill`) now dispatch through
`invoke_tool(get_default_registry(), ...)`.  Per-tool design issues
identified in the migration audit were addressed with three bridge
patterns on `RouterCallerState`:

1. **`op_context_factory: Callable | None`** — RouterLoop binds
   `host.make_router_op_context` so file / mcp / web handlers receive
   the operator-declared PermissionDecl + Workspace, matching the
   legacy router branch.
2. **`host: Any`** — duck-typed RouterHostAdapter reference for MCP
   handlers that preserve the session-level MCPClient cache.
3. **Per-tool callable bridges** (`run_skill_fn`, `list_memory_fn`,
   `read_memory_body_fn`, `remember_fn`, `forget_fn`) — bound to
   RouterLoop's private helpers so chain_id propagation
   (`invoke_skill`) and agent-aware memory paths (memory cluster) are
   preserved.

`RouterLoop._invoke_router_tool` is now a thin top-branch (registry
dispatch) plus a comment placeholder for future clusters.
`_normalise_router_tool_result` adapts handler return shapes (= dict
envelopes from op_runtime synthesis) back to the bare-string /
bare-list shapes the legacy router branches emitted to the LLM,
preserving LLMReplay byte-identity end-to-end through all 5 cluster
migrations.

**M4 Phase 4 (landed):** phase-side migration completes the architectural
goal.

- **Phase 4 step 1 (commit `ebe5786`)** — `_DISPATCH_KIND` sidecar dict
  removed; `get_dispatch_kind()` reads `ToolDefinition.dispatch_kind`
  from the registry.
- **Phase 4 step 2** — coarse-name `FILE_OP` / `MCP_OP` / `RUN_SKILL_OP`
  ToolDefinitions registered with `gates(phase="allow")` so phase
  Control IR `kind` values map 1:1 to registry entries.
  `ControlIRExecutor.execute()` dispatches via
  `invoke_tool(get_default_registry(), op.kind, ...)`. Catalog building
  (`_build_phase_tool_catalog`) reads schemas from the registry.
- **Phase 4 step 3** — `OP_KIND_MODEL_MAP` retained as the op-kind
  reference (= linter `ALL_OP_KINDS`, `OP_PURITY` coverage); no longer
  consulted at dispatch time. `op_runtime/<kind>.py` handlers retained
  as the shared implementation that registry handlers delegate to.
- **`is_op_allowed` helper** — prefix-wildcard membership for coarse-name
  `allowed_ops` declarations matching fine-grained `op.kind` values.

**#1240 (the 2-axis tool-model pivot) superseded the coarse phase-side
dispatch.** The phase catalog and Control IR now use the fine-grained
chat-tools subset directly:

- **Catalog axis** — the coarse `FILE_OP` / `MCP_OP` / `RUN_SKILL_OP`
  phase ToolDefinitions were dropped; phases advertise the fine file kinds
  (`read_file` … `grep_files`) plus `invoke_skill` / `call_mcp_tool` (the
  chat-tool names, aliased back to the `run_skill` / `mcp` kinds at the
  parse boundary). `OP_KIND_MODEL_MAP` now holds the fine file kinds; the
  coarse `"file"` kind was removed (the `FileIROp` model survives only as
  the shared execution backend).
- Phase `allowed_ops` defaults migrated to the fine kinds, so phase
  Control IR now emits fine kinds — the earlier "still emits coarse kinds
  today" caveat no longer holds.

**Tool addition cost** at the steady state: 1 file in
`src/reyn/tools/<name>.py` + 1 register call in `__init__.py` = 2 touch
points for a router-or-phase tool. New phase-side coarse op kinds
additionally need an `OP_KIND_MODEL_MAP` entry (linter / purity
coverage) and a Pydantic `IROp` model in `schemas/models.py` =
3-touch-point budget for a fully phase-eligible new kind. This is the
baseline future tool-scope expansion amortises against.

ADR-0026 is now **Accepted**.

**Cross-reference:** [../../deep-dives/decisions/0026-unified-tool-registry.md](../../deep-dives/decisions/0026-unified-tool-registry.md)
