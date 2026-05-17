# B32 Worker 2 — Dogfood Findings

**Batch**: B32 Worker 2/7  
**Scenario set**: `dogfood/scenarios/stdlib_skills_core.yaml` (9 scenarios)  
**HEAD**: `c8fae2e`  
**Date**: 2026-05-17  
**Retest of**: B30 Worker 2 (V/I/R/B = 1/3/5/0)

---

## Summary Table

| ID | Scenario | Outcome | Key finding |
|----|----------|---------|-------------|
| S1 | index_docs_basic | **refuted** | routing+phases OK; postprocessor SafeModeViolation (glob import blocked) kills skill_run_completed:success |
| S2 | read_local_files_explain_source | **verified** | full event chain + skill_run_completed:finished; MCP unavailable, reply honestly disclosed |
| S3 | read_local_files_multi_file | **refuted** | LLM called list_actions(category=["file"]) -> 0 results -> inline reply; no routing_decided/skill_run_spawned |
| S4 | skill_builder_web_summariser | **inconclusive** | routing+8 phases correct; skill_run_interrupted (CancelledError) when stdin closed |
| S5 | word_stats_demo_sentence | **inconclusive** | skill_run_completed:finished; python preprocessor stats all zeros; LLM inferred correct values |
| S6 | word_stats_demo_multiline | **inconclusive** | skill_run_completed:finished; reply incorrectly said "not a valid action" despite skill completing |
| S7 | eval_run_direct_llm | **inconclusive** | routing+skill__eval correct (NEW-2 verified); 3 parallel evals all skill_run_interrupted |
| S8 | chat_compactor_long_session | **inconclusive** | 5 turns inline; compaction not triggered (all within head window); no skill_run_spawned |
| S9 | chained_find_then_index | **refuted** | T1 incorrect (no files found); T2 routing to skill__index_docs correct (NEW-1); interrupted |

**Final tally: V=1, I=5, R=3, B=0**  
**Delta vs B30: +0V, +2I, -2R, 0B**

---

## NEW-1 Verify (S1, S9)

**Verified.**

S1 first LLM request (`fcd032ea`, cold start, tools=17):

```
skill__index_docs: PRESENT
skill__eval: PRESENT
Tool names: plan, list_actions, describe_action, invoke_action, file__read,
  file__list, reyn.source__list, web__search, web__fetch,
  memory.operation__remember_shared, skill__skill_builder, skill__skill_improver,
  skill__skill_importer, skill__mcp_search, skill__read_local_files,
  skill__index_docs, skill__eval
```

LLM tool_calls (S1, first turn):
```
invoke_action(action_name="skill__index_docs", args={"corpus_name": "concepts_index", "path_glob": "docs/concepts/"})
```

No hallucinated `rag.operation__add_source` or `rag.operation__create_index`. NEW-1 fix working.

S9 turn 2 also confirmed: 2x `routing_decided` both to `skill__index_docs`.

---

## NEW-2 Verify (S7)

**Verified.**

S7 first LLM request (`d993e228`, cold start, tools=17, `skill__eval` present):

LLM tool_calls (first turn):
```
invoke_action(action_name="skill__eval", args={"test_case_input": "日本語で「hello」を翻訳してください", "criteria": "「こんにちは」を含む応答が返ること", ...})
```

No hallucinated `skill__direct_llm_eval`. NEW-2 fix working.

---

## Detailed Findings

### S1 — index_docs_basic — REFUTED

Events: routing_decided ✓ (skill__index_docs), skill_run_spawned ✓, phase_started ✓ (strategy), phase_completed ✓ (strategy→end), 2 python preprocessor steps completed ✓.  
Missing: `skill_run_completed:success` — got `skill_run_failed`.

Root cause: Postprocessor `__post__` ran `extract_and_split` in safe mode despite `--allow-unsafe-python` flag being passed. The step imports `glob` which is blocked in safe mode. Preprocessor steps honored the flag; postprocessor did not. This is a mode-inheritance bug in the postprocessor runner.

Note: First run without `--allow-unsafe-python` failed immediately at phase entry. Second run with flag got further (strategy phase completed) but failed at postprocessor.

---

### S2 — read_local_files_explain_source — VERIFIED

Events: routing_decided ✓ (2x: skill__read_local_files + reyn.source__read), skill_run_spawned ✓, skill_run_completed:finished ✓, phase_started ✓ (decide_files + read_and_respond), no permission_denied event ✓.

Reply: "access denied error for the requested paths" — MCP filesystem server unavailable; reply meets rubric ("if MCP unavailable, says so"). The `tool_failed` events have `error_kind: permission_denied` but this is NOT the `permission_denied` event type (must_not_emit check passes).

---

### S3 — read_local_files_multi_file — REFUTED

Events: tool_called (list_actions) ✓, tool_returned (0 items) ✓, chat_turn_completed_inline.  
Missing: routing_decided, skill_run_spawned, phase_started.

LLM chose `list_actions(category=["file"], filter="src/reyn/op_runtime/")` → 0 results → inline reply saying cannot access directory. Did not invoke `skill__read_local_files`. The skill description ("Read one or more local project files") does not clearly signal directory browsing; LLM tried file catalog search instead.

---

### S4 — skill_builder_web_summariser — INCONCLUSIVE

Events: routing_decided ✓, skill_run_spawned ✓, phase_started ✓ (8 visits: plan_skill, design_artifacts, review_plan, build_skill×4, verify_skill×3), lint_completed ✓ (2x), skill_run_interrupted (CancelledError, will_resume=true).

Skill was actively building (2 rollbacks from verify_skill back to build_skill for lint corrections). Reply correctly said "/tasks" (SPAWN-ACK). Skill was still in progress when stdin closed — expected behavior for long-running async skill.

---

### S5 — word_stats_demo_sentence — INCONCLUSIVE

Events: All required events fired. routing_decided ✓, skill_run_spawned ✓, skill_run_completed:finished ✓, phase_started ✓, llm_called ✓, no permission_denied ✓.

Bug: Python preprocessor computed zeros:
```json
{"stats": {"char_count": 0, "word_count": 0, "line_count": 0, "longest_line_chars": 0, "estimated_tokens": 1}}
```
LLM's text_review artifact: "44 characters over 1 line, ~11 tokens" — correct values derived from LLM inference, not preprocessor output.

Rubric: values mentioned in reply are correct for the input, but they were inferred not precomputed.

---

### S6 — word_stats_demo_multiline — INCONCLUSIVE

Events: routing_decided ✓ (2x), skill_run_completed:finished ✓, phase_started ✓.

Reply: "I'm sorry, I cannot fulfill this request. The tool `word_stats_demo` is not a valid action." — despite skill having completed (status=finished). This is a task_completed handler narration failure: agent received completion event but narrated it as an error.

Two routing_decided events (skill__word_stats_demo then word_stats_demo) may have confused the handler. This is Issue E from the recurring list.

---

### S7 — eval_run_direct_llm — INCONCLUSIVE

Events: routing_decided ✓ (3x, all skill__eval), skill_run_spawned ✓ (3x), phase_started ✓ (3x run_target), phase_completed ✓ (some respond phases), 3x skill_run_interrupted.

Router dispatched eval 3 times in parallel (likely plan-mode multi-step dispatch). All 3 instances interrupted when stdin closed. Reply "/tasks" — correct SPAWN-ACK.

NEW-2 fully verified: skill__eval in hot-list on cold start, LLM invoked directly.

---

### S8 — chat_compactor_long_session — INCONCLUSIVE

Events: 5 user_message_received ✓, 5 compaction_check (all too_few_turns), 0 skill_run_spawned, 0 routing_decided.

All 5 messages answered inline. Compaction could never trigger: head_size=12 means all 5 turns are within the head window (never compacted). The scenario design cannot trigger compaction with current config.

Final reply content (routing/catalog explanation) meets rubric on quality. But skill_run_spawned was not emitted, which the scenario requires (count>=1).

---

### S9 — chained_find_then_index — REFUTED

Turn 1: LLM called list_actions → 0 results → replied "docs/concepts/ 以下には Markdown ファイルがありませんでした" (incorrect — files exist). Same routing miss as S3.

Turn 2: Agent correctly dispatched skill__index_docs twice (for architecture.md and events.md). Both routing_decided ✓, skill_run_spawned ✓, phase_started ✓ (strategy). Both interrupted when stdin closed.

Missing: skill_run_completed:success. Turn 1 answer also incorrect.

NEW-1 confirmed again on Turn 2.

---

## Recurring Issues

### A: SafeModeViolation in index_docs postprocessor (S1, S9)
`--allow-unsafe-python` not passed to `__post__` postprocessor; blocks index_docs end-to-end.

### B: Async skill interruption on stdin close (S4, S7, S9)
Background skills (skill_builder, eval, index_docs) get CancelledError when chat session ends. Expected behavior; dogfood recipe should pipe /quit or wait for task_completed before exiting.

### C: Directory listing routing miss (S3, S9 T1)
For directory queries, LLM uses `list_actions(category=["file"])` → 0 results → inline. Should route to `skill__read_local_files`. Description gap.

### D: word_stats python preprocessor zeros (S5, S6)
All stats zero in artifact. LLM infers correct values. Likely a safe-mode restriction on the stat computation module.

### E: task_completed narration bug for word_stats_demo (S6)
skill_run_completed:finished but agent says "not a valid action". Router state machine confusion on the second routing_decided (bare name `word_stats_demo` vs qualified `skill__word_stats_demo`).

### F: Wipe recipe gap
Agent state not wiped between scenarios. `.reyn/agents/dogfood-b32-2/state/` and `history.jsonl` must also be deleted for clean isolation. Brief recipe only covers events/ and global state.
