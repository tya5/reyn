# B38 Worker 3 Findings — control_ir_ops.yaml

**Batch**: B38
**Worker**: W3 (control_ir_ops.yaml, 9 scenarios)
**HEAD**: 1d5042d
**Port**: 8083
**Agent prefix**: dogfood-b38-3-sN
**LLM trace**: `.reyn/llm_trace_b38_w3.jsonl`
**Date**: 2026-05-17

## Aggregate Score

**V/I/R/B = 2/1/6/0** (9 scenarios)

| Scenario | B37 | B38 | Key observation |
|---|---|---|---|
| file_read_via_chat | V | **V** | file__read; routing_decided+tool_executed; mentions P1-P8 |
| file_glob_grep | R | **R** | LLM chose file__list (ARS has file__glob but preference unchanged) |
| web_search_query | V | **V** | web__search; web_search_started+web_search_completed; OpenAI SDK mentioned |
| web_fetch_url | R | **R** | ARS helped (correct action selected: web__fetch); but permission_denied+tool_failed |
| sandboxed_exec_simple | R | **R** | sandboxed_exec_started+completed emitted; returncode=-6 (pyenv sandbox blocked) |
| lint_a_skill | R | **R** | ghost alias skill__lint_index_events not found; lint_completed absent |
| recall_indexed_source | V | **I** | routing_decided=1, tried rag.operation__recall but KeyError:'sources' |
| judge_output_direct | R | **R** | skill_run_spawned; aborted (artifact_path literal passed, file not found) |
| ask_user_round_trip | R | **R** | inline reply both turns; no skill_run_spawned, no user_intervention_* events |

**DeltavsB37 = -1V +1I** (net regression of 1; S7 V->I)

---

## V-Angle 1: B37 W3 S8 judge_phase schema fix retest (PRIMARY)

**s8_judge_verdict_emitted: NO**

S8 still returned R. The B38 fix (29a0c31) corrects the postprocessor output_schema — switching from an inline dict literal to the phase_judgment artifact reference so that artifact_to_json_schema() wraps it correctly. In B37, S8 failed at the postprocessor validation step (validation_error event).

In B38, S8 failed at a different (earlier) point: the LLM aborted the workflow because artifact_path was the literal string "artifact_path" (placeholder value) — the judge phase LLM attempted to read the file but it did not exist.

**B37 failure mode**: validation_error -> postprocessor schema rejected the partial output (missing criteria_results, passed, score).
**B38 failure mode**: workflow_aborted -> LLM phase abort at judge phase ("artifact_path was not found after multiple attempts").

Key events from skill_runs/2026-05/2026-05-17T161128_judge_phase.jsonl:
- workflow_started + artifact_created -> skill spawned correctly
- 10x context_built/llm_called/... cycles (LLM retried file read 10 times, each permission_granted)
- phase_retry (1x)
- workflow_aborted: "Failed to read artifact: artifact_path was not found after multiple attempts."

The B38 postprocessor fix is structurally correct but untestable via the current S8 scenario design: judge_phase requires an artifact_path pointing to a real artifact file; the scenario asks the LLM to evaluate inline text. The fix IS verified by tests/test_judge_phase_schema.py (3 Tier 2 tests added in 29a0c31).

---

## V-Angle 2: D2-wrapper scope expansion verify

**ars_scope_expanded: YES (CONFIRMED)**

From llm-tools-schema trace (request a77f7590-464a-42bc-9638-f340698f9233, agent dogfood-b38-3-ars-check):

ARS block header: "ACTION ARG SCHEMAS (canonical keys for all session-visible actions)" (vs "current hot-list actions" in B37).

Confirmed entries in ARS block:
  file__glob: {path, pattern}
  file__grep: {case_sensitive, glob, max_results, path, pattern}
  file__write: {content, path}
  web__fetch: {max_length, url}
  rag.operation__drop_source: {source}
  rag.operation__recall: {embedding_model, filters, query, sources, top_k}
  exec__sandboxed_exec: {allow_subprocess, argv, env_passthrough, network, read_paths, timeout_seconds, write_paths}
  [+ all 17 static ops + session skills + MCP tools + peer agents]

All 17 static ops listed unconditionally without hot-list seeding.

---

## V-Angle 3: B37 W3 trajectory check

B37=4V baseline; B38=2V. DeltavsB37=-1V +1I.

S8: R->R (failure mode changed: validation_error -> workflow_aborted; no verdict emitted in either batch).
S4: R->R (ARS expansion caused correct action selection - LLM now picks web__fetch not web__search - but permission denied in environment).
S7: V->I (B37 LLM replied inline; B38 LLM tried rag.operation__recall -> KeyError:'sources').

---

## V-Angle 4: HOT_LIST_SEED expansion

file__write: {content, path} and rag.operation__drop_source: {source} confirmed present in ARS block (B38 seed additions). file__glob, file__grep, web__fetch also confirmed (pre-existing seed entries now unconditionally listed via scope expansion).

---

## Key Findings

**F1 (S8 judge fix scope)**: B38 postprocessor schema fix changes output_schema to artifact-reference form. B37 failure was validation_error in PostprocessorExecutor; B38 failure is workflow_aborted at the judge phase (earlier, different cause). The fix is correct but the S8 scenario cannot exercise it without a real artifact_path on disk.

**F2 (D2-wrapper scope expansion confirmed)**: ARS block now unconditionally lists all 17 static ops + session skills + MCP tools + peer agents. Header updated from "hot-list actions" to "all session-visible actions". No hot-list seeding required for file__glob, file__grep, web__fetch, file__write, rag.operation__drop_source arg-name guidance.

**F3 (ARS expansion partially helps action selection)**: S4 shows LLM now correctly selects web__fetch (not web__search as in B37). The ARS inclusion of web__fetch improved action selection. However, permission is denied in this environment, so the rubric is still not satisfied.

**F4 (S2 residual: LLM routing preference)**: Despite file__glob and file__grep in the ARS block, S2 still routed to file__list. ARS provides arg-name schema guidance but does not alter action selection preference. LLM chose the most familiar action (file__list) over the semantically correct ones (file__glob/file__grep). Unchanged from B37.

**F5 (S7 regression V->I)**: B37 LLM answered inline "cannot use recall" - rubric passed. B38 LLM attempted rag.operation__recall (tool now in ARS with sources field) but omitted sources arg -> KeyError:'sources'. The ARS expansion caused the LLM to attempt the tool but with incomplete args.

**F6 (S5 behavior improvement, still R)**: B37 S5 had sandboxed_exec_started=0 (exec not dispatched). B38 has sandboxed_exec_started=1 + sandboxed_exec_completed=1. ARS now includes exec__sandboxed_exec so LLM correctly invokes via invoke_action. Exec fails at runtime (pyenv libpython3.12 blocked by seatbelt sandbox), not at dispatch level.
