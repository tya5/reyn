# B5R2-B: skill_improver Chain — Scenario B Retest 2

## Verdict: partial — B5-H2 prompt fix confirmed; KeyError 'name' persists (different root cause)

## Setup
- Clean `.reyn/`; default agent only
- Input: `skill_improver で direct_llm を 1 回 review して改善案を出して`
- HEAD: `fe91321` (B5-H2 fix applied)
- Runs: 1 (clean state)

## dogfood_trace summary

```
============================================================
DOGFOOD TRACE SUMMARY
============================================================

[Skill Chain]  (6 workflow(s))
  skill_improver (entry=prepare)  status=active     (ask_user — #142f, no target path)
  skill_improver (entry=prepare)  status=active     (ask_user — #7021, no target path)
  skill_improver (entry=prepare)  status=finished   (#e9b8 — ran full chain)
    phases: prepare -> copy_to_work -> run_and_eval -> plan_improvements -> apply_improvements -> finalize
  eval (entry=run_target)  status=finished          (2 entries — duplicated)
    phases: run_target -> evaluate
  skill_narrator (entry=narrate)  status=finished

[Tool Calls]  (20 important tool call(s))
  [ 1] invoke_skill("skill_improver")  x3 (parallel dispatch — B5-M1 repro)
  [ 4] ask_user(...)  x2 (missing target path — for parallel instances)
  [ 6] file(write, ".reyn/improver_state.json")
  [ 7] file(glob, "direct_llm/**/*.md")
  [10] file(read, "direct_llm/skill.md")  → content retrieved
  [13] file(write, ".reyn/skill_improver_work/direct_llm/skill.md")  content_len=0  ✗
  [17] run_skill(skill="eval", ...)
  [18] run_skill(skill=".reyn/skill_improver_work/direct_llm/skill.md", ...)  ← correct key ✅
  [19] control_ir_failed: KeyError 'name'  ← persists ✗

[Peer Failures / Chain Discards]  (0 event(s))
[Interventions]  dispatch=0  resolve=0
[Agent Messages]  (0 message(s))

=== Cost Summary ===
  Total: $0.000196  |  93,209 tokens  |  16 calls
  Per-model:
    gemini-2.5-flash-lite: $0.000196  1,646 tokens  (1 call — router)
    openai/gemini-2.5-flash-lite: $0.000000  91,563 tokens  (15 calls — skill phases)
```

## dogfood_trace chain

```
=== Skill / Tool Chain ===
[T+1.0s]  invoke_skill("skill_improver") x3
[T+1.0s]  workflow_started: skill_improver (#e9b8)
  [T+1.0s]  phase_started: prepare
  [T+4.0s]  file(write, .reyn/improver_state.json)
  [T+4.0s]  phase_completed: prepare
  [T+4.0s]  phase_started: copy_to_work
  [T+5.0s]  file(glob, direct_llm/**/*.md) → [skill.md, ...]
  [T+7.0s]  file(read, direct_llm/skill.md) → content OK
  [T+8.0s]  file(write, .reyn/skill_improver_work/direct_llm/skill.md) content=0 bytes  ✗
  [T+10.0s] phase_completed: copy_to_work
  [T+10.0s] phase_started: run_and_eval
  [T+11.0s] run_skill(skill="eval", ...)
    [T+11.0s] phase_started: run_target
    [T+13.0s] run_skill(skill=".reyn/skill_improver_work/direct_llm/skill.md") ← correct key ✅
    [T+13.0s] control_ir_failed: {"kind": "run_skill", "error": "'name'"}  ✗
    [T+14.0s] phase_completed: run_target  (score=0.0)
  [T+18.0s] phase_completed: run_and_eval
  [T+21.0s] phase_completed: plan_improvements
  [T+26.0s] phase_completed: apply_improvements  (retry=1 on first attempt)
  [T+28.0s] phase_completed: finalize
[T+28.0s]  workflow_finished skill_improver  score=0.0
[T+28.0s]  workflow_started: skill_narrator
[T+29.0s]  workflow_finished: skill_narrator
```

## B5-H2 fix effect assessment

**Prompt fix confirmed**: The LLM in `eval.run_target` now emits `run_skill` ops with
`"skill":` key (correct) instead of `"name":` or `"path":` (wrong). This is directly
observable from the tool_called event: `args_keys=['skill', 'input', 'model', ...]`.

**But `KeyError: 'name'` persists** — the error is NOT from the Control IR field name
mismatch. It comes from `src/reyn/compiler/parser.py:133`:

```python
name=fm["name"],  # KeyError when frontmatter has no 'name' field
```

The workspace file `.reyn/skill_improver_work/direct_llm/skill.md` is **0 bytes** because
`copy_to_work` reads the file content successfully but writes empty content. The `load_dsl_skill`
call in `run_skill.py` then calls `parse_skill` which fails on empty frontmatter.

**Root cause trace**:
1. `copy_to_work` calls `file(read, "direct_llm/skill.md")` → returns content ✅
2. `copy_to_work` calls `file(write, ".reyn/skill_improver_work/direct_llm/skill.md", content="")` ✗
3. `eval.run_target` calls `run_skill(skill=".reyn/skill_improver_work/direct_llm/skill.md")` (correct key)
4. `run_skill.py` → `load_dsl_skill(path)` → `parse_skill` → `fm["name"]` → `KeyError: 'name'`

The B5-H2 fix was a prompt instruction fix (correct) but it fixed the **wrong layer**.
The actual error `'name'` in B5-FV was always from empty workspace files, not from the
`"name"` vs `"skill"` key confusion. The B5-H2 prompt fix does eliminate one class of error
(wrong key in Control IR) but the observable `KeyError: 'name'` trace was misleading —
both errors produce `error: "'name'"` in the event log, making them hard to distinguish.

## copy_to_work empty-write bug (B5R2-H2)

**B5R2-H2 (HIGH)**: `copy_to_work` phase reads source files (content confirmed in result)
but writes 0-byte destination files. The LLM omits the content in the `file(write, ...)` op
despite reading it. This makes every eval cascade score 0.0 because the workspace skill files
are empty and fail to parse.

Likely cause: The LLM performs `file(read)` as an act step and `file(write)` in a separate
act step but does not carry the read content into the write content field. The phase instructions
may need explicit "use the content from the previous read result as the write content" instruction.

## Narrative reply delivery (B4-H1 fix effect)

The skill_narrator ran and delivered a message to the user:
> `スキル「skill_improver」は0.0のスコアで終了し、変更はありませんでした。`

This confirms B4-H1 narrator-reply path works. The reply reached the user even though
the chain score was 0.0.

## Reproduced findings from B5-FV

- **B5-M1**: Three parallel `skill_improver` invocations (3× `invoke_skill`) when user
  asked for 1 review. Still present.
- **B5-M2**: `phase_retry` on `apply_improvements` (1 retry). Still present.

## Expected checklist

| Check | Result |
|-------|--------|
| copy_to_work workspace 作成成功 | ✅ (files created, but empty) |
| eval cascade 起動 | ✅ |
| run_skill Control IR に `skill:` key 使用 (B5-H2 prompt fix) | ✅ confirmed |
| KeyError 'name' 解消 | ✗ (persists — different root: empty workspace files) |
| eval score non-zero | ✗ (0.0 — empty workspace files fail to parse) |
| 改善案が user に届く | ✅ (narrator delivered summary, even with score=0.0) |
| B4-H1 narrator reply confirmed | ✅ |
