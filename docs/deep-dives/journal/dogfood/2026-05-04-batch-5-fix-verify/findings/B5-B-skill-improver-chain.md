# B5-B: skill_improver Chain — Scenario B Fix-Verify

## Verdict: ✅ PARTIAL — B4-H2 fix confirmed; eval cascade blocked by new B5-H2

## Setup
- Clean `.reyn/`; default agent only
- Input: `skill_improver で direct_llm を 1 回 review して改善案を出して`
- Runs: 1 run (no retry needed — chain ran to completion with timeout on second instance)

## dogfood_trace summary (key excerpts)

```
[Skill Chain]  (13 workflow(s))
  skill_improver (entry=prepare)  status=finished
    phases: prepare -> copy_to_work -> run_and_eval -> plan_improvements -> apply_improvements -> finalize
  skill_improver (entry=prepare)  status=active  (timed out, second parallel invocation)
  eval (entry=run_target)  status=finished  (x4 runs)
  skill_narrator (entry=narrate)  status=finished  (x2 runs)

[Tool Calls]  (58 important tool call(s))
  invoke_skill("skill_improver")  x3 (parallel)
  copy_to_work writes: .reyn/skill_improver_work/my_app/  (4 files)
  copy_to_work writes: .reyn/skill_improver_work/test_skill/  (5 files)
  run_skill("eval", ...)  x4 (nested evals)
  run_skill(".reyn/skill_improver_work/*/skill.md", ...)  → control_ir_failed x8

[Peer Failures / Chain Discards]  (0 event(s))

=== Cost Summary ===
  Total: $0.000198  |  333,099 tokens  |  51 calls
  Model: openai/gemini-2.5-flash-lite (50 calls) + gemini-2.5-flash-lite (1 call)
```

## dogfood_trace chain (key events)

```
[T+2.0s]  workflow_started: skill_improver
  [T+2.0s]  phase_started: prepare
  [T+4.0s]  tool: file(write, .reyn/improver_state.json)
  [T+4.0s]  phase_started: copy_to_work
  [T+10.0s] tool: file(glob, reyn/local/my_app/**/*.md)  → success
  [T+11.0s] tool: file(write, .reyn/skill_improver_work/my_app/...)  x4
  [T+13.0s] phase_completed: copy_to_work
  [T+15.0s] tool: run_skill("eval", ...)  ← nested eval starts
    [T+16.0s] tool: run_skill(".reyn/skill_improver_work/my_app/skill.md")
    [T+16.0s] control_ir_failed: {"kind": "run_skill", "error": "'name'"}
  [T+24.0s] run_skill_completed: eval  status=finished  (but score=0.0)
  [T+26.0s] phase_started: plan_improvements
  [T+28.0s] phase_started: apply_improvements
  [T+38.0s] phase_started: finalize
  [T+41.0s] workflow_finished  (skill_improver run #1)
  [T+41.0s] workflow_started: skill_narrator
    [T+41.0s] phase_started: narrate
    [T+43.0s] phase_completed: narrate
```

## B4-H2 fix verification

| Check | Result |
|-------|--------|
| `copy_to_work` creates workspace dir | ✅ `.reyn/skill_improver_work/my_app/` created (4 files) |
| `copy_to_work` creates workspace dir | ✅ `.reyn/skill_improver_work/test_skill/` created (5 files) |
| `copy_to_work` completes within 5-6 turns | ✅ Completed in ~4 file ops (well within budget) |
| glob scope correct | ✅ `reyn/local/my_app/**/*.md` (not wildcard) |
| eval cascade runs | ✅ eval workflow starts and runs `run_target → evaluate` |
| eval score non-zero | ✗ score=0.0 (new bug blocks it — see B5-H2) |
| skill_improver delivers improvement summary | ✅ narrator ran; user received summary |

## New finding: B5-H2 (HIGH)

`control_ir_failed: {"kind": "run_skill", "error": "'name'"}` in `eval.run_target`

The `eval` skill's `run_target` phase emits `run_skill` Control IR with a **full path** as the skill
identifier (e.g., `.reyn/skill_improver_work/my_app/skill.md`). The OS's `run_skill` handler
expects a `name` key but receives a path string as the skill reference, causing `KeyError: 'name'`.

This blocks all nested `run_skill` calls from `eval.run_target`, making every eval score 0.0.

Affects: skill_improver → eval → run_target chain (all nested skill runs fail).
Fix needed in: `src/reyn/op_runtime/` run_skill handler or eval skill's Control IR shape.

## Additional findings

**B5-M1 (MED)**: Three parallel `skill_improver` invocations launched simultaneously (3× `invoke_skill`)
when the user asked for 1 review. The router parallelized where it should have been single. This wastes
tokens (333k total vs ~6k for Scenario A which stayed shallow).

**B5-M2 (MED)**: `phase_retry` events on `plan_improvements` and `apply_improvements` (each retried
once). The model emits structurally invalid Control IR on first try, requiring retry. This is a prompt
or output-format compliance issue in those phases.

## Expected checklist

| Check | Result |
|-------|--------|
| copy_to_work 5-6 turn で完結 | ✅ |
| eval cascade 成功 | ✗ (score=0.0 due to B5-H2) |
| 改善案 (1 段落) が user に届く | ✅ (narrator delivered summary) |
