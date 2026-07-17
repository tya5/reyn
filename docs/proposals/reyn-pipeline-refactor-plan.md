# Reyn Pipeline — Reuse-Side Refactor Plan

Companion to `reyn-pipeline-spec-v0.8.md` + `reyn-pipeline-reconciliation.md`. Primary-evidence scoping audit of the existing mechanisms Pipeline will reuse, so they become cleanly callable by a non-LLM/non-router consumer (a deterministic Pipeline executor / driver-session). Branch main, 2026-07-04. **Uncommitted working note.**

## Headline
- **The router loop does NOT need refactoring.** Pipeline is an *additive* parallel execution path that reuses the same registry / session / WAL / permission primitives. Once the programmatic seams below exist, a `PipelineExecutor` (or driver-session) is built independently.
- **Most seams are low-hanging**: the enforcement/pattern already exists; only a clean caller-side API/helper is missing (~30–80 lines each).
- **One deep-entanglement gap**: per-session turn budget (`max_turns`) — no per-session budget concept exists; the turn governor is woven into the router loop's iteration.
- Validates the "driver = session-typed deterministic driver" lens: the executor reuses session-spawn/capability/recovery/events wholesale.

## Mechanisms (7) — current coupling → target seam → size

| # | Mechanism | Current state / coupling | Target seam | Size | Risk |
|---|---|---|---|---|---|
| 1 | Session spawn + ephemeral | LLM tool `session_spawn.py`; `spawn_session_fn` is a closure over `RouterLoopHost`; `build_scoped_chat_session` is chat-shaped. Underlying `registry.spawn_session_recorded` is clean but unexposed. | Extract `build_session_core` + add `spawn_ephemeral_session(registry, identity, narrowing, budget)` (`runtime/session_api.py`) | M | low-med |
| 2 | Per-session budget (max_turns) | **No per-session `max_turns` anywhere.** Turn governor = router-loop's `wrap_up_system_prompt` (soft LLM prompt, not hard enforce). Token budget via `BudgetGateway`/`CostConfig` is process/turn level. | `SessionBudget{max_turns,max_tokens,timeout,on_exhausted}` threaded through spawn → a turn-counter in the session run *wrapper* (not the router iteration) | **L** | **high** (router_loop blast radius) |
| 3 | Capability narrowing | `ContextualLayer` ⊆-parent enforcement already correct; narrowing flows as `dict\|None` through spawn. Only the LLM tool is a caller today; profile-name→profile lookup not called from spawn path. | `build_narrowing_for_identity(identity, parent_profile, tools_allowlist)` (`capability_profile.py`) — ⊆-parent check caller-side | S | near-zero |
| 4 | Recovery / config-generation | `record_config_generation(state_log,path,content)` already caller-agnostic (keyed at durable seq, truncation-surviving). Pipeline control-plane persistence (spec §10) unresolved. | `record_pipeline_state(...)` (`core/events/pipeline_recovery.py`, ~30-line copy). **Truncate-falsify test MANDATORY** (CLAUDE.md gate) | S | low |
| 5 | Event emission | `EventLog` already consumer-agnostic (`agent_id` may be None); `emit_cli_event` shows the no-session pattern. | Reuse as-is; `EventLog(run_id=pipeline_run_id)`; document `pipeline_id` field convention | S | ~0 |
| 6 | Schema definition/validation (`verify: schema`) | **Entirely net-new** — no reusable schema registry/validator exists. `ToolSpec.parameters` is JSON-Schema for tool *inputs* only. | `core/schema/` — `SchemaRegistry` + `SchemaValidator` (JSON Schema recommended) | M | low |
| 7 | Router-loop coupling | Router loop = LLM turn loop; `RouterLoopHost`/`RouterCallerState` chat-centric. | **No change to router loop.** Build `PipelineExecutor` on the programmatic APIs (1–5). Router = LLM path; executor = deterministic path; shared primitives. | L (but additive) | med |

## PR breakdown (dependency-ordered)
- **A1**: extract `build_session_core` from `build_scoped_chat_session` (byte-identical chat path) — S
- **A2**: add `spawn_ephemeral_session` API + gate-equivalence test vs the LLM tool path — S  *(dep: A1)*
- **B1**: `build_narrowing_for_identity` + Tier-1 non-default-value test — S  *(dep: A1)*
- **C1**: `SessionBudget` + `max_turns` turn-counter in the session run wrapper — **L, high risk** *(dep: A2)*
- **D1**: `record_pipeline_state` + mandatory truncate-falsify test — S  *(dep: A2)*
- **E1**: `SchemaRegistry` + `SchemaValidator` (JSON Schema) — M *(independent)*
- **F1**: Pipeline event-identity convention + doc + test (no `EventLog` code change) — S *(dep: A2)*

```
A1 → A2 → B1
         → C1   (high risk — last)
         → D1
         → F1
E1  (independent)
```
**Recommended sequence: A1 → A2 → B1 → D1 → F1 → E1, then C1 last** (C1 is the hard one; Pipeline prototyping can start with unbounded sessions before C1 lands).

## Notes
- Each PR must be behavior-preserving with a gate-equivalence test (the current LLM-tool path stays byte-identical); this de-risks "something assumed router-only has a hidden router dependency."
- The refactor delivers value independently of Pipeline (cleaner, consumer-agnostic reuse seams).
- The Pipeline executor itself + DSL parser + static analyzer are entirely net-new (out of this refactor's scope — this refactor only cleans the *reused side*).
