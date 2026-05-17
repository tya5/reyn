# B33 Worker 2 Findings — stdlib_skills_core (9 scenarios)

**Date**: 2026-05-17  
**HEAD**: 08ccc27  
**Agent**: dogfood-b33-2  
**Worktree**: /tmp/reyn-worktrees/b33-2  
**Run ID**: b33-worker2-v2

---

## Aggregate

| Band | Count | Rate |
|------|-------|------|
| Verified | 0 | 0.0% |
| Inconclusive | 0 | 0.0% |
| Refuted | 9 | 100% |
| Blocked | 0 | 0.0% |
| Total | 9 | |
| Brier | 0.1076 | |

## H3 Race Fix Verification

CONFIRMED (4/4 applicable scenarios). invoke_skill_spawn_ack_exit observed in:
- S4 skill_builder_web_summariser: YES
- S5 word_stats_demo_sentence: YES  
- S6 word_stats_demo_multiline: YES
- S7 eval_run_direct_llm: YES

Event sequence confirmed: skill_run_spawned -> routing_decided -> invoke_skill_spawn_ack_exit -> skill_run_completed -> skill_completion_injected -> chat_turn_completed_inline

B28 skill_run_interrupted pattern: ABSENT. Wipe confirmed: session_restored=0.

## B30-NEW-1/2 Check

skill__index_docs and skill__eval NOT visible in cold-start tools array (15 unique tools seen). B30-NEW-1/2 seeds not confirmed for this worker.

## Per-Scenario Results

S1 index_docs_basic: REFUTED (predicted refuted) Brier=0.000. LLM tried rag.operation__create_index (nonexistent). No invoke_skill. Router used list_actions fallback.

S2 read_local_files_explain_source: REFUTED (predicted refuted) Brier=0.094. Reply VERIFIED (judge 0.8, correctly explained scheduler.py). Events refuted: used file__read via invoke_action, not invoke_skill. skill_run_spawned count=0. Artifact absent.

S3 read_local_files_multi_file: REFUTED (predicted refuted) Brier=0.000. Same direct-action pattern. Judge score below threshold.

S4 skill_builder_web_summariser: REFUTED (predicted inconclusive) Brier=0.375. H3 fires. skill_run_failed (phase loop: build_skill output identical to rejected rollback). Reply empty — send_to_agent_impl returns before skill reply captured post-spawn-ack.

S5 word_stats_demo_sentence: REFUTED (predicted refuted) Brier=0.219. H3 fires. Skill completes (status=finished). Two failures: (a) reply empty (send_to_agent_impl capture gap), (b) skill_run_completed.status=finished vs asserted success — schema mismatch.

S6 word_stats_demo_multiline: REFUTED (predicted refuted) Brier=0.125. H3 fires. Reply non-empty (skill result injected). Judge 0.3 — reply estimates token count instead of citing precomputed stats. skill_run_completed.status=finished vs success mismatch.

S7 eval_run_direct_llm: REFUTED (predicted refuted) Brier=0.031. H3 fires. Router dispatched skill__direct_llm not skill__eval (eval not in hot-list). Reply empty (capture gap). 

S8 chat_compactor_long_session: REFUTED (predicted refuted) Brier=0.125. 5 inline turns, no skill dispatch. Final reply VERIFIED (judge confirmed routing answer). Events refuted: skill_run_spawned=0 (expected), llm_called=0 (event type never emitted in router path).

S9 chained_find_then_index: REFUTED (predicted refuted) Brier=0.000. Turn 1: file__list found no docs. Turn 2: tried rag.operation__create_index (nonexistent). Same S1 root cause.

## Cross-Scenario Patterns

Pattern A (Direct action vs skill invocation): S1, S2, S3, S9 use invoke_action path. Event assertions expecting skill_run_spawned are wrong for universal catalog behavior.

Pattern B (status mismatch): skill_run_completed.status=finished in actual events, but scenarios assert status=success. Persistent mismatch across S1/S5/S6. Pre-existing bug, B32 predictions already account for it.

Pattern C (reply capture gap post-H3): S4, S5, S7 have empty reply_text. After invoke_skill_spawn_ack_exit, send_to_agent_impl returns before skill completion reply. S6 non-empty (inconsistent — likely timing difference in whether skill_completion_injected round-trip completes before return).

Pattern D (llm_called event absent): 0 occurrences across all 9 scenarios. LLM calls DO happen (evidenced by replies) but llm_called event not emitted in router path. Scenarios S5/S8 assertions for this event are unverifiable.
