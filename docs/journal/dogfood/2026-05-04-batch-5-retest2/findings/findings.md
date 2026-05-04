# Batch 5 Retest 2 — Findings Index

**Date**: 2026-05-04  
**HEAD**: `ca116f3` + `fe91321` (B5-H1 + B5-H2 both applied)  
**Scenarios**: A (curry/specialist) + B (skill_improver eval cascade)

## Fix verification summary

| Fix | Target bug | Status | Evidence |
|-----|-----------|--------|----------|
| B5-H1 (`ca116f3`) | specialist stops after list_skills | partial | specialist now reaches describe_skill but not invoke_skill |
| B5-H2 (`fe91321`) | eval run_target KeyError 'name' | partial | prompt fix confirmed; error persists from different root cause |
| B4-H1 (narrator reply) | skill result not reaching user | confirmed | narrator delivered score=0.0 summary to user |

## New findings

| ID | Severity | Description |
|----|----------|-------------|
| B5R2-H1 | HIGH | describe_skill stop: gemini-2.5-flash-lite exits after describe_skill without invoking. B5-H1 added one step but invoke_skill still unreached. |
| B5R2-H2 | HIGH | copy_to_work writes 0-byte files: reads source correctly but LLM omits content in write op. Empty workspace files cause parse failure and score 0.0. |

## Scenario files

- `prelude.md`
- `B5R2-A.md`
- `B5R2-B.md`
