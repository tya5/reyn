# B30 Worker 3 Findings — control_ir_ops

Generated: 2026-05-17
HEAD: 4be42fe
Scenario set: control_ir_ops.yaml (9 scenarios)
B28 W3 baseline: 6V/1I/2R/0B

## Summary

Final: V=2 I=0 R=7 B=0  (ΔvsB28 = -4 verified)

### B30 Verification Angles

| Angle | Result |
|-------|--------|
| C1 (no dup func decl) | PASS — S1 trace: 14 tools, no duplicates |
| routing_decided emit | PARTIAL — emitted for S1/S2/S3/S5/S6/S8/S9; missing S4 (plan tool) and S7 (inline) |
| chat_turn_completed_inline emit | PASS — emitted for S4/S6/S7/S9 (inline/plan path) |
| Q2 exclusivity (routing_decided ⊕ chat_turn_completed_inline) | PASS — mutually exclusive per turn |

---

## Per-Scenario Results

### S1: file_read_via_chat — VERIFIED
- Events: routing_decided ✓, tool_executed ✓, no permission_denied ✓
- Reply: Summarised P1-P8 from principles.md. Rubric satisfied.

### S2: file_glob_grep — REFUTED
- Events: routing_decided ✓ but tool_executed ✗ (tool_failed: KeyError:'path')
- Root cause: LLM used file__list hot-alias with glob params {match, filter} — file__list expects 'path'. Wrong action called.
- Reply: Error message, no file paths listed. Rubric not satisfied.
- REGRESSION vs B28 (was VERIFIED).

### S3: web_search_query — VERIFIED
- Events: routing_decided ✓, web_search_started ✓, web_search_completed ✓, no permission_denied ✓, no web_search_failed ✓
- Reply: Referenced OpenAI SDK results. Rubric satisfied.

### S4: web_fetch_url — REFUTED
- Events: routing_decided ✗ (plan tool used, not catalog action), chat_turn_completed_inline ✓
- web_fetch_started ✓ (within plan step), but routing_decided must_emit fails.
- Reply: Excellent Python 3.12 summary (PEP 695, f-strings, etc.) — reply would pass.
- Root cause: LLM chose plan tool first; plan tool is not a catalog action → routing_decided never fires.
- REGRESSION vs B28 (was VERIFIED — used invoke_action directly).

### S5: sandboxed_exec_simple — REFUTED
- Events: routing_decided ✓ (exec__run, outcome:success), sandboxed_exec_started ✗, sandboxed_exec_completed ✗
- Root cause: exec category hidden (no sandbox backend). LLM tried exec__run (wrong action name). Same env limitation as B28.
- B28=INCONCLUSIVE, B30=REFUTED (stricter classification — routing_decided fires but exec events absent).

### S6: lint_a_skill — REFUTED
- Events: routing_decided ✓, skill_run_spawned ✓, skill_run_failed ✓ — lint_completed ✗
- Root cause: LLM routed skill__skill_improver for "lint a skill". Improver failed: --allow-unsafe-python required.
- Same as B28 (consistently REFUTED).

### S7: recall_indexed_source — REFUTED
- Events: routing_decided ✗, chat_turn_completed_inline ✓ (inline reply, no catalog dispatch)
- Reply: "I can't use recall directly. recall only available in plan steps. Would you like search_actions instead?"
- Rubric: neither returned chunks nor clearly reported no indexed sources. Rubric not satisfied.
- REGRESSION vs B28 (was VERIFIED — routing_decided emitted, graceful not-found reply).
- Possible cause: B28-MED-1 seed (skill__index_docs) changed hot-list composition, LLM now knows recall is plan-only.

### S8: judge_output_direct — REFUTED
- Events: routing_decided ✓, skill_run_spawned ✓, tool_executed ✓ (many) — events VERIFIED
- Reply: "I have initiated the judge_phase skill... I will notify you once it's complete." — no score, no rubric reference.
- Root cause: skill dispatched asynchronously; single-turn CUI captures only the dispatch acknowledgement.
- REGRESSION vs B28 (was VERIFIED — reply contained synchronous phase output JSON).

### S9: ask_user_round_trip — REFUTED
- Session contamination: inbox carried judge_phase task_completed from S8 (in-flight skill run persists across agent recreation).
- Events: routing_decided ✓, skill_run_spawned ✓, skill_run_completed ✓ — user_intervention_requested ✗, user_intervention_received ✗
- Root cause: skill_builder asked skill name via chat reply (not formal ask_user control IR op). Same as B28 (consistently REFUTED).

---

## Regressions vs B28

| Scenario | B28 | B30 | Root Cause |
|----------|-----|-----|------------|
| S2 | VERIFIED | REFUTED | file__list hot-alias KeyError:'path' |
| S4 | VERIFIED | REFUTED | plan tool bypasses routing_decided |
| S5 | INCONCLUSIVE | REFUTED | Same env limit, now routing_decided fires but exec events absent |
| S7 | VERIFIED | REFUTED | LLM inline reply — no catalog dispatch |
| S8 | VERIFIED | REFUTED | Async dispatch — single-turn reply lacks score |

---

## New Issues Found

- **B30-NEW-1 [HIGH]**: file__list hot-alias called with glob args → KeyError:'path' (S2)
- **B30-NEW-2 [MED]**: plan-first routing for web_fetch bypasses routing_decided (S4)
- **B30-NEW-3 [MED]**: recall "plan-only" inline response — no catalog dispatch, rubric fails (S7)
- **B30-NEW-4 [MED]**: judge_output async dispatch — single-turn reply is dispatch-ack, not result (S8)
- **B30-NEW-5 [LOW]**: Agent inbox contamination from previous scenario's in-flight skill (S9)

