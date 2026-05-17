# B30 Worker 2 — Dogfood Findings

**Date**: 2026-05-17  
**Branch/HEAD**: `4be42fe` (main, post B29 merges)  
**Scenario set**: `dogfood/scenarios/stdlib_skills_core.yaml` (9 scenarios)  
**Agent**: `dogfood-b30-2` at `/tmp/reyn-worktrees/b30-2`

---

## Verdict Matrix

| ID | Scenario | B27 | B28 | B30 | Notes |
|----|----------|-----|-----|-----|-------|
| S1 | index_docs_basic | R | R | R | rag.operation__create_index hallucination persists |
| S2 | read_local_files_explain_source | R | R | R | Routed file__read directly, no skill_run_spawned |
| S3 | read_local_files_multi_file | R | R | R | Routed file__list directly, no skill_run_spawned |
| S4 | skill_builder_web_summariser | R | R | V | skill__skill_builder invoked, run completed |
| S5 | word_stats_demo_sentence | R | R | I | python.safe step runs (NEW-2 fix), stats not shown synchronously |
| S6 | word_stats_demo_multiline | R | R | I | python.safe step runs (NEW-2 fix), S5 stats appear in S6 session |
| S7 | eval_run_direct_llm | R | R | R | New hallucination: skill__direct_llm_eval (not in skill registry) |
| S8 | chat_compactor_long_session | — | R | I | All 5 turns inline LLM, final reply coherent, no skill artifact |
| S9 | chained_find_then_index | R | R | R | rag.operation__create_index hallucination persists |

**B30 totals: V=1 / I=3 / R=5 / B=0** (vs B28 V=1/I=0/R=8/B=0)

---

## B30 Verification Angles

### C1 Verify: No duplicate function declarations

**Result: PASS — no duplicates found**

All 9 scenario traces checked. Tool lists consistently show 14 entries:
`plan, list_actions, describe_action, invoke_action, file__read, file__list, reyn.source__list, web__search, web__fetch, memory.operation__remember_shared, skill__skill_builder, skill__skill_improver, skill__skill_importer, skill__mcp_search`

No duplicate names appear in any LLM call across all traces. The `_UNIVERSAL_WRAPPER_NAMES` defensive filter in `_build_hot_list_aliases` is effective.

---

### B28-MED-1 Verify (S1, S9): skill__index_docs seeded

**Result: FIX INCOMPLETE — hallucination persists**

`skill__index_docs` IS in `DEFAULT_HOT_LIST_SEED` (verified):

```
DEFAULT_HOT_LIST_SEED = (..., 'skill__read_local_files', 'skill__index_docs')
```

However it does NOT appear in the router tool list. Root cause: `get_top_n(n=10, seed)` truncates at `hot_list_n=10`. The seed has 12 items after MED-1; items 11 (`skill__read_local_files`) and 12 (`skill__index_docs`) are always cut.

Evidence from S1 trace (`53145fad`):
- tools (14): `skill__index_docs` absent
- LLM tool_call: `invoke_action(action_name="rag.operation__create_index")`
- routing_decided: `{"action_name": "rag.operation__create_index", "outcome": "success"}`

Evidence from S9 turn 2: identical hallucination, identical routing_decided.

Fix gap: `hot_list_n` must be bumped to >=13 in `ActionRetrievalConfig` defaults.

---

### B28-NEW-2 Verify (S5, S6): python.safe step succeeds

**Result: FIX VERIFIED**

S5 skill run `20260517T002955Z_word_stats_demo_5216`:
- `preprocessor_step_started {step_type: python}`
- `preprocessor_step_completed {step_type: python}`
- `skill_run_completed {status: finished}`
- No `permission_denied`

S6 skill run `20260517T003110Z_word_stats_demo_331c`:
- Same pattern: step_started → step_completed → skill_run_completed(finished)
- No `permission_denied`

`reyn.yaml`: `permissions: python.safe: allow` confirmed.

Residual: word_stats runs asynchronously; stats surface in next session's completion injection, not the current turn. Rubric "reply contains computed statistic" fails within the session → INCONCLUSIVE.

---

### B29 eval audit verify (S7): skill__eval vs skill__skill_improver

**Result: B29 fix partial — new hallucination variant**

B28 observed `skill__skill_improver` called for eval requests. B30: `skill__skill_improver` NOT observed. However, LLM called `skill__direct_llm_eval` (non-existent).

Evidence from S7 trace (`b97879bf`):
- tools (14): `skill__eval` absent (same N=10 truncation)
- tool_call: `invoke_action(action_name="skill__direct_llm_eval")`
- tool_failed: `ValueError: skill 'direct_llm_eval' not found; available: ['direct_llm', 'eval', ...]`
- routing_decided: `{"action_name": "skill__direct_llm_eval", "outcome": "error"}`

The description disambiguation (B29) eliminated the skill_improver confusion. But without `skill__eval` visible in the tool list, LLM hallucinated a concatenated name. Root cause shared with MED-1: N=10 cap.

---

### routing_decided / chat_turn_completed_inline emit

| Scenario | routing_decided | source | chat_turn_completed_inline |
|----------|----------------|--------|---------------------------|
| S1 | YES | invoke_action (outcome success) | NO |
| S2 | YES | invoke_action (file__read) | NO |
| S3 | YES | invoke_action (file__list) | NO |
| S4 | YES | invoke_action (skill__skill_builder) | YES |
| S5 | YES | invoke_action (skill__word_stats_demo) | YES |
| S6 | YES | invoke_action (skill__word_stats_demo) | YES (x2) |
| S7 | YES | invoke_action (outcome error) | YES |
| S8 | NO (all inline) | — | YES (x5) |
| S9 T1 | NO | list_actions (not invoke_action) | YES |
| S9 T2 | YES | invoke_action (rag.operation__create_index) | NO |

`chat_turn_completed_inline` emits correctly for all inline LLM replies. B28-Q2 case A fix confirmed. `routing_decided` emits on `invoke_action` calls only — correct by design.

---

## New Bug: B30-NEW-1 — hot_list_n=10 truncates MED-1 and eval entries

**Severity: HIGH** — blocks S1, S7, S9; contributes to S2/S3 routing miss

`DEFAULT_HOT_LIST_SEED` has 12 entries but `hot_list_n=10` caps. The last two (`skill__read_local_files`, `skill__index_docs`) never reach the LLM tool list on cold-start sessions. `skill__eval` is not in the seed at all.

Fix: bump `hot_list_n` default to >=13 in `src/reyn/config.py` and add `skill__eval` to `DEFAULT_HOT_LIST_SEED`.

---

## Per-Scenario Detail

### S4 (VERIFIED) — skill_builder_web_summariser
- routing_decided: `skill__skill_builder` via invoke_action, outcome=success
- skill_run_spawned: `20260517T002901Z_skill_builder_1ca4`
- skill_run_completed: `status=finished`
- phases: plan_skill, design_artifacts, review_plan, build_skill
- artifact: `reyn/local/web_article_summarizer/skill.md` written

### S8 (INCONCLUSIVE) — chat_compactor_long_session
- 5 turns all inline LLM; compaction not triggered (5 turns inside head+tail guard)
- Final reply (turn 5) coherently explains routing / UAC / routing_decided — rubric 3/3
- No direct_llm artifact in .reyn/artifacts (inline replies have no artifact file)
- No permission_denied

---

## Fix Verification Summary

| Fix | Status | Evidence |
|-----|--------|---------|
| B28-MED-1 seed skill__index_docs | INCOMPLETE | In seed but hot_list_n=10 cuts it; S1/S9 hallucination persists |
| B28-NEW-2 python.safe allow | VERIFIED | preprocessor_step_completed, no permission_denied, S5/S6 |
| B29 eval/skill_improver disambiguation | PARTIAL | skill_improver not seen; skill__direct_llm_eval new variant |
| B28-Q2 chat_turn_completed_inline | VERIFIED | Emits for all inline turns across all scenarios |
| C1 no duplicate declarations | VERIFIED | No duplicates in any of 9 scenario traces |
