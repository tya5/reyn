# ADR-0026 M2 POC — Success findings (2026-05-09)

## Summary

M2 POC for the unified tool registry landed cleanly. All four verification
gates passed. `web_search` is now the single source of truth in the registry;
both `build_tools()` (router surface) and the planned phase-side path consume
it. No stop signals were triggered.

## Gates

| Gate | Status | Detail |
|---|---|---|
| Byte-identity for LLMReplay fixtures | GREEN | `tests/test_replay_skill_router.py` — 11 passed, 0 failures; no fixture re-recording required |
| Drift test | GREEN | `tests/test_web_search_unified.py` — 14 new Tier 2 invariants pass; description and parameters constants verified byte-identical to legacy `ToolSpec` literal |
| Full suite | GREEN | 1500 passed / 2 xfailed (delta: +14 from M1 baseline of 1486) |
| mkdocs strict | GREEN | `mkdocs build --strict` — no errors or warnings |

## What worked cleanly

1. **`render_for_router()` shape** — `ToolDefinition.render_for_router()` from M1
   produces exactly `{"type": "function", "function": {"name": ..., "description": ...,
   "parameters": ...}}` which is identical to `ToolSpec.to_openai_dict()`. No
   adaptation needed; byte-identity was immediate.

2. **`build_tools()` integration** — replacing the inline `ToolSpec` literal
   with a registry lookup + render in `build_tools()` required only a small
   code block. The ToolSpec wrapper (`ToolSpec(name=..., description=...,
   parameters=...)`) was used to slot the rendered values back into the existing
   `specs: list[ToolSpec]` list, preserving the downstream conversion
   `[spec.to_openai_dict() for spec in specs]` unchanged.

3. **`get_default_registry()` entry point** — a single function in
   `src/reyn/tools/__init__.py` constructs and returns the registry with
   WEB_SEARCH registered. Lazy import (`from reyn.tools.web_search import
   WEB_SEARCH`) avoids circular dependencies at module init time.

4. **No fixture re-recording** — the LLMReplay fixtures record the full
   `tools=` payload sent to the LLM. Since `render_for_router()` is
   byte-identical to the prior literal, all 11 replay tests passed without
   touching any fixture file.

## Adapter shim — legacy OpContext bridge

The existing `handle_web_search` handler expects `(op: WebSearchIROp, ctx:
OpContext, caller: Literal["preprocessor", "control_ir"])`. `ToolContext` and
`OpContext` are not structurally identical; the adapter in
`src/reyn/tools/web_search.py::_handle()` bridges them as follows:

| OpContext field | Source in adapter | Notes |
|---|---|---|
| `workspace` | `ctx.workspace` | Direct pass-through |
| `events` | `ctx.events` | Direct pass-through |
| `permission_decl` | `PermissionDecl()` | Required field on OpContext, absent from ToolContext. Empty-defaults `PermissionDecl()` is safe for web_search: the handler performs no permission checks (read-only public query). This is the only mandatory field ToolContext cannot supply. |
| `permission_resolver` | `ctx.permission_resolver` | Direct pass-through |
| `subscribers` | `getattr(ctx.events, "subscribers", [])` | Defensive fallback; events objects without `subscribers` get an empty list. Web search does not spawn sub-skills, so subscribers are not forwarded. |
| `skill_name`, `skill`, `resolver`, `output_language`, `intervention_bus`, `current_phase`, `parent_skill_run_id` | hardcoded defaults | Unused by web search handler |
| `shell_allowed`, `mcp_servers`, `mcp_clients` | hardcoded defaults | Unused by web search handler |
| `model`, `max_phase_visits`, `state_dir_strategy`, `sub_state_dir_override`, `preprocessor_phase_name`, `preprocessor_step_index` | hardcoded defaults | Unused by web search handler |

The `permission_decl` gap is the only structural mismatch found in M2. It is not
a showstopper because `PermissionDecl()` is a safe default for any read-only
capability that does not write to the workspace (web_search, web_fetch, catalog
browsing). Capabilities that require non-empty permission declarations
(file_write, shell, mcp) will need `ToolContext` to carry a `permission_decl`
field or the phase-state sub-object to expose it. This is a design note for M3.

## Recommendations for M3

1. **Add `permission_decl` to `ToolContext` or `phase_state`** — M3 migrations
   for file write ops, shell, and mcp will need the permission declaration at
   handler time. The cleanest fix: add `permission_decl: PermissionDecl =
   field(default_factory=PermissionDecl)` to `ToolContext` directly (it's
   protocol-agnostic — both router and phase grant permissions). Alternatively,
   expose it on `phase_state` and require capabilities that need it to check
   `ctx.phase_state is not None`. Recommend the direct-field approach for
   simplicity.

2. **First file-op migration triggers Open Questions 6 + 7** — the ADR notes
   that file ops (step #3 in the capability table) force a naming decision
   (`read_file` vs `file` + `action`). An ADR amendment recording the choice
   (recommendation: router-side fine-grained names) should precede that step.

3. **Pattern to repeat** — the web_search migration is the canonical M3
   template: (a) write `src/reyn/tools/<name>.py` with `_DESCRIPTION` and
   `_PARAMETERS` constants matching the existing `ToolSpec` literal verbatim,
   (b) write an `_handle()` adapter wrapping the legacy handler, (c) define the
   `ToolDefinition` instance, (d) register in `get_default_registry()`, (e)
   replace the `ToolSpec` literal in `build_tools()` with a registry lookup,
   (f) add Tier 2 invariants covering byte-identity + gates + purity + category
   + registry lookup.

4. **Phase-side dispatch unification (M3 scope)** — M2 left `OP_KIND_MODEL_MAP`
   and `ControlIRExecutor.available_ops()` unchanged. M3 should wire the
   registry into `available_ops()` so phase-side descriptions also derive from
   the single-source `ToolDefinition`. The `render_for_phase()` method on
   `ToolDefinition` produces the `ControlIROpSpec`-compatible shape; the
   executor needs to call `registry.for_phase()` and convert via
   `render_for_phase()` instead of the hand-written `ControlIROpSpec` list.
