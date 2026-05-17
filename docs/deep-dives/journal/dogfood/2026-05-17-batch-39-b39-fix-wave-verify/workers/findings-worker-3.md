# B39 Worker 3 Findings — control_ir_ops.yaml

**Batch**: B39 | **Worker**: W3 | **HEAD**: b4daeb1 | **Port**: 8083
**Agent prefix**: dogfood-b39-3-sN | **Date**: 2026-05-17 | **State mode**: fresh

## Aggregate Score

**V/I/R/B = 3/0/6/0** (9 scenarios) | **DeltavsB38 = +1V** (S8 R->V)

| Scenario | B37 | B38 | B39 | Key observation |
|---|---|---|---|---|
| file_read_via_chat (S1) | V | V | **V** | file__read hot_list_alias; routing_decided+tool_executed; reply mentions 8 principles |
| file_glob_grep (S2) | R | R | **R** | LLM uses file__grep now (improvement vs B38 file__list); grep returned 0 matches (accurate) |
| web_search_query (S3) | V | V | **V** | web_search_started+web_search_completed; OpenAI SDK mentioned |
| web_fetch_url (S4) | R | R | **R** | web__fetch selected (ARS-guided); permission_denied at runtime |
| sandboxed_exec_simple (S5) | R | R | **R** | sandboxed_exec_started+completed; returncode=-6 seatbelt blocked |
| lint_a_skill (S6) | R | R | **R** | skill__eval spawned; skill_run_failed postprocessor schema validation |
| recall_indexed_source (S7) | V | I | **R** | rag.operation__recall without sources arg -> KeyError sources |
| judge_output_direct (S8) | R | R | **V** | CRITICAL: skill_run_completed+workflow_finished; score=1.0; no validation_error |
| ask_user_round_trip (S9) | R | R | **R** | No skill_run_spawned, no user_intervention events; LLM replied inline |

