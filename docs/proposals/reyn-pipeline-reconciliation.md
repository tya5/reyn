# Reyn Pipeline Spec v0.8 ↔ Current Reyn — Reconciliation

Companion to `reyn-pipeline-spec-v0.8.md`. Primary-evidence check of the spec's Reyn-integration claims against the actual codebase (branch main, 2026-07-04). The spec was authored (by fable5) from the owner's verbal description, not the code — this document is the correction pass. **Uncommitted working note.**

## Bottom line
The spec is a coherent design, but its §7 framing — "Pipeline は独自機構を持たない、すべて Reyn 既存機構に準拠" — is **aspirational, not current status**. The individual mechanisms it wants to reuse (permission, WAL, event log, sessions, capability narrowing) DO exist, but:
- the **Pipeline execution engine itself is 100% net-new** (nothing exists), and
- several specific integration claims are **factually wrong about current reyn** and would mislead an implementer.

## §7 conformance claims — verification

| Spec claim | Rating | Reality (file:line) |
|---|---|---|
| 承認 = tool-granular **Allow/Deny/Ask** | **PARTIAL/MISLEADING** | Real model = per-axis allowlists (`PermissionDecl.tool/mcp/file_*`) + 4-layer approval flow. Config is `allow`/`deny` (2-value); "Ask" is the *absent-approval default* when a RequestBus exists, NOT a declarable per-tool value. `permissions.py:196`, `permission-model.md:196` |
| 記録 = 3層 (hooks / **Global Journal** / **Audit Event**) | **PARTIAL — terms invented** | "Global Journal" = the **WAL/StateLog** (`state_log.py`). "Audit Event" = **EventStore**, a synchronous file-backed JSONL appender — **NOT OTEL, NOT pub/sub** (that claim is false). hooks real (`hooks.py`) but are LLM-self-registered, not operator event-subscription. |
| agent step / for_each = 一時セッション → crash-recovery/time-travel **for free** | **MISMATCH — overstated** | Sessions exist (`session_spawn.py`, `mode=ephemeral`) + WAL-track existence (`session_spawned`). BUT WAL does NOT record pipeline control-plane position (current step, refine iteration, carry_forward). Crash mid-pipeline restores sessions but loses WHERE it was. Spec §10 admits this, then claims the benefit as real elsewhere. Needs NEW WAL event kinds. |
| capabilities = 起動元からの縮小のみ (⊆-parent) | **MATCHES** | Real: `ContextualLayer` enforces `child ⊆ parent` (union-of-denials ∩ intersection-of-allows). `capability_profile.py`, `permission-model.md:556`. Caveat: DSL `capabilities:{tools:[...]}` must map to `CapabilityProfile.tool_allow/tool_deny`. |
| identity = 静的リテラル | **PARTIAL** | Named agents/profiles exist (`AgentProfile`, `profile.py`), but "run this step AS agent X" programmatically (non-LLM) has no wiring — net-new. |
| Pipeline 起動 = `run_pipeline` tool, permission 対象 | **ABSENT** | `grep run_pipeline src/` = 0 results. No pipeline tool, registry, or entry point. |

## Net-new infrastructure the spec requires (nothing exists)
Pipeline DSL parser (YAML→AST) · expression evaluator (`transform.value`/`until`/`verify.condition`/`fold.init`) · the Pipeline executor (step sequencing, retry/refine loops, for_each/parallel/fold dispatch) · pipeline registry (named lookup for `call`/`match`) · `run_pipeline` tool · static analyzer (§7.3's 6 items) · new WAL event kinds for control-plane state · pipeline-definition approval mechanism. **This is a runtime component of comparable scope to the control-IR layer that was just deleted (#2458–#2469).**

## Relationship to deleted control-IR / old skill (§9)
§9's analysis is structurally SOUND: the old skill failed because the control plane was non-authoritative (LLM decided transitions); Pipeline makes the DSL executor the sole transition authority — the correct inversion, NOT the same mistake. BUT it is a new execution layer of similar weight to what was just removed — worth a conscious "are we re-adding weight we just shed?" check.

## Relationship to task-system #2187 (loose trees)
Complementary, not competing: tasks = emergent/LLM-driven/non-deterministic; Pipeline = declarative/deterministic/verified-outputs+budget. A task can call a pipeline; a pipeline can create tasks. Overlap only in "sequential dependent steps" → needs a when-to-use-which design note (spec §0.6 doesn't fully disambiguate).

## Corrections the spec needs before it drives implementation
1. Rename "Global Journal" → WAL/StateLog; drop "pub/sub/OTEL" from Audit Event (or mark future).
2. Reframe §7 from "準拠 (already conforms)" → "reuses these existing mechanisms; the executor is net-new."
3. Crash-recovery is NOT free — spell out the new WAL event kinds needed for control-plane position (this is the recovery contract, per CLAUDE.md gate + truncate-falsify).
4. Fix the permission model description: axis allowlists + 4-layer approval, not per-tool Allow/Deny/Ask.
5. Define the expression language + `transform` execution surface (sandboxed python? CEL? Jinja?) — biggest undefined trust-boundary decision.

## New open questions (beyond spec §10)
- **Where does the stateless Pipeline executor live?** Reyn is entirely session/agent-centric (WAL, router loop, permissions all per-session). A non-session executor is an architectural decision (long-lived process? special session type?) affecting WAL, permissions (whose PermissionDecl?), recovery.
- **`run_pipeline` calling context**: how does the executor get the caller's PermissionDecl/profile to enforce ⊆-parent on child steps?
- **Stale `run_skill` refs in permission-model.md** (lines 109/193/421/449/451) describe the per-skill `ScopedSecretStore` credential model that no longer has a caller — Pipeline's `call` would need equivalent credential scoping.
- **for_each `max_parallel` vs `safety.spawn.max_children`**: does the existing spawn-tree governor bound Pipeline-spawned sessions, or does Pipeline need its own?
