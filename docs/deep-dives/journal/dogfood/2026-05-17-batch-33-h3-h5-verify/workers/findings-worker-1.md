# Batch 33 Worker 1 Findings — chat_router_smoke (7 scenarios)

**Date**: 2026-05-17
**HEAD**: 08ccc27 (feat/fp-0034-phase1-universal-catalog)
**Agent**: dogfood-b33-1
**Reyn worktree**: /tmp/reyn-worktrees/b33-1
**Set**: chat_router_smoke (7 scenarios)
**Baseline**: B32 W1 = 4/0/3/0

---

## 0. Run summary

| Item | Value |
|---|---|
| HEAD | `08ccc27` |
| Scenarios | 7 |
| Aggregate | **V=2 / I=1 / R=4 / B=0** |
| Delta vs B32 | -2V |
| H3 spawn_ack_exit fires | 1/1 applicable (S4) |
| session_restored observed | 0/7 (wipe clean) |
| Mean Brier | 0.446 |

---

## 1. Verdict Matrix

| Scenario | B28 | B30 | B32 | B33 | Notes |
|---|---|---|---|---|---|
| simple_capability_question | — | I | V | R | Asked back, no capabilities described |
| factual_query_direct_llm | — | R | V | V | Excellent idempotency explanation inline |
| skill_discovery_request | — | R | R | R | routing_decided not emitted (list_actions inline path) |
| explicit_skill_invocation_word_stats | — | R | V | I | Skill lifecycle complete, H3 fires, reply vague on statistics |
| catalog_routing_decided_emitted | — | R | R | R | Agent asked for poem theme, did not write poem |
| multi_turn_pronoun_reference | — | R | V | V | Multi-turn coherent, Python code examples present |
| out_of_scope_graceful_decline | — | R | R | R | Asked for image details, did not decline |

**B33 W1 V/I/R/B = 2/1/4/0 | Delta vs B32 = -2V**

---

## 2. Per-scenario evidence

### S1: simple_capability_question — REFUTED

**Reply**: "私はReynエージェントです。Reynプロジェクトのチャットルーターとして機能します。Reynの機能について知りたいですか？それとも、特定のタスクを実行したいですか？"

**Events**: user_message_received + chat_turn_completed_inline. No permission_denied. No session_restored.

**Rubric**: "mentions skills, agents, or chat capabilities at a high level" — FAIL. Reply mentions "Reynエージェント" but does not describe skills or capabilities; asks back with a clarifying question. B32 W1 S1 reply enumerated specific tool names — this run's LLM chose question-back pattern instead.

**Verdict**: REFUTED — reply_pass=false

---

### S2: factual_query_direct_llm — VERIFIED

**Reply**: Comprehensive explanation of idempotency with HTTP method examples (GET/PUT/DELETE), delete-and-retry scenario. Clear, factual, comprehensible.

**Events**: user_message_received + tool_failed(search_actions: unknown_tool) + invoke_action(rag.corpus__readme) + routing_decided + chat reply. No permission_denied. No session_restored.

**Notable**: search_actions tool not in catalog (pre-existing gap). Router fell back to invoke_action via RAG. Reply quality unaffected.

**Rubric**: "explains the concept of idempotency in a factual and comprehensible way" — PASS.

**Verdict**: VERIFIED — reply_pass=true, events_pass=true

---

### S3: skill_discovery_request — REFUTED

**Reply**: Complete list of 16 skills with Japanese descriptions. Content excellent.

**Events**: user_message_received + list_actions(category=["skill"]) + tool_returned(16 items) + chat_turn_completed_inline. routing_decided NOT emitted. No permission_denied. No session_restored.

**Events check**: must_emit routing_decided — FAIL. list_actions invocation does not trigger routing_decided (only invoke_action dispatch does).

**Verdict**: REFUTED — events_pass=false. Structural gap unchanged from B28/B30/B32.

---

### S4: explicit_skill_invocation_word_stats — INCONCLUSIVE

**Reply**: "The word_stats_demo skill has finished processing the text. It analyzed the text and provided a commentary on its length and token estimation." — vague narration, no specific numbers.

**Events (chat)**:
- user_message_received → invoke_action(skill__word_stats_demo) → skill_run_spawned → routing_decided → invoke_skill_spawn_ack_exit(spawn_ack_count=1) → skill_run_completed(finished) → skill_completion_injected → chat_turn_completed_inline
- No permission_denied. No session_restored.

**Events (skill workflow)**:
- workflow_started → artifact_created(_input) → python_step_started/completed → artifact_created(review_preprocessed) → llm_called → llm_response → artifact_created(review/v01_text_review.json) → workflow_finished

**Rubric**: "reply contains word count, character count, or similar statistics" — FAIL. Skill DID compute stats (char_count=44, estimated_tokens=10) and commentary artifact contains them, but router narrated the completion abstractly without relaying numbers.

**H3 verify**: invoke_skill_spawn_ack_exit FIRED at 2026-05-17T11:42:14.076279. Reply came via skill_completion_injected path (confirmed by event sequence). (answered) race NOT observed. H3 fix structurally verified.

**Artifacts**: word_stats_demo artifacts created in .reyn/artifacts/word_stats_demo/ — artifacts_pass=true.

**Verdict**: INCONCLUSIVE — events_pass=true (all must_emit fired), artifacts_pass=true, reply_pass=false (no specific statistics). OS/structural behavior correct; reply quality is LLM variance.

---

### S5: catalog_routing_decided_emitted — REFUTED

**Reply**: "承知いたしました。どのようなテーマや雰囲気の詩をご希望ですか？例えば、自然、感情、季節など..."

**Events**: chat_started(clean) + user_message_received + chat_turn_completed_inline. No permission_denied. No session_restored (wipe fix confirmed effective).

**Events check**: must_emit_any(routing_decided OR chat_turn_completed_inline) — PASS (chat_turn_completed_inline fired).

**Rubric**: "reply contains a poem or poem-like text" — FAIL. LLM asked for theme clarification instead of writing a poem. Stable behavior across B28/B30/B32/B33.

**Wipe note**: B32 S5 was refuted due to WAL contamination (session_restored injected S4's word_stats completion). B33 S5 has NO session_restored — wipe recipe fix confirmed. Refuted for a different reason (LLM clarification pattern).

**Verdict**: REFUTED — reply_pass=false

---

### S6: multi_turn_pronoun_reference — VERIFIED

**Turn 1 reply**: Concise one-line explanation of Python list comprehension.

**Turn 2 reply**: 4 Python code examples demonstrating list comprehension (even filter, doubling, if-else, nested). Coherent with turn 1.

**Events**: Two user_message_received + two inline completions. Turn 2 triggered web__search (routing_decided emitted). No permission_denied. No session_restored.

**Rubric**: "second reply contains Python code example demonstrating list comprehension" — PASS. "Coherent with first turn" — PASS.

**Verdict**: VERIFIED — reply_pass=true, events_pass=true

---

### S7: out_of_scope_graceful_decline — REFUTED

**Reply**: "どのような画像を生成したいですか？...画像の内容、スタイル、色調などを指定していただけると..."

**Events**: user_message_received + chat_turn_completed_inline. No permission_denied. No session_restored.

**Rubric**: "politely declines or explains capability not available" — FAIL. Agent treats image generation as actionable and solicits details. Pattern stable across B28/B30/B32/B33.

**Verdict**: REFUTED — reply_pass=false

---

## H3 verify

**Patch**: Router exits on invoke_skill/invoke_action spawn-ack via invoke_skill_spawn_ack_exit event (commit 25834de/60f6055).

**Applicable scenarios**: S4 only (word_stats_demo spawn).

**Evidence**:
- invoke_skill_spawn_ack_exit fired at 2026-05-17T11:42:14.076279 with spawn_ack_count=1
- skill_completion_injected fired at 2026-05-17T11:42:17.377460 — reply via skill completion path
- chat_turn_completed_inline fired AFTER skill_completion_injected — correct ordering
- Reply narrates skill completion (not a hallucinated "Understood" pre-skill response)

**Verdict**: H3 fires 1/1 applicable. (answered) race NOT observed. Structural fix verified.

**Caveat**: Reply quality (whether statistics appear in chat reply) is LLM-non-deterministic and not addressed by H3. Inconclusive on S4 reply rubric is separate from H3 race fix.

---

## Wipe completeness verify

**Recipe applied** (complete B33 recipe):
```bash
rm -rf .reyn/events .reyn/agents/dogfood-b33-1/events
rm -f  .reyn/state/action_usage.jsonl .reyn/state/wal.jsonl
rm -rf .reyn/state/plans/
rm -rf reyn/local/
rm -f  .reyn/agents/dogfood-b33-1/history.jsonl
```

**Result**: 0/7 scenarios had session_restored event. Direct inspection of all 7 event files confirms zero occurrences.

B32-NEW-FINDING-1 (wal.jsonl gap) and B32-NEW-FINDING-2 (history.jsonl gap) are fully addressed. **Wipe CLEAN.**

---

## Brier score

| Scenario | Actual | p(V) | p(I) | p(R) | p(B) | Brier |
|---|---|---|---|---|---|---|
| simple_capability_question | R | 0.25 | 0.0 | 0.75 | 0.0 | 0.125 |
| factual_query_direct_llm | V | 0.25 | 0.0 | 0.75 | 0.0 | 1.125 |
| skill_discovery_request | R | 0.0 | 0.0 | 1.0 | 0.0 | 0.0 |
| explicit_skill_invocation_word_stats | I | 0.25 | 0.25 | 0.25 | 0.25 | 0.75 |
| catalog_routing_decided_emitted | R | 0.0 | 0.0 | 0.75 | 0.25 | 0.125 |
| multi_turn_pronoun_reference | V | 0.25 | 0.0 | 0.5 | 0.25 | 0.875 |
| out_of_scope_graceful_decline | R | 0.0 | 0.0 | 0.75 | 0.25 | 0.125 |
| **Total / Mean** | | | | | | **3.125 / 0.446** |

S3 prediction perfect (Brier=0). S2 highest contribution (1.125) — H5 refit set 75% refuted but actual was verified (factual queries reliably answered inline). S4 inconclusive is within the predicted equal-weight distribution.

---

## C1/Q2 stability

- **C1**: No duplicate event types in any session across all 7 scenarios. ✓
- **Q2**: chat_turn_completed_inline emitted for all inline-path completions (S1, S2, S3, S5, S7 single; S6 twice; S4 post-skill-completion). ✓

---

## New findings surfaced in B33 W1

### B33-W1-OBS-1: S1 capability description is LLM probabilistic (N=1)

B32 S1 enumerated tools; B33 S1 asked back. No code change between runs. Rubric sensitivity to LLM output style is high. H5 refit (verified=0.25) is appropriately calibrated. Not a regression.

### B33-W1-OBS-2: H3 reply quality still LLM-non-deterministic post-fix

H3 ensures reply comes from skill_completion_injected path. But whether the router LLM narrates statistics from the artifact or describes completion abstractly is not enforced. B32 relayed "単語数: 9..." while B33 said "commentary on its length". Envelope-layer or system-prompt guidance could enforce "relay the artifact output verbatim" but this is not currently done.

### B33-W1-OBS-3: S5 poem clarification pattern stable (4 batches)

LLM asks for theme clarification on "短い詩を書いてください" across B28/B30/B32/B33. Consistently refuted. Fix candidate: add system-prompt or routing rule that treats simple creative requests as directly answerable without clarification.
