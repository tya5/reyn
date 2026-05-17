# B38 Worker 2 Findings — stdlib_skills_core.yaml

**Batch**: B38
**Worker**: W2 (stdlib_skills_core.yaml, 9 scenarios)
**HEAD**: 1d5042d
**Port**: 8082 (existing reyn web process, PID 14204)
**Date**: 2026-05-17

---

## Aggregate Score

**V/I/R/B = 2/2/5/0** (9 scenarios)

DeltavsB37 W2 (2/5/2/0): 0DeltaV, -3DeltaI, +3DeltaR.

| Scenario | Verdict | Key observation |
|---|---|---|
| index_docs_basic | REFUTED | LLM called operation__create_index directly (unknown tool); tool_failed; no skill_run_spawned |
| read_local_files_explain_source | INCONCLUSIVE | Correct 1971-char reply via file__read direct; no skill_run_spawned |
| read_local_files_multi_file | REFUTED | Reply "couldn't find any files" factually wrong; file__list ran but LLM reported failure |
| skill_builder_web_summariser | REFUTED | LLM hallucinated skill__create_web_article_summarizer; tool_failed |
| word_stats_demo_sentence | VERIFIED | invoke_action→word_stats_demo→spawned→completed; 44 chars/1 line |
| word_stats_demo_multiline | VERIFIED | invoke_action→word_stats_demo→spawned→completed; 88 chars/4 lines |
| eval_run_direct_llm | REFUTED | LLM dispatched judge_phase (not eval); skill_run_failed (turn budget exhausted) |
| chat_compactor_long_session | INCONCLUSIVE | 5 turns coherent (1766-2903 chars); inline path; no compaction triggered |
| chained_find_then_index | REFUTED | T1 OK (file list); T2 wrong action (rag.operation__remember_shared instead of index_docs) |

Regression vs B37: S3 INC→REF (file__list result ignored), S7 VER→REF (judge_phase dispatched, turn budget), S9 INC→REF (wrong action). B38 ARS code fix confirmed correct but did not improve these scenarios.

---

## V-Angle 1: B37 W2 S1 Wrapper-Path Retest — ARS Scope Expansion

**ars_scope_expanded: YES** (code-verified)

Direct invocation of _collect_all_session_ars_entries() (static-only mode) confirms:

```
exec__sandboxed_exec: {allow_subprocess, argv, env_passthrough, network, read_paths, timeout_seconds, write_paths}
file__delete: {path}
file__glob: {path, pattern}
file__grep: {case_sensitive, glob, max_results, path, pattern}
file__list: {path}
file__read: {path}
file__write: {content, path}
mcp.operation__drop_server: {clear_secrets, scope, server}
memory.operation__forget: {layer, slug}
memory.operation__remember_agent: {body, description, name, slug, type}
memory.operation__remember_shared: {body, description, name, slug, type}
rag.operation__drop_source: {source}       <- canonical key (B37 W2 S1 used source_id)
rag.operation__recall: {embedding_model, filters, query, sources, top_k}
reyn.source__list: {path}
reyn.source__read: {path}
web__fetch: {max_length, url}
web__search: {max_results, query}
```

rag.operation__drop_source is now in ARS with canonical key `source`. ARS block header: "all session-visible actions" (was "current hot-list actions").

### B38 S1 Actual Execution — wrapper_arg_canonical_s1

S1 events:
```
[tool_failed]      tool=operation__create_index, error_kind=unknown_tool
[routing_decided]  action_name=operation__create_index, outcome=error
```

LLM did NOT call rag.operation__drop_source at all. It attempted operation__create_index directly (not via invoke_action). wrapper_arg_canonical_s1: N/A — invoke_action was not used for the indexing action in B38 S1.

The ARS scope expansion is confirmed at code level. Behavioral confirmation that canonical `source` (not `source_id`) is used requires a session where the LLM actually routes through invoke_action(rag.operation__drop_source). In B38 S1, the LLM chose a different non-existent direct tool.

---

## V-Angle 2: Ghost Alias `skill__create_skill` Rejection

**ghost_rejected: PARTIAL** (structural check passes; rank-displacement prevents appearance)

_is_valid_qualified_name("skill__create_skill") returns True (structural-only: valid category `skill`, valid separator `__`, non-empty entry name `create_skill`). Ghost aliases pointing to non-existent skills are NOT rejected.

Aliases actually rejected at load (stderr warnings):
- list_actions — not in current action registry
- describe_action — not in current action registry  
- search_actions — not in current action registry
- operation__create_index — not in current action registry

skill__create_skill is NOT warned because structural parse succeeds.

### Why ghost did not appear in B38 S4

skill__create_skill is at rank 23 in freq+recency scoring. With hot_list_n=20 default, it is outside the top-20 window. It does not appear as a direct alias tool.

B38 S4 LLM invoked:
```
invoke_action({action_name: "skill__create_web_article_summarizer",
               args: {description: "Takes a web article URL...", name: "web_article_summariser"}})
→ tool_failed: ValueError: skill 'create_web_article_summarizer' not found
```

skill__create_skill was not in the tool list (rank 23). B37 F3 ghost invocation did not recur in B38.

Residual risk: if usage patterns shift such that fewer than 23 actions rank above skill__create_skill, it re-enters the top-20.

---

## V-Angle 3: Hot-List Coverage Gap Baseline (B38)

ARS scope expanded to cover all 17 static operations. Skill aliases with empty input_schema still absent from ARS (no properties to embed).

Top-20 hot list (B38 actual):
1. reyn.source__read      6. skill__word_stats_demo   11. skill__lint_index_events
2. web__search            7. exec__sandboxed_exec     12. skill__skill_builder
3. file__read             8. skill__judge_phase       13. rag.operation__drop_source
4. file__list             9. web__fetch               14. rag.operation__remember_shared
5. agent.peer__researcher 10. agent.peer__dogfood-...  15-20: skill__index_events, file__write, skill__create_web_article_summarizer, rag.operation__recall, skill__haiku, exec__run_python_code

Skill gap status (B38 vs B37 W2 carry-over):
- skill__index_docs:      ABSENT from hot-list (in seed, all 20 slots full); ABSENT from ARS (empty input_schema)
- skill__read_local_files: ABSENT from hot-list (in seed, all 20 slots full); ABSENT from ARS (empty input_schema)
- skill__direct_llm:      ABSENT from hot-list; ABSENT from ARS
- skill__eval:            ABSENT from hot-list (in seed, all 20 slots full); ABSENT from ARS (empty input_schema)

Hot-list coverage gap: UNCHANGED vs B37 W2.

---

## V-Angle 4: C1 / Q2 Stability

C1 (skill routing): S5/S6 correct (word_stats_demo, 2/9). S7 dispatched wrong skill. Others failed before dispatch.

Q2 (multi-turn coherence): STABLE. S8 5-turn: T1=1971, T2=1766, T3=2306, T4=2903, T5=2260 chars. All coherent, context-continuous.

---

## Additional Findings

F1: ARS scope expansion confirmed — rag.operation__drop_source:{source} in ARS (code-level positive verification).

F2: S1 LLM chose operation__create_index (not rag.operation__drop_source). ARS fix prevents arg-name hallucination when invoke_action is used; does not prevent wrong direct tool name selection.

F3: Ghost alias rank-displaced (rank 23) but not structurally rejected. B37 F3 does not manifest in B38. Underlying cause remains.

F4: S3 regression — file__list returned results but LLM reply said "couldn't find any files". Behavioral regression vs B37 W2 S3.

F5: S7 regression — judge_phase dispatched instead of direct_llm; skill_run_failed (turn budget exhausted). Unrelated to ARS.

F6: S9 T2 wrong routing — rag.operation__remember_shared (rank 14, memory) invoked instead of index_docs. Semantic routing failure.
