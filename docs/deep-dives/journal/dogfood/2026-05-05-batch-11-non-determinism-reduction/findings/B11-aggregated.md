# B11 Aggregated Summary — Non-determinism Reduction Wave

| Field | Value |
|---|---|
| Date | 2026-05-06 |
| main HEAD | `2d892e6` |
| Test suite | 1016 passed / 2 xfailed |
| Batch | 11 (non-determinism reduction wave) |
| Theme | Fix accumulation → stability measurement transition |

## Fix Contribution Summary (R1 + R2 + R3)

### R1 (B10-NEW-1): `_resolved_paths` schema preservation

| Field | Value |
|---|---|
| Commit | `4898ef9` |
| Target bug | Path hallucination (`/tmp/reyn-workspace/...` vs `/tmp/reyn_workspace/...`) |
| Root cause | `_resolved_paths` field absent from `improvement_session.yaml` → `_strip_data` silently removes it |
| Fix | Added `_resolved_paths` to schema properties + required list + CRITICAL instruction in copy_to_work.md |
| Verdict | **partially confirmed** — copy_to_work preprocessor step[0] (compute_paths) succeeds in all partial sessions; path hallucination not observed |
| Limitation | Cannot confirm full path preservation because copy_to_work is blocked by step[1] (B11-NEW-1) before run_and_eval is reached |

### R2 (G12 Pattern D): `describe_skill` routing strip

| Field | Value |
|---|---|
| Commit | `2d892e6` |
| Target bug | G12 empty-stop attractor after describe_skill (1381→~200 char strip of routing+category fields) |
| Root cause | `describe_skill` returned full routing block (~1000 chars), P-b threshold crossed |
| Fix | `_DESCRIBE_SKILL_STRIP_FIELDS = frozenset({"routing", "category"})` in `_describe_skill()` |
| N-shot pre-fix rate | 50% (5/10 replay on B10-S5b trace) |
| N-shot post-fix rate | 0% (0/10 replay with routing stripped) |
| Verdict in Step 2 | **not triggered** — no G12 empty-stop observed in any session; 0/5 attractor events |
| Note | Sessions 1 and 4 reached invoke_skill and ran phases without empty-stop; R2 fix appears effective but masked by B11-NEW-1 blocker |

### R3 (B9-NEW-3/B10-NEW-2): Router direct invoke rule

| Field | Value |
|---|---|
| Commit | `2c14aa6` |
| Target bug | Router emitting text-reply clarification instead of invoke_skill when skill name is in Available skills |
| Fix | Rule change in `router_system_prompt.py`: "If user names a skill in Available skills, call invoke_skill directly (skip list_skills)" |
| Pre-fix rate | 50-60% text-reply per session |
| Step 2 rate | 60% text-reply (3/5 sessions) |
| Verdict | **inconclusive** — fix fires in 2/5 sessions; 3/5 remain text-reply; no statistically significant improvement |
| Pattern | When R3 fires (runs 1, 4): direct invoke_skill without list_skills hop. When it doesn't (runs 2, 3, 5): text-reply clarification with 1 LLM call |

## Cumulative Effect (R1 + R2 + R3 combined)

| Metric | Batch 10 baseline | B11 Step 2 (N=5) | Delta |
|---|---|---|---|
| Complete rate | 50% (1/2, small N) | 0% (0/5) | -50pp |
| Routing-fail rate | 50% (1/2) | 60% (3/5) | +10pp |
| Partial rate | 0% (0/2) | 40% (2/5) | +40pp |
| G12 attractor rate | ~25-50%/session | 0% | -25pp (R2 effective) |

**Interpretation**: The cumulative fix stack (R1+R2+R3) improved the route structure in
sessions that pass routing (R3 fires + R2 prevents attractor), but introduced no net
complete rate improvement because copy_to_work file.read permission_denied (B11-NEW-1 =
B8-NEW-1) is a hard blocker for all sessions that reach the skill chain. The batch 10
"50% complete" was achieved in a specific state where this blocker did not gate; current
state regresses on complete rate despite structural improvements.

## B11-NEW findings

### B11-NEW-1: copy_to_work preprocessor step[1] file.read permission_denied

**Category**: C5 (design-vs-runtime gap), already tracked as B8-NEW-1  
**Severity**: HIGH — blocks every partial session  
**Error**: `Phase 'copy_to_work' preprocessor step[1] run_op (file): read from '<stdlib>/direct_llm/skill.md' was not approved`  
**Sessions affected**: Run 1, Run 4 (all partials)  
**Fix direction**: Add stdlib skills path glob to `skill_improver`'s permissions frontmatter, or have OS auto-approve `run_op` preprocessor file reads for paths within declared skill directories  
**Note**: This is the same bug as B8-NEW-1. It was present before batch 11 but was not the gating issue in the one batch 10 complete session (Run 2 of S1). Now confirmed as the dominant blocker.

### B11-NEW-2: R3 routing fix non-deterministic (60% text-reply persists)

**Category**: C1 (model-capability-tradeoff) / C7 (prompt-vs-bloat-tradeoff)  
**Severity**: MED — 60% sessions fail at routing, blocking chain start  
**Observation**: Despite the structural rule change ("If user names a skill in Available skills list, call invoke_skill directly"), 3/5 sessions produced text-reply clarification. The LLM inconsistently recognizes `skill_improver` as an entry in the Available skills list.  
**Pattern observed in failures**: Router replies with detailed explanation of what skill_improver and direct_llm do, asking for clarification about improvement criteria  
**Fix direction candidates**:
1. Ensure `skill_improver` appears in the Available skills list rendered in system prompt (verify injection is working)
2. Strengthen the rule with an example that exactly matches the test input pattern (Japanese `で` routing)
3. Reduce Available skills list verbosity to help the LLM scan more reliably

## Batch 12 Candidates

Priority order based on B11 findings:

| Priority | Bug | Impact | Fix direction |
|---|---|---|---|
| 1 (CRITICAL) | B11-NEW-1 (copy_to_work file.read perm) | Blocks all partial sessions; 0% complete rate without this fix | Add stdlib skill path glob to skill_improver permissions frontmatter |
| 2 (HIGH) | B11-NEW-2 (R3 routing non-determinism) | 60% sessions blocked at routing | Verify Available skills list injection; add Japanese-input routing example |
| 3 (MED) | R1 full verification | Cannot confirm _resolved_paths preservation without reaching run_and_eval | Depends on B11-NEW-1 fix; re-verify in batch 12 |
| 4 (LOW) | G4 spike (strong model trial) | R2 fix reduced G12 to 0%, but weak LLM routing non-determinism ceiling may require strong model | After B11-NEW-1 + B11-NEW-2 fixed |

### Recommended batch 12 approach

**Step 1**: Fix B11-NEW-1 (copy_to_work permissions) — deterministic fix, high confidence  
**Step 2**: Fix B11-NEW-2 (R3 routing) — diagnose Available skills list injection  
**Step 3**: Integration 5-shot retest  
**Target**: 60%+ complete rate (3/5)

The batch 10 single complete run demonstrated the chain CAN complete end-to-end. With
B11-NEW-1 fixed, the blocking issue for partial sessions is removed. The remaining risk
is 60% routing-fail non-determinism (B11-NEW-2).

## Calibration Assessment (vs batch 11 prelude)

| Prediction | Actual | Assessment |
|---|---|---|
| 60-70% complete rate (3-4/5) | 0% (0/5) | **Miss** — copy_to_work perm blocker not factored in |
| R3 routing-fail ~10% or less | 60% routing-fail | **Miss** — R3 fix is non-deterministic at 60% residual |
| R2 G12 attractor ~0% | 0% in Step 2 sessions | **Verified** — no G12 observed in any session |
| R1 path hallucination resolved | Partial — step[0] succeeds | **Partial** — cannot fully verify without reaching run_and_eval |

**Root of prediction miss**: The prelude assumed copy_to_work would not be the gating
blocker, based on the batch 10 complete run. In batch 10, the one complete session (S1
Run 2) happened to pass copy_to_work; the blocker was not consistently triggered. Batch
11 reveals this was not consistent — it blocks deterministically at step[1] in this
worktree configuration. The prelude should have treated B8-NEW-1 (copy_to_work perm) as
a known open blocker that needed to be in the fix list.

**Lesson**: When a "known bug" was not blocking in the previous run, it does not mean it
is fixed — it may have been bypassed by a non-deterministic factor. Always verify known
open bugs before measuring stability.
