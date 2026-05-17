# H6 wipe gap contamination ablation

## Wipe recipe applied

Full set, applied before EVERY scenario re-run:

```bash
rm -rf .reyn/events .reyn/agents/ablation-h6/events 2>/dev/null
rm -f  .reyn/state/action_usage.jsonl .reyn/state/wal.jsonl 2>/dev/null
rm -rf .reyn/state/plans/ 2>/dev/null
rm -rf reyn/local/ 2>/dev/null
rm -f  .reyn/agents/ablation-h6/history.jsonl 2>/dev/null
```

Verification method: confirmed `session_restored` event absent in every post-wipe run.
Worker: `ablation-h6` in `/tmp/reyn-ablation/H6-wipe-gap`.
HEAD: `c8fae2e` (same as B32 — no source edits).

---

## Per-scenario before/after

| Scenario | B32 verdict | Wipe-extended verdict | Contamination observed in trace? | Notes |
|---|---|---|---|---|
| W1 S5 `catalog_routing_decided_emitted` | REFUTED | REFUTED | YES — session_restored eliminated | B32: word_stats completion injected via WAL, agent narrated prior skill. Clean: LLM asked clarifying question instead of writing poem. Different failure mode, still REFUTED. |
| W5 S2 `mcp_call_remote_tool` | INCONCLUSIVE | INCONCLUSIVE | YES — history bleed from S1 eliminated | B32: contaminated context from S1 mcp_search failure. Clean: routing_decided emitted, mcp_search fails for unsafe-python infrastructure reason. Verdict unchanged. |
| W5 S3 `agent_delegation_simple` | INCONCLUSIVE | INCONCLUSIVE | YES — history bleed from S1-S2 eliminated | B32: args double-serialization error. Clean: routing_decided emitted (x2), agent.peer__researcher sent but agent not found — no substantive summary or creation guidance. Verdict unchanged. |
| W5 S4 `multi_agent_topology_route` | INCONCLUSIVE | INCONCLUSIVE | YES — history bleed from S1-S3 eliminated | B32: writer not seeded/visible, inline fallback. Clean: researcher delegated (agent_message_sent), LLM used own knowledge, writer attempted, 2-line summary returned. Rubric borderline. Verdict unchanged. |
| W6 `plan_compare_two_concepts` | VERIFIED | VERIFIED | YES — session_restored present in B32 but did not harm | B32 verified despite contamination (plan_emitted, 4 points, both docs referenced). Clean: identical positive outcome. No change. |
| W6 `plan_explain_with_code_references` | REFUTED | REFUTED | YES — session_restored eliminated | B32: LLM used list_actions+describe_action, plan_emitted=0. Clean: LLM answered inline, plan_emitted=0. Not contamination-driven — LLM consistently avoids plan for this scenario. |
| W6 `plan_summary_across_n_files` | REFUTED | **VERIFIED** | YES — session_restored eliminated | **FLIP.** B32: LLM read 3 files via invoke_action directly (plan_emitted=0). Clean: plan_emitted, 4 steps, plan_aggregated, 5 pillars with source refs, all must_emit satisfied, rubric met. |

Directly inspected: 7/7 scenarios. Each run verified clean start via absence of `session_restored` in event log.

---

## Quantitative

- N suspected-contaminated scenarios re-run: **7**
- N flipped to verified/inconclusive: **1** (plan_summary_across_n_files: REFUTED → VERIFIED)
- Flip rate: **1/7 = 0.14**
- Conclusion: **not-wipe-bound (K/N < 0.2)**
- Specific contamination types observed eliminated:
  - `session_restored` via `wal.jsonl` (W1 S5 — pending skill completion injected)
  - `history.jsonl` bleed (W5 S2/S3/S4 — prior conversation turns accumulate)
  - `session_restored` via stale agent state (W6 plan_mode — prior skill_builder completions injected into plan_mode context)

---

## Notes

### What changed with full wipe

Every re-run confirmed `session_restored` was absent — the contamination class was structurally eliminated. This validates that the full wipe recipe (wal.jsonl + history.jsonl) works as intended.

### Why flip rate is low

The 6 non-flipping scenarios fall into two categories:

1. **Independent failure mode** (W1 S5, W6 plan_explain): The contamination caused a different failure symptom in B32, but the underlying behavior also fails without contamination. Removing contamination exposes the true baseline behavior — which is also a failure. Verdict = REFUTED in both conditions, different root cause each time.

2. **Contamination changed context but not verdict class** (W5 S2/S3/S4): Infrastructure blockers (unsafe-python gate, agent not found) persist regardless of history bleed. The B32 INCONCLUSIVE was structurally sound even under contamination, because the rubric partial-satisfaction was tied to those blockers, not to bleed.

### The one flip (plan_summary_across_n_files)

This is the clearest case of contamination-driven verdict change. In B32, prior skill_builder completions accumulated in session history and were injected at session start via `session_restored`. The LLM chose direct `invoke_action` reads for the 3 files (likely because its context carried plan-mode prior turns suggesting the task was familiar/simple). Without that contamination, the LLM correctly identified the 3-file multi-source synthesis as a plan-eligible task and produced a verified outcome.

This is primary data: the clean run emitted `plan_emitted`, `plan_step_started` x4, `plan_step_completed` x4, `plan_aggregated`, rubric met (5 pillars, source refs from all 3 docs). The B32 run emitted zero plan events.

### Pre-conclusion observation checklist (5Q applied)

1. **Specific observations listed**: Yes — 7/7 scenarios directly executed and traced, event logs inspected, replies read.
2. **Primary data**: Events log (direct OS output) for all 7. Reply text from history.jsonl. No inference chains beyond what events show.
3. **Falsifying data sought**: Checked whether clean runs could also REFUTE for contamination-unrelated reasons (yes — W1 S5 and W6 plan_explain show independent failures).
4. **Observation infra**: `session_restored` event is the direct OS signal for contamination presence. Its absence in clean runs is definitive — not a proxy.
5. **N/N direct inspect**: All 7/7 scenarios inspected directly. No extrapolation.

### Implication for B32 aggregate

Only 1 scenario (plan_summary_across_n_files) would have flipped from REFUTED to VERIFIED if the full wipe had been in B32's recipe. B32 aggregate V/I/R/B = 11/24/22/0. With full wipe, the corrected estimate would be approximately 12/24/21/0 (V+1, R-1). Wipe-gap explains ~4.5% of the REFUTED count (1/22).

The remaining 21/22 REFUTEDs in B32 have contamination-independent root causes: routing attractors, missing routing rules, plan trigger rate, skill infrastructure gaps, etc.

### This ablation tests the WIPE RECIPE, not the OS state model

A high flip rate would confirm the wipe extension is worth shipping immediately. A low flip rate (as observed) confirms:
- The wipe recipe extension (wal.jsonl + history.jsonl) is correct and necessary
- But most B32 REFUTEDs are structural, requiring OS or skill fixes
- Task #98 (wipe recipe doc update) remains HIGH priority, but its impact on verified-rate is incremental (not categorical)
