---
type: concept
topic: architecture
audience: [human, agent]
---

# LLM invocation surfaces — Router-style vs Phase-style

## 1. Why this matters

Reyn invokes the LLM in two structurally distinct contexts: the chat router (and its plan-mode variant) and the phase executor inside a skill. Each context exposes its own vocabulary of capabilities — function-calling tools for the router, Control IR ops for the phase. The two sets overlap substantially but not completely. Without a written account of that divergence, contributors tend to add new capabilities to whichever surface is convenient, and the gap widens silently. This document names the two invocation kinds, maps the divergence, and identifies which asymmetries are principled and which are convention drift — so that future additions land in the right place, and unintended asymmetries surface before they accumulate.

---

## 2. Two invocation kinds

### 2.1 Router-style (chat and planner)

**Used by:** `RouterLoop` (interactive chat sessions) and `PlanRuntime` (plan-mode step execution). Both share a single implementation: `RouterLoop` with a `RouterLoopHost` facade that narrows the catalog per context.

**Mechanism:** native LLM function calling via `call_llm_tools` (backed by litellm). Tool definitions follow the OpenAI `tools` array shape; the model replies with `tool_calls` in the assistant message. The OS dispatches each call, appends the `tool_result`, and re-invokes the LLM until it produces a plain text reply.

**Tool surface:** `build_tools()` in `src/reyn/chat/router_tools.py` assembles the tool list. The actual count depends on operator configuration:

- **Always present (14 tools):** `list_skills`, `describe_skill`, `list_agents`, `describe_agent`, `list_memory`, `read_memory_body`, `delegate_to_agent`, `remember_shared`, `remember_agent`, `forget_memory`, `web_search`, `plan`, `reyn_src_list`, `reyn_src_read`.
- **Conditional (+0 to +9 tools):** `invoke_skill` (when skills are registered), `list_directory` + `read_file` (when file read scope is configured), `write_file` + `delete_file` (when file write scope is configured), `web_fetch` (operator opt-in), `list_mcp_servers` + `list_mcp_tools` + `call_mcp_tool` (when MCP servers are configured).
- **Verified range: 14–23 tools.** (The comment in `router_tools.py` that states "11–18" predates the addition of `web_search`, `plan`, `reyn_src_list`, and `reyn_src_read` and is stale.)

**Plan-mode is the same surface, minus `plan` itself:** `PlanRuntime` wraps `execute_plan`, which builds a `_PlanStepHost` facade and instantiates `RouterLoop` with `exclude_tools={"plan"}` to prevent recursive plan decomposition. Every other router tool available to the parent session is available to each plan step, filtered further by the step's declared `tools` list.

**Role:** orchestration — pick the next sub-component (skill, agent, plan, memory operation, direct text reply).

### 2.2 Phase-style (skill execution)

**Used by:** every phase invocation inside a skill, driven by `OSRuntime`.

**Mechanism:** JSON output contract. The LLM returns a single structured response:

```json
{
  "control": {"type": "transition|finish|abort", "decision": "continue|finish|abort",
               "next_phase": "<name> or null", "confidence": 0.0, "reason": {}},
  "artifact": {"type": "<schema_name>", "data": {}},
  "control_ir": []
}
```

No native function calling. The LLM declares its intended side effects in `control_ir` as typed op objects; the OS dispatches them.

**Op surface:** 8 Control IR op kinds, defined in `OP_KIND_MODEL_MAP` in `src/reyn/op_runtime/registry.py`:

| Op kind | Purpose |
|---------|---------|
| `file` | Read, write, glob, grep, edit, or delete files |
| `mcp` | Call a tool on a configured MCP server |
| `run_skill` | Invoke a sub-skill as a nested workflow |
| `shell` | Run a shell command |
| `lint` | Run the DSL linter on a skill directory |
| `ask_user` | Pause and prompt the user for input |
| `web_fetch` | Fetch a single URL |
| `web_search` | Search the public web |

Each phase narrows this set further via `allowed_ops: list[str]` in the phase declaration (default: `["file", "ask_user"]`). The OS enforces `allowed_ops` at dispatch time as a defense-in-depth layer.

**Role:** domain work — produce an artifact for the next phase or as the skill's final output.

### 2.3 What is NOT a third invocation kind

Two constructs are sometimes confused with LLM invocation kinds because they appear in the same phase execution context:

**Preprocessor steps** (`run_skill` / `iterate` / `validate` / `lint_plan` / `python`) run deterministically, before the phase LLM call. They do not invoke the LLM themselves. The `python` step executes a sandboxed Python function. The `run_skill` step dispatches a sub-skill recursively — that sub-skill contains its own phases that DO invoke the LLM phase-style, but the preprocessor step itself is synchronous and does not make an LLM call from the preprocessing layer. See [preprocessor.md](preprocessor.md).

**Postprocessor steps** (same step types) run deterministically, after the LLM's `finish` output, before the artifact is returned to the caller. Not an LLM call. See [postprocessor.md](postprocessor.md).

Both are OS-executed deterministic pipelines, not LLM invocations.

---

## 3. Capability comparison matrix

| Capability | Router-style surface | Phase-style surface | Status |
|------------|---------------------|---------------------|--------|
| File read | `read_file` (conditional on file read permission) | `file` op (kind=file, op=read) | Symmetric |
| File write / delete | `write_file`, `delete_file` (conditional on file write permission) | `file` op (kind=file, op=write/delete) | Symmetric |
| File list directory | `list_directory` | `file` op (kind=file, op=glob) | Symmetric |
| Web search | `web_search` (always present) | `web_search` op | Symmetric |
| Web fetch | `web_fetch` (operator opt-in) | `web_fetch` op | Symmetric |
| MCP call_tool | `call_mcp_tool` (conditional on mcp_servers) | `mcp` op | Symmetric |
| MCP discover (list servers / tools) | `list_mcp_servers`, `list_mcp_tools` | Not available | Gap (Type C) |
| Shell | Not available | `shell` op | Role-separated (Type B) |
| Lint | Not available | `lint` op | Role-separated (Type B) |
| Run / invoke skill | `invoke_skill` (conditional on skills registered) | `run_skill` op | Symmetric |
| Inter-agent delegation | `delegate_to_agent` | Not available | Role-separated (Type B) |
| Ask user | Not available as a tool; router exits with a text reply | `ask_user` op | Role-separated (Type B) |
| Memory read | `list_memory`, `read_memory_body` | Via context_builder injection only (read at phase start; no mid-phase query) | Gap (Type C) |
| Memory write | `remember_shared`, `remember_agent`, `forget_memory` | Not available | Gap (Type C) |
| Catalog browse | `list_skills`, `describe_skill`, `list_agents`, `describe_agent` | Via op_catalog injection in ContextFrame only (no mid-phase query) | Gap (Type C) |
| Plan invocation | `plan` | Not available (use `run_skill` for in-phase decomposition) | Role-separated (Type B) |
| Reyn source read | `reyn_src_list`, `reyn_src_read` | Not available | Router-only |

---

## 4. Four divergence types

### Type A — Healthy symmetry

Capabilities present on both sides with the same semantic, expressed in different invocation forms (function calling vs Control IR JSON). These are not problems; they are the natural consequence of two API styles.

**Examples:** file ops (`read_file` ↔ `file/read`), web ops (`web_search` / `web_fetch` ↔ `web_search` / `web_fetch` ops), MCP invocation (`call_mcp_tool` ↔ `mcp` op), skill invocation (`invoke_skill` ↔ `run_skill` op).

The router LLM calls `invoke_skill("name", input={...})`; the phase LLM emits `{"kind": "run_skill", "skill": "name", "input": {...}}`. The OS dispatches both. The symmetry is real; the surface form differs because the two invocation kinds use different protocols.

### Type B — Deliberate role separation

Asymmetries that exist for principled reasons and should remain asymmetric:

- **`delegate_to_agent` is router-only.** Phases work within a skill scope. Routing a request to a peer agent is an orchestration decision that belongs to the chat session, not to a phase mid-execution. Allowing agent delegation from inside a phase would conflate the orchestration layer (session) with the domain-work layer (phase).

- **`plan` is router-only.** Phases already have `run_skill` for in-phase decomposition. The `plan` tool is the chat session's mechanism for multi-source synthesis across router turns; it has no analog inside a phase because phases have a defined input and output contract.

- **`shell` is phase-only.** Exposing `shell` directly to the chat router would allow the LLM to execute arbitrary commands in a free-form conversational context with no schema boundary. The phase model constrains this: `shell` is opt-in per skill, gated by `allowed_ops`, and the phase's input schema narrows what data reaches the command. The router LLM sees the user's open-ended request; the phase LLM sees a bounded, schema-validated artifact.

- **`lint` is phase-only.** Lint validates the LLM's skill-authoring output during a phase. It has no use in the chat router, which does not produce skill artifacts.

- **`ask_user` is phase-only as an explicit op.** The router LLM asks the user by emitting a plain text reply — the `RouterLoop` exits with that text. The phase LLM cannot exit mid-phase to reply; it must use `ask_user` in `control_ir` to pause and surface a question to the OS.

### Type C — Convention drift

Asymmetries that emerged over time without a doctrine and do not have a strong role-based reason to exist:

- **Memory I/O is router-only.** The tools `list_memory`, `read_memory_body`, `remember_shared`, `remember_agent`, and `forget_memory` are available to the chat router. Phases receive memory injected via context_builder at phase entry (read-only snapshot); they cannot query or update memory mid-phase. There is no principled architectural reason why phases cannot write memory — the gap emerged because memory tools were added to the router for direct user interaction, and no corresponding phase capability was designed.

- **Catalog browse is router-only.** The tools `list_skills`, `describe_skill`, `list_agents`, and `describe_agent` are available to the chat router. Phases that need skill or agent catalog data (for example, `eval_builder` or `skill_improver`) receive the catalog injected as ContextFrame data (`op_catalog`), but they cannot issue a mid-phase catalog query. This gap emerged because catalog browsing was primarily useful for the router's "what skill should I invoke?" decision; the phase use case was less common and handled by injection rather than tools.

- **MCP discover is router-only.** `list_mcp_servers` and `list_mcp_tools` are available to the chat router. Phases using the `mcp` op must have the server name and tool name statically declared in `control_ir`; they cannot discover available MCP tools at runtime. This gap emerged because MCP browsing was added for the router's interactive "what can I do with MCP?" use case, without a corresponding discovery mechanism for the phase-side `mcp` op.

These are gaps, not failures. Whether to close them is the doctrine question addressed in Section 6.

### Type D — Pre-LLM deterministic steps

Preprocessor and postprocessor steps are not LLM invocations, but they appear in feature-parity discussions because they are "things a skill author can reach for." The distinction matters:

- A `python` preprocessor step runs sandboxed Python code — no LLM call.
- A `run_skill` preprocessor step invokes a sub-skill whose phases DO invoke the LLM phase-style — but the preprocessor dispatch itself is synchronous and OS-controlled, not an LLM call in the same turn.
- A `validate` step runs a JSON Schema check — no LLM call.

Preprocessor and postprocessor steps expand what a phase can compute before and after the LLM call; they do not constitute a third invocation kind.

---

## 5. Why the divergence happened — historical pattern

The chat router accumulated capabilities via tool additions whenever a new feature landed: memory I/O, catalog browse, web ops, plan mode, Reyn source access. Each addition was natural in context — the chat user wants to ask about memory directly, or browse the catalog interactively, or search the web in a conversational turn. The phase Control IR op set grew more conservatively (8 op kinds vs up to 23 router tools) because Reyn's phase model emphasizes constrained candidate sets (P4) and skill-author intent: a phase declares what it is allowed to do, and nothing more. The result is that the router accumulated interactive-exploration capabilities; the phase surface stayed domain-work-focused. This is appropriate where the role-separation reason holds (Type B); it is convention drift where it does not (Type C).

---

## 6. Doctrine options

The question: **should the convention-drift gaps (Type C) be closed?** This section presents three options. The choice is a separate decision; this document establishes the framework.

### Option 1 — Full symmetry

Every capability available on both surfaces, expressed in the appropriate invocation form. Type B exceptions (shell, lint, ask_user, plan, delegate_to_agent) are retained as documented exceptions.

- **Pros:** clean doctrine; no asymmetry except by explicit per-capability choice; contributors have a simple default rule ("add to both unless there is a reason not to").
- **Cons:** some capabilities do not fit naturally on both sides (a phase that delegates to a peer agent mid-execution conflates orchestration with domain work); surface area grows; every new capability requires a two-surface implementation.

### Option 2 — Role-based asymmetry (ratify current state)

Document the current asymmetries as the doctrine. Router does orchestration; phase does domain work; capabilities belong to one role only. Type C gaps are accepted as-is.

- **Pros:** minimal change; codifies what already works; contributors have a clear rule ("is this orchestration or domain work?"); no implementation cost.
- **Cons:** Type C gaps are rubber-stamped without re-examining whether the role argument actually applies to them; memory write from a phase is a legitimate need that this option leaves unaddressed; the doctrine may not age well as more complex skills require richer phase-side capabilities.

### Option 3 — Hybrid: close Type C only

Adopt Option 2's role separation for Type B, but explicitly close the three Type C convention-drift gaps:

- **Memory write from phases:** new `memory` op kind (or a stdlib skill like `update_memory`) so phases can write durable facts without routing through the chat layer.
- **Catalog browse from phases:** a stdlib skill (for example, `recall_skill_catalog`) that a phase can invoke via `run_skill` to query the live catalog mid-phase, without embedding catalog knowledge in the OS.
- **MCP discover from phases:** extend the `mcp` op with `action=list_servers` and `action=list_tools` variants so phases can probe available MCP capabilities at runtime.

- **Pros:** principled — role-based where role separation is real (Type B), symmetric where the gap was unintentional (Type C); the doctrine does not accumulate technical debt; new capabilities designed for both surfaces from the start.
- **Cons:** medium implementation cost (three new capabilities); ordering matters (stdlib skills before phase op extensions); requires discipline to avoid re-creating drift with the next batch of additions.

---

## 7. How this connects to existing principles

**P3 (OS controls execution)** — both invocation kinds are OS-mediated. The router LLM calls tools; the OS dispatches them. The phase LLM emits `control_ir`; the OS dispatches those ops. Neither surface allows the LLM to execute directly. The doctrine question is about which capabilities the OS exposes to each kind, not about who controls execution.

**P4 (LLM is a constrained decision engine)** — both invocation kinds present a curated candidate set. The router LLM sees a fixed tool list assembled by `build_tools()`. The phase LLM sees `available_control_ops` built from the phase's `allowed_ops`. Doctrine is about which candidates each kind sees; P4 applies to both sides equally.

**P7 (OS skill-agnostic)** — neither surface should embed skill-specific knowledge. Closing Type C gaps via stdlib skills (Option 3 path) preserves P7: the OS exposes a general `memory` op or `run_skill` mechanism; the skill author decides whether to use it. Embedding skill-specific memory keys or catalog paths in the OS layer would violate P7.

---

## 8. See also

- [principles.md](principles.md) — P3, P4, P7
- [architecture.md](architecture.md) — overall component layering and the runtime loop
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — responsibility boundaries between Phase, Skill, and OS
- [care-boundary.md](care-boundary.md) — what Reyn does and does not own; the downstream tooling section complements the matrix above
- [preprocessor.md](preprocessor.md) — pre-LLM deterministic steps (= why those are not a third invocation kind)
- [postprocessor.md](postprocessor.md) — post-LLM deterministic steps (same reason)
- [../reference/runtime/control-ir.md](../reference/runtime/control-ir.md) — phase-side op vocabulary and semantics
- [../reference/cli/chat.md](../reference/cli/chat.md) — slash commands available in chat (sometimes confused with router tools; they are distinct)
- [../reference/cli/mcp.md](../reference/cli/mcp.md) — MCP server side (Reyn-as-MCP-server exposes a third surface that is NOT covered here because it is external clients calling INTO Reyn, not Reyn's internal LLM invocation kinds)

---

## 9. Implementation: unified registry (M4 Phase 3 step 2 + Phase 4 step 1 complete)

The dual-implementation architecture described in this document (two separate
catalogs: `router_tools.py` / `OP_KIND_MODEL_MAP`) is the historical baseline.
ADR-0026 (Status: Proposed) closes the structural drift by introducing a single
`ToolDefinition` per capability with two render methods.

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

**Phase 3.5 + Phase 4 step 2+ (deferred — design decisions required):**

- Registry dispatch for the remaining 18 router tools (file ×4 / mcp ×3 /
  memory ×5 / web ×2 / reyn_src ×2 / `invoke_skill`) needs per-tool byte-
  equivalence verification. Examples of the gap: `host.file_read` returns
  a string (extracted from op_runtime dict result) but the registry
  handler returns the full dict; memory tools use host-managed index
  layout vs the registry handler's filesystem-direct paths;
  `invoke_skill` needs `chain_id` propagation that `op_runtime
  caller="control_ir"` doesn't carry. Each cluster either gets a
  RouterCallerState callable bridge (preserves behavior) or accepts the
  shape change with dogfood validation.
- Phase-side dispatch consuming registry (`ControlIRExecutor` →
  `get_default_registry().for_phase()`); `allowed_ops` prefix-wildcard
  semantics in the phase dispatcher.
- `OP_KIND_MODEL_MAP` removal (Open Q #2); obsolete `op_runtime/<kind>.py`
  consolidation.

ADR-0026 Status remains **Proposed** until the deferred work above lands.
The `Acceptance criteria` section in the ADR enumerates the closing checklist.

**Cross-reference:** [../deep-dives/decisions/0026-unified-tool-registry.md](../deep-dives/decisions/0026-unified-tool-registry.md)
