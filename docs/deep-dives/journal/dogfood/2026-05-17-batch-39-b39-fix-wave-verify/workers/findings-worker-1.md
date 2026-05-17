# B39 Worker 1 Findings — chat_router_smoke (7 scenarios)

**Date**: 2026-05-17  
**HEAD**: `b4daeb1`  
**Port**: 8081  
**Agent prefix**: `dogfood-b39-1-sN`  
**State mode**: fresh (dogfood_fresh_reset.sh before each scenario)  
**Model**: gemini-2.5-flash-lite (LiteLLM proxy at localhost:4000)

---

## Score: V=2 / I=0 / R=5 / B=0  (ΔvsB38: −2V)

| sN | Scenario ID | Verdict | Reply | Events | Artifact | Notes |
|----|-------------|---------|-------|--------|----------|-------|
| S1 | simple_capability_question | **V** | PASS | PASS | FAIL* | Inline reply, mentions skills. *Artifact not blocking for inline path. |
| S2 | factual_query_direct_llm | **V** | PASS | PASS | FAIL* | Correct idempotency explanation via web__search path. |
| S3 | skill_discovery_request | **R** | PASS | FAIL | FAIL | routing_decided not emitted (list_actions->inline). Persistent. |
| S4 | explicit_skill_invocation_word_stats | **R** | PASS | PASS | FAIL | REGRESSION: direct_llm chosen over word_stats_demo. |
| S5 | catalog_routing_decided_emitted | **R** | FAIL | PASS | FAIL | Clarification asked instead of poem. Persistent attractor. |
| S6 | multi_turn_pronoun_reference | **R** | FAIL | PASS | FAIL | REGRESSION: task_completed narration collapsed code to summary. |
| S7 | out_of_scope_graceful_decline | **R** | FAIL | PASS | FAIL | Image request not declined — persistent B38-OBS-2 attractor. |

---

## Verification Angle 1: Ghost Rejection (B39 #119 + follow-up)

**Result: CONFIRMED WORKING**

Injected correctly-formatted ghost entry: `{"qualified_name":"skill__nonexistent_xyz","ts":<ts>}` into `.reyn/state/action_usage.jsonl`.

Primary evidence (web.log, stderr captured via 2>&1):
```
[reyn] action_usage: skipping ghost alias 'skill__nonexistent_xyz' — not found in current registry
```

LLM inspection: requests `314f3ff2` and `3614f80d` via `dogfood_trace.py --mode llm-tools-schema` — no `skill__nonexistent_xyz` in ARS or tool list. Ghost filtered at hot-list materialization (`_filter_ghost_names_by_registry`, router_loop.py:1120).

Empty-schema skills NOT misclassified: `skill__direct_llm` and `skill__read_local_files` appear correctly in ARS (Angle 2). The `b1ca51a` follow-up fix is effective.

Note on injection format: `_load_from_disk` reads `qualified_name` + `ts` fields. Using legacy `action`/`score` fields was silently ignored (first injection attempt). Correct format required for ghost test to be valid.

---

## Verification Angle 2: Empty-Schema Skills ARS Visible (B39 #120)

**Result: CONFIRMED**

Checked 2 requests via `dogfood_trace.py --mode llm-tools-schema`:
- `3614f80d` (S1, T+0s)
- `314f3ff2` (ARS test, T+343s)

ARS excerpt from invoke_action description (primary data, both requests):
```
skill__direct_llm: {text}
skill__read_local_files: {text}
skill__index_docs: {description, mode, path, source}
skill__eval: {case_input, case_name, phase_criteria, skill_root, spec_path, target_skill_path}
```

Both `skill__direct_llm: {text}` and `skill__read_local_files: {text}` present in all checked requests. The `b4daeb1` fix is structurally effective.

---

## Verification Angle 3: state_mode Field (B39 #122)

**Result: CONFIRMED** — `state_mode: "fresh"` included in `results-worker-1.json`. `dogfood_fresh_reset.sh` ran before each scenario with confirmed removal log output.

---

## Verification Angle 4: ΔvsB38 Explicit

- **B38 W1**: V=4 / I=0 / R=3 / B=0
- **B39 W1**: V=2 / I=0 / R=5 / B=0
- **Delta**: −2V, +2R

Stable (same verdict): S1 V->V, S2 V->V, S3 R->R, S5 R->R, S7 R->R.

Regressions:
- **S4 V->R (B39-OBS-1)**: LLM chose `skill__direct_llm` for explicit word_stats_demo request. B38 correctly routed to word_stats_demo.
- **S6 V->R (B39-OBS-2)**: direct_llm ran successfully via spawn path, produced Python code examples, but agent final reply summarized rather than surfaced the code.

---

## New Findings

### B39-OBS-1: S4 Routing Regression — direct_llm over word_stats_demo [MED]

Primary data: `.reyn/artifacts/` contains `direct_llm/` artifact, not `word_stats_demo/`. Events confirm direct_llm skill run.

Pre-conclusion checklist: Q1=1 observation, Q2=primary (artifacts+events), Q3=B38 used word_stats_demo (different session), Q4=events+artifacts direct observables, Q5=N=1 needs cross-worker confirmation.

Hypothesis: Fresh hot-list (no prior word_stats_demo usage) causes LLM to default to direct_llm as general-purpose text processor.

### B39-OBS-2: S6 task_completed Narration Collapse — code summarized not surfaced [MED]

Primary data: `history.jsonl` shows `[task_completed]` message containing Python code (`[x**2 for x in range(10)]` etc.), followed by agent reply with zero code — only "コード例が提示されました".

Pre-conclusion checklist: Q1=1 observation, Q2=primary (history.jsonl), Q3=B38 used synchronous web__search (different path), Q4=history.jsonl is primary, Q5=N=1.

Hypothesis: Spawn-path task_completed creates narration attractor — LLM describes what was produced rather than surfaces code content. Different from synchronous tool-result path.

---

## Cost

| Model | Calls | Tokens | USD |
|-------|-------|--------|-----|
| gemini-2.5-flash-lite | 15 | 77,109 | $0.007949 |
| openai/gemini-2.5-flash-lite | 2 | 7,946 | $0.000000 |
| **Total** | **17** | **85,055** | **$0.007949** |
