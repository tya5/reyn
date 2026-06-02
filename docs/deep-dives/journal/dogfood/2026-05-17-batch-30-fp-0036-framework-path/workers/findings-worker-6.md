# B30 Worker 6 Findings

**Batch**: B30 | **Worker**: 6/7 | **HEAD**: 4be42fe | **Date**: 2026-05-17

---

## Summary

| Scenario set | Scenarios run | V | I | R | B |
|---|---|---|---|---|---|
| plan_mode.yaml | 3 | 0 | 2 | 1 | 0 |
| fp_0011_narration.yaml (enabled: narr-1, narr-3) | 2 | 0 | 2 | 0 | 0 |
| fp_0011_0012_retest.yaml | 6 | 0 | 3 | 2 | 0 (2 env-blocked) |
| **Total** | **11** | **0** | **7** | **2** | **0** |

B28 baseline: V/I/R/B = 0/10/0/1. Delta: +2R, -3I, -1B.

---

## B29-MED-3 Verification

### Verdict: PARTIAL (cwd injected, step LLMs still use bare filenames)

**System prompt excerpt** (request_id: 90f62873, plan step S1):
```
You are at project root: /private/tmp/reyn-worktrees/b30-6

You are a Reyn agent executing one step of a multi-step plan. ...

## Plan goal
Read and synthesize information from principles.md, architecture.md, and workspace.md

## Your task
Read the principles.md file.
```

**Step LLM tool_calls**:
```
reyn_src_read({"path": "principles.md"})
```

**Tool response**: "The file `principles.md` does not exist in the Reyn repository."

The B29-MED-3 fix (`build_plan_step_system_prompt` cwd injection at line 374 of planner.py) successfully injects "You are at project root: /private/tmp/reyn-worktrees/b30-6" into every plan step system prompt. However the fix is insufficient because:

1. The router LLM generates plan step descriptions with bare filenames ("Read the principles.md file") extracted from the user query.
2. Step LLMs follow their description and call `reyn_src_read("principles.md")`.
3. `reyn_src_read` requires repo-root-relative paths (`docs/concepts/architecture/principles.md`).

All 3 plan steps observed across 2 plan instances (request_ids: 90f62873, 27823dfb, 1f334861) called `reyn_src_read({"path": "principles.md"})` with the same bare filename.

---

## plan_mode.yaml Results

### plan_compare_two_concepts — INCONCLUSIVE
LLM read both files via `read_file` tool directly (not plan) and replied inline. No `plan_emitted`. `chat_turn_completed_inline` fired (Q2 verified). Reply grounded in both docs.

### plan_explain_with_code_references — REFUTED
LLM called `describe_action(plan)`, `describe_action(skill__plan)`, `list_actions`. Did not read plan.py or planner.py. Replied: "I cannot execute the plan tool directly as it requires a category prefix." No code references, no plan invocation.

### plan_summary_across_n_files — INCONCLUSIVE
Plan emitted (4 total across concurrent runs, 2 distinct plans). Steps started ×8, completed ×5. `plan_run_interrupted` ×4 via CancelledError (stdin closed). B29-MED-3: cwd injected, bare filename used, file lookup failed. plan_emitted: 1/3 plan_mode scenarios.

---

## fp_0011_narration.yaml Results

### narr-1-mcp-search — INCONCLUSIVE (env)
mcp_search failed: "unsafe python step, --allow-unsafe-python not provided." Anti-optimism PASS: error surfaced verbatim. Cannot assess narration quality (no successful run).

### narr-3-skill-builder — INCONCLUSIVE (quadruple dispatch + async exit)
Router spawned skill_builder 4 times. 3 completed, 1 failed schema validation. Chat exited before skill_completion_injected + narration turn. D5 anti-double-dispatch FAIL (4x).

---

## fp_0011_0012_retest.yaml Results

### s-fp11-1 (builder invalid spec) — INCONCLUSIVE
Router asked clarification, then built valid skill (status: finished). D1 anti-optimism: NOT TRIGGERED (LLM sanitized input, bypassed error path).

### s-fp11-2 (eval missing target) — INCONCLUSIVE
eval invoked with double dispatch (2x). Skill failed "target not found." Chat exited before narration delivered. D1 observable in events, not in narration.

### s-fp11-3 (mcp_search empty) — INCONCLUSIVE (env)
Same --allow-unsafe-python blocker. Anti-optimism fired: error narrated verbatim. Cannot test empty-result path.

### s-fp12-spawn-1 (builder success ack) — REFUTED
Triple dispatch (3 `skill_run_spawned`). Spawn ack: "skill `string_length` はバックグラウンドで実行中です。完了したら通知します。" — 1 sentence PASS, `/tasks` pointer ABSENT = D2 partial FAIL. D5 FAIL.

### s-fp12-completion-1 (mcp_search narrate) — INCONCLUSIVE (env)
--allow-unsafe-python blocker. skill_run_failed.

### s-fp12-completion-2 (error async narrate) — REFUTED
Double dispatch (2x). skill_builder completed with status=finished (LLM did not generate cyclic graph). Router echoed user request fragment as reply. D1/D3 FAIL.

---

## Cross-cutting Issues

1. **Double/triple/quad dispatch (HIGH)**: Multiple `skill_run_spawned` per request across narr-3 (4x), s-fp12-spawn-1 (3x), s-fp12-completion-2 (2x), s-fp11-2 (2x). Router calls invoke_action multiple times across sequential routing turns.

2. **--allow-unsafe-python env gap (MED)**: 3/6 fp_0011_0012_retest scenarios and 1/2 narr scenarios blocked. Worktree needs `trusted_python_allowed=True` patch or `--allow-unsafe-python` flag.

3. **B29-MED-3 path gap (MED)**: Cwd injected but router generates bare filenames in step descriptions. Fix needs to propagate full paths into plan step descriptions.

4. **/tasks pointer missing from spawn ack (LOW)**: s-fp12-spawn-1 spawn ack omits `/tasks` pointer per D2 rubric.

---

## Q2 / C1 Observations

**Q2 (chat_turn_completed_inline)**: Confirmed firing in plan_compare scenario (inline reply, no plan) and narr-1/fp11-s3 after error path. Working correctly.

**C1 (no duplicate decl)**: No duplicate plan_emitted for same plan_id observed. 4x plan_emitted in plan_summary was 4 distinct plan objects. C1: NO VIOLATION.
