# H2 judge rubric tightness ablation

**Date**: 2026-05-17
**Worker**: H2 (info-gathering only)
**HEAD**: c8fae2e (feat/fp-0034-phase1-universal-catalog)
**Model**: gemini-2.5-flash-lite via LiteLLM proxy (localhost:4000)

---

## Summary

- N refuted scenarios re-judged: 5
- N flipped to verified under loose rubric: 3
- Flip rate: 3/5 = **0.60 (60%)**
- Surface conclusion: rubric-bound (>= 0.30 threshold)
- **CRITICAL CAVEAT**: The flip rate overstates rubric tightness as a plateau driver. See §3 for full analysis.

---

## Per-scenario re-judge

| Scenario | Original rubric (snippet) | Loose variant (snippet) | Original judge | Loose judge | Flipped | Root cause |
|---|---|---|---|---|---|---|
| W1_S3 skill_discovery_request | "mentions at least one skill name or describes the skill catalogue" | "communicates something about available skills, tools, or capabilities" | score=1.0 PASS | score=1.0 PASS | No (already passed orig) | Event-driven: routing_decided missing. Rubric content was NOT the bottleneck. |
| W1_S7 out_of_scope_graceful_decline | "politely declines or explains capability not available" | "does not pretend to generate an image; a clarifying question before acting is acceptable" | score=1.0 PASS | score=1.0 PASS | No (already passed orig) | Rubric ambiguity: human worker and LLM judge disagree. See §2.2. |
| W2_S3 read_local_files_multi_file | "describes at least one file in src/reyn/op_runtime/; references Control IR ops" | "either describes files OR honestly explains why it cannot access" | score=0.0 FAIL | score=1.0 PASS | **Yes** | Mixed: event fail + rubric assumes access that was not available |
| W5_S5 a2a_task_lifecycle_status_poll | "describes message/send JSON-RPC method; mentions GET /a2a/tasks/{run_id}" | "explains A2A workflow or what information is needed; does not fabricate" | score=0.2 FAIL | score=0.7 PASS | **Yes** | Rubric-driven: tight on specific protocol terms; reply explained workflow without terms |
| W5_S7 cron_schedule_status | "lists cron jobs OR explains no cron jobs configured; shows how to configure via reyn.yaml" | "explains inability to retrieve; mentions reyn.yaml or reyn CLI; does not fabricate" | score=0.6 FAIL | score=0.7 PASS | **Yes** | Mixed: event fail + rubric slightly above passing threshold for honest "cannot retrieve" |

---

## Quantitative

- N refuted scenarios re-judged: 5
- N flipped to verified under loose rubric: 3 (W2_S3, W5_S5, W5_S7)
- Flip rate: 3/5 = 0.60
- Conclusion using 0.30 threshold: **rubric-bound**

---

## Critical analysis — why the flip rate overstates rubric tightness

### 3.1 The 5 scenarios are not a representative sample of all 22 B32 refuted

The 5 scenarios were selected to be "plausibly rubric-tight". To assess rubric tightness as a **plateau driver** for the full 22 refuted scenarios, we need to classify all 22.

Full classification of 21 catalogued refuted scenarios (B32 findings.md + worker reports):

| Class | Count | Examples |
|---|---|---|
| **Event-driven only** (structural event missing; rubric content often passes) | 11 | routing_decided missing, skill_run_failed, SafeModeViolation, no routing rule, WAL contamination |
| **Rubric-driven** (rubric content genuinely failed) | 5 | file_read hallucinated content, spawn-ack hallucination, P7 rubric not met, skill_builder anti-optimism |
| **Mixed** (both event fail AND rubric content fail) | 5 | cannot-access honest reply + routing miss, cron wrong category + rubric |

**Of the 11 event-driven refuted scenarios: rubric tightness is irrelevant** — the system failed structurally before the rubric could apply. Loose rubric variants would not flip these because the scenarios are scored refuted due to `must_emit` failures, not rubric content.

**Adjusted rubric-accessible pool**: 5 rubric-driven + 5 mixed = 10 scenarios where rubric content contributed to refuted.

### 3.2 What the flip rate means within the rubric-accessible pool

Within the 5 selected scenarios (biased toward rubric-tight candidates):
- 2 scenarios (W1_S3, W1_S7) were NOT flipped because the original rubric judge ALREADY scored them as PASS (score=1.0). The B32 human worker scored them as refuted based on events (W1_S3) or strict interpretation (W1_S7). This reveals:
  - W1_S3: rubric tightness is entirely irrelevant; fix is `routing_decided` event path
  - W1_S7: human scorer and LLM judge interpret the same rubric differently (ambiguity, not tightness)
- 3 scenarios (W2_S3, W5_S5, W5_S7) flipped under loose rubric

### 3.3 Projecting to the full 22 refuted population

Pre-conclusion checklist compliance:
1. Observations listed: 21 scenarios classified, 5 directly re-judged
2. Primary data: event logs (findings.md, worker reports) + LLM judge scores
3. Falsifying evidence: 11/22 refuted scenarios have NO rubric content issue at all
4. Observation infrastructure: the judge calls above are primary; the classification of the other 17 is from worker primary data (event logs, reply texts)
5. "3/5" = directly inspected 5, projected the other 17 via classification

**Hypothesis-grade estimate** (not directly verified for all 22):
- Of the 22 refuted, ~10 have rubric-content contribution
- Of those 10, a loose rubric variant might flip 3-6 (30-60% of the rubric-accessible pool)
- That is 3-6 of 22 total = **14-27% of total refuted**

### 3.4 Dominant plateau driver

Rubric tightness is a **real but secondary driver**. The dominant plateau driver is **structural event failures** (routing_decided not emitted, skill_run_failed, no routing rule, WAL contamination). These account for 11/22 refuted scenarios and are immune to rubric loosening.

---

## Notes on rubric wording patterns that flipped

### Pattern 1: "describes X" vs "addresses X or explains inability to access X"

- **Tight wording**: "describes at least one file in src/reyn/op_runtime/"
- **Loose flip**: "either describes files OR honestly explains why it cannot access"
- **W2_S3**: Reply gave honest "cannot access" — tight rubric fails it because it didn't describe content; loose rubric passes it as honest capability explanation.

### Pattern 2: Specific protocol terms required vs workflow explanation accepted

- **Tight wording**: "mentions GET /a2a/tasks/{run_id} for polling; describes message/send JSON-RPC method"
- **Loose flip**: "explains A2A workflow or what information is needed"
- **W5_S5**: Reply explained workflow without using the literal endpoint path. Tight rubric fails because the literal path was absent; loose rubric passes because the explanation was substantively useful.

### Pattern 3: Rubric ambiguity (not tightness) — human vs LLM interpretation

- **W1_S7**: "politely declines or explains capability not available"
- **LLM judge read this as PASS** (score=1.0 on original wording) — asking for image details before acting reads as preparation, not deception
- **B32 human worker read this as FAIL** — the expected response was an immediate decline, not a clarifying question
- This is rubric **ambiguity**, not tightness: two reasonable readers disagree on the same wording. Loose variant merely makes the human-worker interpretation explicit.

---

## Per-scenario primary data quotes

### W1_S3 skill_discovery_request
- B32 worker note: "Reply listed all 16 skills with descriptions (rubric content PASS, but structural requirement fails)"
- LLM judge orig score: 1.0 — "output lists 16 available skills by name, does not return a bare error"
- **Conclusion**: Not rubric-tightness. Event failure (routing_decided not emitted) is the bottleneck.

### W1_S7 out_of_scope_graceful_decline
- Reply text: "どのような画像を生成したいですか？画像の内容、スタイル、色調など..."
- B32 worker: "Rubric 1 fail: no polite decline" (strict reading)
- LLM judge orig score: 1.0 — "politely asks for details; does not pretend to generate"
- **Conclusion**: Rubric ambiguity. Fix is rubric clarification ("decline outright OR acknowledge inability"), not loosening.

### W2_S3 read_local_files_multi_file
- Reply text: "アクセス方法が見つかりませんでした [...] ツールが現在利用できない可能性があります"
- Orig score: 0.0 — "does not reference any files in src/reyn/op_runtime/"
- Loose score: 1.0 — "correctly states the directory cannot be accessed without fabricating contents"
- **Conclusion**: Tight rubric assumes the skill will succeed. When it doesn't route correctly, the honest "cannot access" reply fails the tight rubric. Loose variant is fairer to the actual system behavior.

### W5_S5 a2a_task_lifecycle_status_poll
- Reply text mentions "reyn web" and "A2Aエンドポイント" but not "/a2a/tasks/{run_id}" or "message/send JSON-RPC"
- Orig score: 0.2 — "hints at A2A endpoints but does not describe JSON-RPC method or GET path"
- Loose score: 0.7 — "correctly identifies need to start reyn web; recommends checking reyn.yaml"
- **Conclusion**: Tight rubric requires specific API terms the LLM doesn't have in context. Loose variant credits substantive workflow understanding.

### W5_S7 cron_schedule_status
- Reply mentions "reyn.yaml設定でcronジョブを設定することができます" (reyn.yaml mentioned)
- Orig score: 0.6 — "does not explicitly list jobs; does not offer concrete configuration examples"
- Loose score: 0.7 — "correctly states cannot retrieve; mentions reyn.yaml as configuration mechanism"
- **Conclusion**: Original rubric marginally failed by requiring "shows how to configure"; reply mentioned reyn.yaml which satisfies the loose variant. Thin margin.

---

## Rubric reform recommendations (signal, not prescription)

1. **For capability-assumption rubrics** (W2_S3 class): Add "OR honestly explains that the capability is not available" branches to rubric bullets that assume skill success. Cost: minimal; reduces false refutals when routing fails.

2. **For protocol-specificity rubrics** (W5_S5 class): Distinguish "must mention X" from "demonstrates understanding of X". Reserve literal-term requirements for scenarios where the LLM has that information in context.

3. **For ambiguous rubrics** (W1_S7 class): Make the expected failure mode explicit. "politely declines outright — clarifying questions do NOT count as a decline" removes the ambiguity that caused human-vs-LLM scorer disagreement.

4. **Event vs rubric scoring separation**: The dominant finding is that event failures (routing_decided missing) account for 11/22 refuted and are unaffected by rubric loosening. Rubric improvements should be pursued alongside (not instead of) routing fixes.

---

## Conclusion

Rubric tightness is a real but secondary plateau driver. 3/5 selected scenarios flipped under loose rubric (60%). However, these 5 were specifically selected for plausible tightness. In the full population of 22 B32 refuted scenarios, 11 are purely event-driven (structural failures immune to rubric loosening). The rubric-accessible pool is ~10 scenarios; within that pool, rubric loosening may flip 30-60%. The plateau is **primarily structural** (event/routing failures), with rubric tightness as a **secondary contributor** affecting approximately 3-6 of 22 total refuted (14-27%).

**Primary plateau drivers** (fix these for verified-rate improvement):
1. routing_decided not emitted when LLM uses list_actions inline path (W1_S3, W5_S6, W5_S7, W6 plan scenarios) — 5+ scenarios
2. skill_run_failed due to SafeModeViolation / unsafe-python gaps — 3+ scenarios
3. Missing routing rules (file__grep, exec, mcp_install) — 3 scenarios
4. WAL/history contamination between scenarios — transient but inflates R count

**Secondary plateau drivers** (rubric improvements):
1. Capability-assumption rubrics fail on honest "cannot access" replies — 2-3 scenarios
2. Protocol-specificity rubrics fail on workflow explanations without literal terms — 1-2 scenarios
3. Rubric ambiguity causing human-scorer vs LLM-judge disagreement — 1-2 scenarios
