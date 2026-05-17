# B39 Worker 2 Findings — stdlib_skills_core.yaml

**Batch**: B39
**Worker**: W2 (stdlib_skills_core.yaml, 9 scenarios)
**HEAD**: b4daeb1
**Port**: 8082 (existing reyn web process)
**Date**: 2026-05-17
**Agents**: dogfood-b39-2-s1 through dogfood-b39-2-s9

---

## Aggregate Score

**V/I/R/B = 1/3/5/0** (9 scenarios)

DeltavsB38 W2 (2/2/5/0): -1V, +1I, 0R, 0B.

| Scenario | Verdict | Key observation |
|---|---|---|
| index_docs_basic | REFUTED | invoke_action(rag.operation__create_corpus) unknown action; no skill_run_spawned |
| read_local_files_explain_source | INCONCLUSIVE | Correct reply via file__read direct; no skill_run_spawned, no read_local_files artifact |
| read_local_files_multi_file | REFUTED | Two list_actions calls, no file ops; reply "ファイルが見つかりませんでした" |
| skill_builder_web_summariser | REFUTED | skill__skill_builder dispatched; skill_run_failed (phase_no_progress/workflow_aborted) |
| word_stats_demo_sentence | VERIFIED | invoke_action(skill__word_stats_demo, {text:...}); skill_run_completed(finished); 44 chars |
| word_stats_demo_multiline | INCONCLUSIVE | Direct wrapper (empty args) dispatched twice; stats inconsistent (0 chars + 159 chars) |
| eval_run_direct_llm | REFUTED | LLM dispatched skill__direct_llm not skill__eval; wrong routing |
| chat_compactor_long_session | INCONCLUSIVE | T1 reyn.source__read; T2-T5 inline; no compaction; T5 coherent routing answer |
| chained_find_then_index | REFUTED | T1 file__list OK; T2 rag.operation__remember_or_update_corpus (wrong action) |

---

## Stdlib Skill ARS Verify (V-Angle 1)

**four_skills_ars_visible: YES** — 14/14 structural tests pass (pytest test_b39_skill_alias_schema_visible.py).

### Schemas (verified from _collect_all_session_ars_entries)

```
skill__direct_llm:       {text: string}
skill__read_local_files: {text: string}
skill__index_docs:       {source, path, description, mode}
skill__eval:             {case_name, case_input, spec_path, target_skill_path, skill_root, phase_criteria}
```

B39 fix: `user_message.yaml` added to `direct_llm/artifacts/` and `read_local_files/artifacts/`. `_extract_skill_input_hint` now resolves the schema from skill-local artifacts/ dir. Both skills go from absent to present in ARS (B38→B39 delta).

---

## Hot-List Coverage Gap Status (V-Angle 2)

**hot_list_coverage_gap_closed: partial**

Structural: CLOSED. ARS now has 11 skill entries (B38 had 9). skill__direct_llm and skill__read_local_files now appear in invoke_action's ARS description via `_enrich_invoke_action_description`.

Behavioral: S7 event trace:
```
tool_called: invoke_action(action_name=skill__direct_llm, args={...})  [no prior list_actions]
routing_decided: skill__direct_llm  outcome=success
```
LLM chose invoke_action(skill__direct_llm) directly — confirms ARS embedding surfaces the skill in LLM context without a list_actions round-trip. This is positive evidence that the structural fix translates to behavioral accessibility.

Residual: action-selection layer. LLM chose skill__direct_llm instead of skill__eval in S7. That is a semantic routing error at the selection layer, not an ARS visibility error. B38 retro §3 hypothesis (action-selection prior) confirmed.

Note: skill__direct_llm is NOT in DEFAULT_HOT_LIST_SEED. With hot_list_n=10 and fresh state, it does not appear as a direct wrapper tool. It reaches the LLM only through invoke_action's ARS description.

---

## B37 W2 S1 Wrong-Path Retest (V-Angle 3)

- B37: invoke_action(rag.operation__drop_source, {source_id:...}) — wrong arg name
- B38: direct tool call operation__create_index — unknown tool
- B39: invoke_action(rag.operation__create_corpus, {name, path, description}) — unknown action

Pattern: LLM consistently attempts a non-existent RAG create/corpus action. The B39 ARS fix does not address this (the missing action is not one of the 4 target skills).

---

## C1 / Q2 Stability (V-Angle 4)

C1: S5 correct only (word_stats_demo via invoke_action with text arg). S6 double-dispatch with empty args. S7 wrong skill. Others failed.

Q2 (multi-turn): STABLE. S8 five turns all coherent (2253/2102/2721/2440/2000+ chars). T5 final reply correctly explains skill routing, universal action catalog, and routing_decided event semantics.

---

## Additional Findings

F1: B39 primary verify confirmed — 4 skills in ARS with correct schemas. 14/14 structural tests pass.

F2: S7 confirms action-selection layer is the remaining gap. ARS visibility is resolved; skill selection is not.

F3: S1 three-batch pattern (B37/B38/B39): different non-existent RAG "create" actions each batch. Root cause: no rag.operation__create_index or similar action in catalog; LLM infers it should exist.

F4: S6 double-dispatch with empty args: skill__word_stats_demo called twice via direct wrapper (not invoke_action) with `{}` args. Both completed but stats for empty-input run showed 0 chars/0 lines vs correct 159 chars/25 words.

F5: S4 skill_builder correctly dispatched but aborted (build_skill → verify_skill → phase_no_progress). Same failure as B38.

F6: S3 routing failure: list_actions called twice with incompatible filters, no file read/list executed. Same pattern as B38.
