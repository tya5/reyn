# Dogfood Batch 27 — Worker 2 Findings
## Set: stdlib_skills_core (9 scenarios)
**Date**: 2026-05-17  
**Branch**: main HEAD `5965b58` (worktree `/tmp/reyn-worktrees/b27-2`)  
**LLM**: gemini-2.5-flash-lite via LiteLLM proxy

---

## Verdict Matrix

| # | Scenario | Verdict | reply_pass | events_pass | artifacts_pass |
|---|----------|---------|------------|-------------|----------------|
| 1 | index_docs_basic | **refuted** | FAIL | FAIL | FAIL |
| 2 | read_local_files_explain_source | **inconclusive** | FAIL | FAIL | PASS |
| 3 | read_local_files_multi_file | **refuted** | FAIL | FAIL | FAIL |
| 4 | skill_builder_web_summariser | **inconclusive** | FAIL | FAIL | PASS |
| 5 | word_stats_demo_sentence | **blocked** | FAIL | FAIL | FAIL |
| 6 | word_stats_demo_multiline | **refuted** | FAIL | FAIL | FAIL |
| 7 | eval_run_direct_llm | **refuted** | FAIL | FAIL | FAIL |
| 8 | chat_compactor_long_session | **refuted** | PASS | FAIL | FAIL |
| 9 | chained_find_then_index | **refuted** | FAIL | FAIL | FAIL |

**Summary: V=0 / I=2 / R=6 / B=1**

---

## Setup Note: Isolation Protocol

A systemic issue was discovered during setup: `.reyn/state/action_usage.jsonl` persists across agent sessions and re-injects previously-seen action names into the router's tool list. After scenario 1 tried `rag.operation__create_index`, that action name was recorded and re-injected in subsequent sessions, causing Gemini to return HTTP 400 "Duplicate function declaration found: describe_action" (`describe_action` was both a base tool AND re-injected via action_usage lookup).

**Mitigation applied**: `rm -rf .reyn/state/action_usage.jsonl` between each scenario + new agent per scenario.

---

## Per-Scenario Findings

### S1: index_docs_basic — refuted

**Primary data**:
- routing_decided: `{"action_name": "operation__create_index", "source": "hot_list_alias", "outcome": "error"}` (x2)
- Events emitted: `chat_started, user_message_received, tool_failed x2, routing_decided x2, compaction_check, chat_stopped`
- `skill_run_spawned`: NOT emitted
- Reply: "The tool `operation__create_index` is not available."

**Finding**: Router resolves "concepts_index をセマンティック インデックスに登録" to `operation__create_index` via hot_list_alias. This action does not exist. `index_docs` skill is unreachable from this phrasing.

---

### S2: read_local_files_explain_source — inconclusive

**Primary data**:
- routing_decided x3: `{"action_name": "skill__read_local_files", "source": "invoke_action", "outcome": "success"}`
- Events: `skill_run_spawned` x3, `skill_run_interrupted` x3, `skill_run_completed` NEVER emitted
- Artifact present: `.reyn/agents/b27-s2/state/skills/*_read_local_files.snapshot.json`
- RuntimeWarning: `coroutine 'OpenAIChatCompletion.acompletion' was never awaited`
- Reply (thin): "cron スケジューラに関連するコードを管理しているようです"

**Rubric**: clause 1 PASS (cron purpose), clause 2 FAIL (no CronJob/CronScheduler details), clause 3 PASS
**Events**: FAIL (skill_run_completed not emitted)

**Finding 1**: `read_local_files` dispatched 3x in parallel (router called `invoke_action` 3x before getting responses — race condition).  
**Finding 2**: All 3 runs interrupted before completion. Async coroutine warning suggests lifecycle management bug.

---

### S3: read_local_files_multi_file — refuted

**Primary data**:
- LLM trace: `list_actions(category=["file"], filter="src/reyn/op_runtime/")` then `list_actions(filter="src/reyn/op_runtime/")` then stop
- `routing_decided`: NOT emitted
- `skill_run_spawned`: NOT emitted
- Reply: "I couldn't find any files in the `src/reyn/op_runtime/` directory."

**Finding**: Router misused `list_actions` as a filesystem directory browser. `list_actions` returns catalog actions, not files. Router never escalated to `skill__read_local_files`.

---

### S4: skill_builder_web_summariser — inconclusive

**Primary data**:
- routing_decided: `{"action_name": "skill__skill_builder", "source": "invoke_action", "outcome": "success"}`
- Events: `skill_run_spawned` x1, `skill_run_interrupted`, `skill_run_completed` NEVER emitted
- Artifact: `.reyn/agents/b27-s4/state/skills/*_skill_builder.snapshot.json` (present)
- Trace: `[skill_builder#ff55] phase started: plan_skill` emitted before interrupt
- Reply: "バックグラウンドでスキル作成を開始しました" (no completion, no skill name mentioned)

**Rubric**: clause 1 FAIL (only "started," no "generated/lint passed"), clause 2 FAIL (web_article_summariser not mentioned), clause 3 PASS
**Events**: FAIL (skill_run_completed not emitted)

**Finding**: Same async interrupt pattern as S2. Dispatch is correct; completion is unreachable.

---

### S5: word_stats_demo_sentence — blocked

**Primary data**:
- Events: `skill_run_spawned, preprocessor_step_started, preprocessor_step_failed, skill_run_interrupted, skill_run_failed`
- Error: "safe python step ./stats.py:compute_text_stats denied by user"
- `reyn.yaml`: `permissions: python.pure: allow` (no `python.safe`)
- Skill definition (`src/reyn/stdlib/skills/word_stats_demo/skill.md`): `mode: safe`

**Finding**: `word_stats_demo` requires `python.safe` mode; `reyn.yaml` only grants `python.pure`. Configuration gap in dogfood environment — not a skill/OS bug.

---

### S6: word_stats_demo_multiline — refuted

**Primary data**:
- Events: 5x `user_message_received` (multi-line heredoc split per line)
- `skill_run_spawned`: NOT emitted
- Reply: "the `skill__word_stats_demo` tool does not accept text as an argument"

**Finding 1**: Multi-line heredoc piped via stdin was split into 5 separate user turns (newline-delimited).  
**Finding 2**: LLM incorrectly claimed the skill does not accept text; skill accepts `user_message`. Tool description may be misleading.  
**Note (5Q)**: Single run observed; pattern ("always splits") not asserted.

---

### S7: eval_run_direct_llm — refuted

**Primary data**:
- routing_decided: `{"action_name": "skill__skill_improver", ...}` — wrong skill dispatched
- Events: `skill_run_spawned` (skill_improver), `skill_run_failed`
- Error: "Skill 'skill_improver' declares an unsafe python step ... --allow-unsafe-python was not provided"
- `eval` artifact: absent

**Rubric**: All 3 clauses FAIL (no eval result, no criterion reference, no score)

**Finding 1**: Router dispatched `skill_improver` instead of `eval`. "eval してください" likely mapped semantically to improvement/improver rather than evaluation skill.  
**Finding 2**: `eval` skill is not registered in router's action catalog.

---

### S8: chat_compactor_long_session — refuted

**Primary data (5 turns)**:
- All 5 turns: only `chat_started, user_message_received, compaction_check, chat_stopped`
- `skill_run_spawned`: NEVER emitted across all 5 turns
- `llm_called`: NEVER emitted (only appears inside skill phase event logs, not router layer)
- Tokens: T1~4022 + T2~4621 + T3~5857 + T4~6785 + T5~7736 ≈ 29,021 total (below 30k threshold)
- `direct_llm` artifact: absent
- Final reply (turn 5): coherently explains routing_decided, universal action catalog, skill routing

**Rubric (final turn)**: clauses 1/2/3 all PASS
**Events**: FAIL (skill_run_spawned and llm_called must_emit not satisfied)

**Finding 1**: LLM answered all 5 architecture questions from context without skill dispatch.  
**Finding 2**: Compaction did not trigger (29k < 30k threshold).  
**Finding 3**: `llm_called` only records inside skill-phase event files — structurally impossible to satisfy from chat-layer alone without skill dispatch.

---

### S9: chained_find_then_index — refuted

**Primary data**:
- Turn 1 trace: `list_actions(category=["file"], filter="docs/concepts/.*\\.md$")` → empty (same misuse as S3)
- Turn 2 routing_decided x2: `{"action_name": "rag.operation__add_source", "source": "invoke_action", "outcome": "success"}`
- `skill_run_spawned`: NOT emitted
- `index_docs` artifact: absent
- Turn 2 reply: "`rag.operation__add_source` というアクションは利用できないようです"

**Rubric**: clauses 1/2/3 all FAIL (indexing not confirmed, "arch_events_index" not in reply, error reported)

**Finding**: Same root cause as S1 — "セマンティック インデックスに登録" maps to non-existent `rag.operation__*` actions. Turn 1 also repeats S3 router misuse pattern.

---

## Cross-Scenario Patterns (hypotheses with primary-data support)

**H1 — index_docs not in catalog**: S1 and S9 (2/2 indexing scenarios). Both resolved to non-existent `rag.operation__*`. `index_docs` skill is not reachable via Japanese indexing phrasing. [2 primary observations]

**H2 — skill_run_completed never fires**: S2 and S4 (2/2 successfully dispatched skills). All end with `skill_run_interrupted`. Async lifecycle management issue; coroutine warning in S2 supports this. [2 primary observations]

**H3 — list_actions misused as file finder**: S3 and S9 turn 1 (2/2 directory-listing queries). [2 primary observations]

**H4 — eval not in catalog**: S7 routed to `skill_improver`; no `eval` action in routing_decided. [1 primary observation]

---

## Systemic Bug: action_usage.jsonl Cross-Session Pollution

**Evidence**: Third and fourth LLM calls in trace show `tools (13): ..., describe_action, ..., describe_action, ...` (two occurrences vs. clean first call with no duplicate). File `.reyn/state/action_usage.jsonl` accumulated `rag.operation__create_index` and `describe_action` from S1, causing Gemini HTTP 400 on all subsequent sessions.

**Impact**: Without manual cleanup, all scenarios after the first are blocked by this bug.

---

## Calibration Check

All 9 scenarios underperformed their "verified" top-band predictions. Dominant failure modes not anticipated in predictions:
1. Skills not registered in catalog (index_docs, eval)
2. skill_run_completed replaced by skill_run_interrupted (async lifecycle)
3. python.safe permission gap in reyn.yaml
4. action_usage.jsonl cross-scenario contamination

---

## Blockers for Fix Wave

1. **[HIGH] index_docs unreachable via chat**: `index_docs` skill missing from router's action catalog; router resolves to non-existent `rag.operation__*`
2. **[HIGH] skill_run_completed never emitted**: All dispatched skills end `skill_run_interrupted`; async lifecycle bug
3. **[HIGH] eval routes to skill_improver**: `eval` skill not in catalog; router picks `skill_improver`
4. **[MED] action_usage.jsonl cross-session pollution**: Requires explicit cleanup between runs; should be scoped per-agent or reset on agent creation
5. **[MED] python.safe not in default reyn.yaml**: `word_stats_demo` requires `python.safe`; only `python.pure` granted
6. **[MED] list_actions misused as file finder**: Router uses action catalog filter for directory listing; tool description ambiguous
7. **[LOW] multi-line stdin split**: Heredoc multi-line piped via stdin is split into separate user turns
