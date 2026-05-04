---
id: B9-G16
batch: 9
date: 2026-05-05
bug_ref: B8-NEW-5 / giveup-tracker G16
status: resolved
verdict: fixed (wording)
---

# B9-G16: eval_builder routing wording fix (G16 / B8-NEW-5)

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD at dispatch | `d1f2d30` |
| Fix commit | (see commit SHA below) |
| Verdict | **resolved** — wording fix applied, 9 Tier 1 contract tests added |

## Root cause

`direct_llm の eval を作って` was misrouted to the `eval` skill (run evaluations) instead
of `eval_builder` (create eval spec). The `eval` skill ran `direct_llm` as a test target
through `judge_phase` → `narrator`, completing silently with wrong behavior: no `eval.md`
generated.

Two disambiguation failures:
1. `eval_builder` description: `Auto-generate an eval spec (eval.md) for a skill` — no contrast verb
2. `eval_builder` when_not_to_use: mentioned "use eval instead" but not explicitly tied to run/execute intent
3. `eval` skill: no `when_not_to_use` mention of `eval_builder` for create intent

## Fix applied

### eval_builder/skill.md

**Description** (before → after):
- Before: `Auto-generate an eval spec (eval.md) for a skill` (42 chars)
- After: `Build an eval spec (eval.md) — to run evaluations use the eval skill instead` (76 chars)

G12 constraint: 76 ≤ 80 chars — full description visible in list_skills without truncation.
Distinctive verb `Build` in position 0; explicit contrast "use the eval skill instead".

**when_to_use** additions:
- `User wants to *create* / *build* / *generate* an eval spec (eval.md) for a skill`
- `Typical input form is "SKILL_NAME の eval を作って" or "eval.md を生成して"`

**when_not_to_use** additions:
- `Intent is "eval を実行する" or "SKILL_NAME を eval して" — use eval skill, not eval_builder`
- `eval_builder creates the spec; eval runs it — for "eval して" choose eval, not eval_builder`

**examples.positive** (added G16 bug input):
- `"direct_llm の eval を作って"` (was `"X skill 用の eval を作って"`, now both present)

**examples.negative** (strengthened):
- `"direct_llm を eval して"` (added direct_llm form matching G16 actual bug input)
- `"skill X を eval して"` (retained)

### eval/skill.md (symmetric fix)

**when_to_use** additions:
- `Typical input form is "SKILL_NAME を eval して" or "eval を実行"`

**when_not_to_use** additions:
- `Intent is "eval を作って" or "eval.md を生成して" — use eval_builder, not eval`
- `eval runs the spec; eval_builder creates it — for "eval を作って" choose eval_builder`

**examples.negative** additions:
- `"direct_llm の eval を作って"` (explicit contrast, G16 actual bug input)
- `"eval.md を生成して"` (added)

## Test approach

**Tier 1 Contract tests** (9 tests, `tests/test_g16_eval_builder_routing_wording.py`):

| Test | Guards |
|---|---|
| `test_eval_builder_description_starts_with_build` | description[0] == 'Build' |
| `test_eval_builder_description_within_80_chars` | len(description) ≤ 80 (G12 safe) |
| `test_eval_builder_description_mentions_eval_skill_contrast` | 'eval' in description |
| `test_eval_builder_when_not_to_use_mentions_eval_skill` | when_not_to_use has eval + run contrast |
| `test_eval_builder_when_not_to_use_distinguishes_create_vs_run` | at least 1 bullet has both concepts |
| `test_eval_builder_positive_examples_include_eval_wo_tsukutte` | 'eval を作って' in positive |
| `test_eval_builder_negative_examples_include_eval_shite` | 'eval して' in negative |
| `test_eval_skill_when_not_to_use_mentions_eval_builder` | eval's when_not_to_use has eval_builder |
| `test_eval_skill_negative_examples_include_eval_wo_tsukutte` | eval's negative has 'eval を作って' |

Tier 3 (LLMReplay) not added: weak LLM stochastic behavior makes a 1-shot replay
insufficient to reliably catch routing ambiguity. Tier 1 pins the DSL contract that
feeds the router; the wording is the load-bearing fix.

## Test run results

```
1000 passed, 2 xfailed in 72.29s
```

(991 pre-existing + 9 new = 1000 passed)

## Description constraint verification

```
Description: 'Build an eval spec (eval.md) — to run evaluations use the eval skill instead'
Length: 76 chars
≤ 80: YES (full text visible in list_skills, no truncation)
First word: 'Build' (distinctive verb — differs from eval skill)
```

## giveup-tracker update

G16 status: **active → resolved** at this commit.
