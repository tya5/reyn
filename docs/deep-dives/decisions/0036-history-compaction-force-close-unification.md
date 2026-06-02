# ADR-0036 (#1092) — chat/plan/phase within-unit history + compaction + force-close unification (Fork 1: RouterLoop convergence)

**Status**: ACCEPTED (user GO 2026-06-02 "懸念点なし、進められるなら進めて"; e2e technical review APPROVE, 3 precisions folded).
**Track**: #1092 (umbrella). **Canonical contract / staging**: GitHub issue **#1234** (FD1–FD7 + staging PR-A..E + test discipline + scope boundary).
**Builds on**: **ADR-0035** (#1212 — phase op-loop / separate-decide / frame-fed; a landed file at `docs/deep-dives/decisions/0035-phase-tool-calls-unification.md`). This ADR **PRESERVES** #1212's separate-decide and converges only the within-unit act-loop history representation (frame-fed → RouterLoop message-history).
**Recon foundation**: e2e DEEP_DIVE.md / DEEP_DIVE_2.md / DEEP_DIVE_3.md (primary-evidence flow-trace on main).

## Context

`CompactionEngine` (head/middle/tail/new + retry, dead-end-free; `services/compaction/engine.py`) is
**already shared** by all three subsystems. The divergence is the higher layer — the **within-unit act-loop
shape**:

| | within-unit act-loop | within-unit compaction | cross-unit result-passing (OUT of scope) |
|---|---|---|---|
| **chat** | RouterLoop over a saved `ChatMessage` list (native-tools) | CompactionController on the message list | — |
| **plan-step** | RouterLoop (saved history per step, native-tools) `planner.py:1029` | step-axis (engine) | `step_results: dict[str,str]`, dependency-gated into next step's prompt |
| **phase** | **FRAME-FED outlier**: `_run_op_loop`/`_run_act_loop` rebuild the frame each turn, `control_ir_results` in the frame, NO message list | `compact_control_ir_results` (engine) | workspace artifacts via `input_schema` (P5) |

So chat + plan-step share the saved-message-history RouterLoop; **phase is the lone outlier** purely because
its act-loop is frame-fed (#1212).

## Scope boundary (user-locked 2026-06-02)

- **IN**: commonize the **within-unit conversation-history (act-turns) + compaction + force-close** across
  chat / plan-step / phase — bring phase's act-loop onto the shared RouterLoop saved-history.
- **OUT (unchanged)**: **cross-step `step_results` / cross-phase workspace-artifacts** result-passing — the
  cross-unit result axis (structurally identical, both result-passing not conversation-history). User confirmed
  "今回そこに手を加える必要はなさそう".
- **OUT (deferred)**: reasoning-preservation (`act_turn_reasoning`) — revisit after history+compaction
  commonized (user: "リスニング(reasoning)の保存については一旦議論から外したい").

## Decisions

### FD1 — Phase converges onto the shared RouterLoop within-unit act-loop
Phase supplies a phase-side `RouterLoopHost` (events = phase EventLog; recording = OS WAL via `LLMCallRecorder`).
The phase frame's static parts (instructions, candidates, op schemas) → `system_prompt_override` (exactly how
plan injects its step prompt). Act-turns become RouterLoop iterations with a **persistent message history**
(assistant tool_calls + tool-result messages); `control_ir_results` become tool-result messages.
`max_iterations` = phase `max_act_turns`.

**FD1 precision (e2e review — the meatiest PR-A scope point): the gap is the tool-catalog BUILD SOURCE, not
dispatch.** Dispatch is ALREADY shared — RouterLoop executes via `dispatch_tool` (`reyn.dispatch`), the same
dispatcher the op-executor uses (no dispatch gap). But RouterLoop builds its tool catalog from **chat-discovery**
(`host.list_available_skills` + universal-catalog wrappers `list_actions`/`search_actions`), NOT from
`allowed_ops`. So **PR-A needs a catalog-source REPLACE seam in RouterLoop**: a phase supplies
`_build_phase_tool_catalog(allowed_ops)` (EXISTS ✓) **INSTEAD of** chat discovery — and per #1212 PR3 decision A
(chat-router tools ≠ phase ops) it must **REPLACE, not augment** (no skills/agents/mcp/universal-wrappers inside
a phase). `system_prompt_override` handles the prompt ✓.

**FD1 RESOLVED — (c) narrow-core extraction** (lead-coder call; e2e verify-gate line-mapped, APPROVE). The
`RouterLoopHost` Protocol is actually **~30 methods** (not ~10) — most chat-specific (web/reyn_src/memory/
file_*/mcp_*/discovery/spawn/send_to_agent/record_plan_*). Rejected: (a) a ~30-method stub phase-host (27 stubs =
throwaway under the user's no-throwaway principle, fragile); (b) a full chat-host refactor (too big, risks the
live chat path). **(c)**: extract the host calls RouterLoop's **loop** directly makes into a **`RouterLoopCore`
Protocol**; `RouterLoopHost = RouterLoopCore + chat-extras`; RouterLoop's loop is typed against
`RouterLoopCore`; the chat `RouterHostAdapter` (a superset) satisfies `RouterLoopCore` for free (zero chat
risk); the **PhaseRouterLoopHost implements `RouterLoopCore` only, ZERO chat-extra stubs**.

verify-gate (e2e, line-mapped — no chat-extra couples into the loop-core, so (c) holds):
- **`RouterLoopCore` = 6 shared members**: `events` (property) · `make_router_op_context()` (the OpContext
  for `dispatch_tool` — the op-execution bridge) · `resolve_model(name)` · `put_outbox(...)` · attrs
  `agent_name` / `agent_role` / `output_language`. (`append_history_entry` / `record_plan_*` / `resolver` are
  NOT directly called by RouterLoop → out of core; no plan-record no-op needed.)
- **`get_phase_op_catalog()`** stays a **getattr-hook** (phase-only; chat doesn't implement it), already added
  in the catalog-source seam (8d48b4ef).
- **chat-extras (outside core, phase-unreached)**: list_available_skills/agents, get_memory_index/mcp/web/
  file_perms/project/universal/embedding/sandbox, memory_*, file_*, mcp_*, web_*, reyn_src_*, spawn/run_skill/
  send_to_agent. They live in the chat-discovery setup, the chat-SP-build (override-skipped), or chat-dispatch
  handlers — phase ops dispatch via the op handlers + the phase OpContext, never these.

**Refinement (i)**: the chat-discovery SETUP (`router_loop.py` ~1486-1717, `list_available_skills`/`get_*`) runs
BEFORE the seam — guard it behind `_phase_op_catalog is None` so a phase calls zero discovery methods (grows the
catalog-source seam from "override `tools=`" to "skip setup + set `tools=`"; no cascade to the chat adapter;
chat byte-identical). **Refinement (ii)**: `put_outbox` is a phase **no-op** — phase has no user outbox (its
output is the result artifact / transition), a legitimate concept-absent no-op, not a fragile chat stub. The
Tier-2 test asserts this is **not load-bearing** (phase accumulates op-results into the message-history; no-op-ing
`put_outbox` drops nothing phase needs — if it were load-bearing, the test fails).

**→ PR-A scope** = catalog-source seam (✓ 8d48b4ef) + `RouterLoopCore` extraction + setup-guard +
PhaseRouterLoopHost (6 core impls + `get_phase_op_catalog`, zero chat-extra stubs). Throwaway-free,
chat-unaffected, P7-aligned (no chat-tool-exec baked into the loop).

**#1240 update — op-execution seam OBVIATED (rebased PR-A, e2e flow-trace on post-catalog-axis main).**
An interim FD1-beta detour added an `execute_phase_op` host hook (RouterLoop delegates phase tool calls to
the host's `control_ir_executor`) because back then RouterLoop's `_invoke_router_tool` routed only chat
`REGISTRY_DISPATCH_TOOLS` names while phase op-kinds (`file__read`/`exec`) hit "unhandled tool". The #1240
catalog axis dissolved that: phase op tool NAMES are now the unified fine registry kinds (`read_file` …
`grep_files`, `invoke_skill`, `call_mcp_tool`), which `_invoke_router_tool` already routes via its
`REGISTRY_DISPATCH_TOOLS` registry path — vindicating FD1's "dispatch is ALREADY shared" precision. So the
op-exec seam is DROPPED and **PhaseRouterLoopHost is catalog-only** (`get_phase_op_catalog` + the 6
`RouterLoopCore` members). Dispatch returns to the FD1 design: phase ops dispatch via `dispatch_tool` /
registry, with the phase `OpContext` supplied by `make_router_op_context` (the "op-execution bridge" named
above) — NOT a phase-specific exec hook.

PR-A is **inert** (PhaseRouterLoopHost is not yet wired into `PhaseExecutor`; `_run_op_loop` still runs).
Two residuals close in **PR-B** (the convergence wiring), verified against current main:
1. add `edit_file` / `glob_files` / `grep_files` to `REGISTRY_DISPATCH_TOOLS` (router_loop.py) — registry
   ToolDefs that chat never exposed as router tools but are now in the phase default `allowed_ops`
   (root-fix = extend the existing registry-uniform path, NOT revive the host exec hook).
2. implement `PhaseRouterLoopHost.make_router_op_context` to return a phase `OpContext` (carrying the phase
   `PermissionDecl` / `allowed_ops` / sandbox policy) so the registry handlers enforce phase permissions —
   the provisioning role the obviated seam's `control_ir_executor` dispatch played (PR-A stubs it as None,
   never reached while inert).

### FD2 — RouterLoop is json-mode-free; the structured transition stays a separable post-pend (肝)
*(3-source locked: user direction + primary-evidence + e2e §3a.)* RouterLoop runs ops native-tools
(`call_llm_tools`, `tool_choice=auto`), ends on `end_turn`. The phase **then** does its structured-transition
json `.call` (P1/P8: control+artifact). This **PRESERVES #1212's separate-decide — it is NOT a reversal.** The
saved-history change is only the act-loop's op-RESULT turn-representation (frame-rebuild → message-list /
native-tool-role), **orthogonal** to the transition decide.

### FD3 — (i)/(ii) split: only the ops-emission envelope converges; transition json is permanent
Phase has two json usages: **(i) ops-emission** (json-mode phases emit ops in the `control_ir` JSON field;
op-loop emits native tool_calls) and **(ii) transition decide** (structured json `.call`). Convergence retires
**ONLY (i)** — eventually all phases emit ops as native tool_calls (PR-E). **(ii) is never a retire target**
(P1/P8 transition contract). The ADR states (i)/(ii) explicitly alongside P1/P8 so "(ii) is unchanged" is
unambiguous.

### FD4 — Crash-recovery memo unifies cleanly (no hard-finding)
*(e2e §3b good-news, review-verified.)* (A)'s `call_tools` memo and plan's `SubLoopMemoProvider` are the **same
pattern** — `compute_sub_loop_args_hash` docstring literally says "Mirrors `dispatcher._compute_llm_args_hash`
shape" + `_serialise/_deserialise` on `LLMToolCallResult` (content/tool_calls/finish_reason/usage) +
`get_recorded_result(args_hash)`/`record`. **Precision (e2e): they differ in backend AND args_hash INPUT** —
`SubLoopMemoProvider` hashes **MESSAGES**, (A) hashes the **FRAME** (`ContextFrame.model_dump`). Under RouterLoop,
phase moves to messages → **phase ADOPTS `compute_sub_loop_args_hash` (messages-based)**; (A)'s frame-based hash
**RETIRES WITH frame-fed**. So FD4 is cleaner than "two backends of one hash": *phase adopts the
SubLoopMemoProvider hash + serialise, with an OS-WAL backend*. No incompatibility — folded into PR-A.

### FD5 — Compaction + force-close reuse directly once phase has a message-history
`CompactionController`/engine drive the phase message-history. `force_compact_now` + the summary-bridge
(`chat/slash/compact.py` in-place middle replacement) apply once phase has a message-history. A phase can
compress its act-history mid-loop + continue.
- **FD5-decision (lead-coder call, override-able)**: phase compaction on the message-history **REPLACES**
  `compact_control_ir_results` (the message-history becomes the within-phase SSoT for act-turns; avoid
  double-compaction). Lands in PR-B.
- **FD5-decision (lead-coder call, override-able)**: phase force-close is triggered by the **`compact` op /
  budget**, NOT a user `/compact` slash (the slash is chat-only; a phase is not a user conversation). Phase
  reuses the `force_compact_now` + summary-bridge **mechanism**, not the chat user-surface. Lands in PR-C.
  **Grounded (e2e review): phase ALREADY has `_make_phase_compact_now` (`phase_executor.py:77`, #1176 B1)
  reached by the `compact` op** — today it compacts `control_ir_results`; PR-C **re-points it to the
  message-history** via `CompactionController`/`force_compact_now` (not the `/compact` slash). The trigger seam
  already exists; PR-C only changes its target.

### FD6 — Replay re-pricing: native tool_call-id normalization returns, bounded by reusing chat's handling
Frame-fed made provider-id normalization MOOT (#1212); RouterLoop threads native tool_call ids → it returns.
BUT `testing/replay.py` (keys on `model + canonical_json(messages)`) + chat RouterLoop replay **already handle
native tool_calls**. Reuse: phase RouterLoop replay == chat RouterLoop replay (same machinery). Net-new:
phase-specific fixtures only. Lands in PR-D.

### FD7 — Staging: op-loop-first; json-mode convergence is the last, large PR
The op-loop ALREADY emits native tool_calls → converging it to RouterLoop is incremental. json-mode → RouterLoop
= switching ops from the control_ir envelope to native tool_calls = "op-loop as default" (all 36 phases run
json-mode today) = much larger. Staging:
- **PR-A** — op-loop → RouterLoop behind the gate: phase-side `RouterLoopHost` + `system_prompt_override` +
  WAL-backed `memo_provider` (FD4) + structured-transition post-pend (FD2). Core convergence, behavior-preserving
  for opt-in (`tool_calls_op_loop_skills`) skills.
- **PR-B** — compaction: drive `CompactionController`/engine on the phase message-history; replace
  `compact_control_ir_results` (FD5).
- **PR-C** — force-close/summary-bridge for the phase act-history, op/budget-triggered (FD5).
- **PR-D** — replay fixtures for the phase RouterLoop (reuse chat's machinery, FD6).
- **PR-E (later, large)** — json-mode convergence = op-loop-as-default + retire (i) the control_ir emission path
  + gate removal (FD3). Gate bridges the interim.

## Consequences
- **+** One within-unit act-loop shape across chat/plan-step/phase (RouterLoop), one compaction controller, one
  force-close mechanism. Phase stops being the frame-fed outlier.
- **+** Promotion symmetry advances (plan→skill): both run the same RouterLoop act-loop + native tools; a
  working plan-step generalizes toward a skill phase with the same loop substrate.
- **−** #1212's frame-fed replay-simplification is given back (FD6) — bounded by reusing chat's existing native
  tool_call handling.
- **−** PR-E (json-mode retire) is large (36 phases); gated migration absorbs the risk.

## Test discipline (per testing.ja.md)
- Tier 2: phase RouterLoopHost wiring (real Workspace/EventLog, no Mock); memo_provider OS-WAL round-trip
  (non-default value, set→crash→resume→identical — reuse #1142/#1146 lesson).
- Tier 3: phase RouterLoop replay fixtures (reuse chat machinery, FD6); compaction-on-message-history behavior.
- Wire-full-frontmatter: load-from-disk path for any new config; full-rootdir suite for prompt/config changes.
- Falsification: enforcement/permission tests use a real (non-None) PermissionResolver (#1214/#1215 lesson).
