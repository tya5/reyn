# B28 Worker 2 Findings — stdlib_skills_core

**Batch**: 28  
**Worker**: 2/7  
**HEAD**: f5a6866 (post-Wave-1 fix wave)  
**Scenario set**: `dogfood/scenarios/stdlib_skills_core.yaml` (9 scenarios)  
**B27 baseline**: V/I/R/B = 0/2/6/1  

---

## Verdict Matrix

| # | Scenario ID | B27 | B28 | Delta | Root Cause |
|---|------------|-----|-----|-------|------------|
| 1 | index_docs_basic | R | R | = | LLM invokes nonexistent rag.operation__add_source / rag.operation__create_index instead of skill__index_docs |
| 2 | read_local_files_explain_source | R | R | = | Router uses direct file__read hot-alias (no skill dispatch → no skill_run_spawned) |
| 3 | read_local_files_multi_file | R | R | = | LLM calls list_actions(filter=...) misunderstanding tool purpose; no routing_decided |
| 4 | skill_builder_web_summariser | I | V | +1 | Fully functional: routing_decided + skill_run_spawned + skill_run_completed all present |
| 5 | word_stats_demo_sentence | B | R | +1(-1) | Skill spawned but preprocessor_step_failed: python.pure:allow in reyn.yaml != python.safe mode |
| 6 | word_stats_demo_multiline | R | R | = | Same as S5: python.safe permission denied |
| 7 | eval_run_direct_llm | R | R | = | LLM invokes skill_improver (wrong skill) with JSON-string args |
| 8 | chat_compactor_long_session | R | R | = | Compaction threshold not reached; llm_called not emitted for router calls |
| 9 | chained_find_then_index | I | R | -1 | LLM invokes nonexistent rag.operation__add_document; same attractor as S1 |

**Aggregate B28**: V=1 / I=0 / R=8 / B=0  

---

## B28 Verification Angles

### C1 Verify — No duplicate function declarations
No C1 regressions. All 9 traces: tools array=14 entries, zero duplicates. Primary data: c1_regression=false for all scenarios.

### routing_decided emit
7/9 scenarios emitted routing_decided.
- S3: no routing_decided. LLM called list_actions with filter param, no invoke_action dispatched.
- S8: no routing_decided. All 5 prompts answered by direct LLM (finish_reason=stop, zero tool calls).

### B27-H4 Partial Verify
S5, S6 emitted skill_run_interrupted. Root cause is preprocessor_step_failed (permission denial), NOT session shutdown. This is a distinct failure mode from H4. S4 correctly ended with skill_run_completed.

---

## Per-Scenario Root Causes

### S1 — index_docs_basic (R)
LLM Turn 1: invoke_action(rag.operation__create_index) — nonexistent.
Tool error: "Unknown action 'rag.operation__create_index': no routing rule."
Probe confirmed available RAG actions: rag.operation__drop_source, rag.operation__recall, skill__index_docs.
Attractor: LLM uses hallucinated RAG CRUD names from training data rather than catalog-discovered skill__index_docs.

### S2 — read_local_files_explain_source (R)
Router used hot-alias file__read directly (describe_action → invoke_action file__read).
File content read correctly; reply correct. BUT: no skill_run_spawned, no phase_started.
Functional success but structural path bypassed. Scenario events requirements unmet.

### S3 — read_local_files_multi_file (R)
LLM called list_actions(category=["file"], filter="src/reyn/op_runtime/").
filter param filtered action names, not file paths → returned empty result.
LLM did not use file__list (available in hot-list). No routing_decided emitted.

### S4 — skill_builder_web_summariser (V)
Full chain: routing_decided → skill_run_spawned → phase_started(x5) → skill_run_completed.
Artifacts written to reyn/local/web_article_summariser/. Reply confirmed.
B27 was inconclusive; B28 verified = genuine fix wave improvement.

### S5 — word_stats_demo_sentence (R)
skill_run_spawned: YES. phase_started: YES. preprocessor_step_failed: YES.
Error: "safe python step ./stats.py:compute_text_stats denied by user"
Root: reyn.yaml has python.pure: allow. skill.md declares mode: safe. permissions.py checks
python.safe key not python.pure → no blanket grant → denial in non-interactive mode.
skill_run_interrupted emitted (H4 check: this is permission-denial path, not session shutdown).

### S6 — word_stats_demo_multiline (R)
Same python.safe denial. Three invoke_action attempts (invoke_action + direct hot-alias x2).
All three: preprocessor_step_failed. Identical error.

### S7 — eval_run_direct_llm (R)
LLM invoked skill__skill_improver (wrong skill) with args as JSON-encoded string.
Error: "args validation failed: '...' is not of type 'object'"
Two compounded errors: wrong skill target + args serialization format.
tool_failed event. No skill_run_spawned.

### S8 — chat_compactor_long_session (R)
5 LLM calls, all finish_reason=stop, zero tool calls. Max input tokens ~8k (well below 30k threshold).
Events: chat_started, chat_stopped, compaction_check, user_message_received only.
llm_called event not emitted: router LLM calls don't emit llm_called (only phase-engine calls do).
Reply quality: good (verified on rubric). Structural events unmet.

### S9 — chained_find_then_index (R)
Turn 1 (list files): success via file__list.
Turn 2 (index): invoke_action(rag.operation__add_document) → Unknown action.
Same RAG attractor as S1. B27 was inconclusive; clean isolation in B28 revealed the attractor on second prompt.

---

## Cross-Scenario Patterns (primary data, 2Q-checked)

**Pattern A — RAG/index attractor (S1, S9)**: 2/2 indexing scenarios triggered nonexistent rag.operation__* names. Catalog probe confirmed skill__index_docs is available; attractor prevents its discovery.

**Pattern B — read_local_files skill bypass (S2, S3)**: File reading goes through hot-alias file__read (S2) or list_actions misuse (S3). skill__read_local_files not invoked in either case.

**Pattern C — python.safe permission config mismatch (S5, S6)**: reyn.yaml python.pure vs runtime python.safe mismatch. Consistent across both word_stats_demo scenarios.

**Pattern D — eval skill routing failure (S7)**: Single observation. LLM chose skill_improver over eval. Insufficient data for attractor classification.

**Pattern E — chat_compactor structural gap (S8)**: Token threshold not reachable in short sessions. Also: llm_called not emitted for router-tier LLM calls (only phase-engine). Scenario design issue.

---

## Actionable Findings

| Priority | Finding | Scenarios |
|----------|---------|-----------|
| HIGH | reyn.yaml grants python.pure:allow but word_stats_demo requires python.safe:allow. Add python.safe:allow to reyn.yaml or change skill mode to pure. | S5, S6 |
| HIGH | RAG indexing LLM attractor: LLM invokes nonexistent rag.operation__add_source/create_index/add_document. Seed rag.operation category pointing to skill__index_docs, or add hot-list entry. | S1, S9 |
| MED | read_local_files skill bypassed by file__read hot-alias. Scenario expectations or routing need alignment. | S2 |
| MED | list_actions filter param semantics: LLM treats filter as directory filter. S3 scenario should use file__list directly. | S3 |
| MED | eval skill not routed (skill_improver invoked instead). | S7 |
| LOW | chat_compactor scenario design: 30k token threshold not reachable in 5-turn sessions. | S8 |
| LOW | llm_called not emitted for router-tier LLM calls. Scenario expectations for llm_called in S5/S8 rely on phase-engine calls only. | S5, S8 |
