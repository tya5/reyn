# Batch 5 Fix-Verify: Findings Index

**Date**: 2026-05-04  
**HEAD**: `30fdc33` (B4-H1 + B4-H2 + prompt-consolidation applied)  
**Scenarios**: A (curry/specialist) + B (skill_improver chain)

## Summary

| Scenario | Expected | Verdict | Notes |
|----------|----------|---------|-------|
| A: curry via specialist | B4-H1 fix confirmed | FAIL | B4-H1 never triggered — specialist never invokes skill |
| B: skill_improver chain | B4-H2 fix confirmed | PARTIAL | workspace created; eval cascade blocked by new B5-H2 |

## Findings

### New HIGH findings

| ID | Scenario | Description |
|----|----------|-------------|
| B5-H1 | A | gemini-2.5-flash-lite does not invoke skills after list_skills. Prompt consolidation (e90c0f2) weakened the commit-obligation rule. Specialist always returns empty after discovery. |
| B5-H2 | B | control_ir_failed run_skill error='name' in eval.run_target. Nested run_skill with full-path skill reference fails with KeyError 'name' in OS handler. Blocks all eval scores to 0.0. |

### New MED findings

| ID | Scenario | Description |
|----|----------|-------------|
| B5-M1 | B | Router launches 3 parallel skill_improver invocations for a single-review request. Should be 1. |
| B5-M2 | B | phase_retry on plan_improvements + apply_improvements on first attempt (invalid Control IR). |

## Fix verification status

| Fix | Commit | Verified? | Notes |
|-----|--------|-----------|-------|
| B4-H1: narrator reply to agent_replies | ffc9b4a | NOT TESTED | Prerequisite (invoke_skill in specialist) blocked by B5-H1 |
| B4-H2: copy_to_work budget 3-to-6 + glob scope | d9787cb | CONFIRMED | Workspace dirs created; 4-file copy completes in budget |
| prompt consolidation | e90c0f2 | REGRESSION | Consolidation appears to have weakened commit-obligation signal causing B5-H1 |

## Recommended next actions

1. B5-H1 (blocker): Restore explicit invoke_skill commit rule as separate bullet in router system prompt.
2. B5-H2 (blocker): Fix run_skill Control IR handler to accept full-path skill references.
3. B5-M1: Limit invoke_skill parallelism to 1 for skill_improver.
4. Re-run Scenario A after B5-H1 fix to properly verify B4-H1.
