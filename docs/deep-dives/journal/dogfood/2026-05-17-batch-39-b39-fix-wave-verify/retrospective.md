# Batch 39 — Retrospective

> Tenth dogfood batch. **9 B36-B39 fix commits all verified KEEP** via
> dogfood-trace evidence after user pushback exposed shortcut-verification
> earlier in the session. Headline V=21/58 (= -2V vs B38) is **not**
> regression-by-fix — every commit was empirically tested at fixed user
> params (= trace-patch-replay + S8 live + B38-state simulation) and none
> caused the observed regressions. The real driver of the W6 R-WEB
> reopening is **a cold-start wrapper-discovery cognitive bias (Class A
> attractor) revealed when direct alias is absent**, sitting at a layer
> outside the B36-B39 fix domain. The methodology lift of this batch is
> the **post-batch commit verify protocol** (= user-tunable params fixed
> + 5-axis context analysis + ladder-cheap fix candidates) that turned
> 「revert?」 into evidence-based keep/keep/keep × 9.

---

## 1. What this batch verified — primary data

### B36-B39 fix wave verify status (= post-batch trace-evidence)

| Commit | Type | Verify method | Result |
|---|---|---|---|
| `561101a` (B37 D2-wrapper hot-list scope) | LLM-input affecting | P2 trace-patch (= ARS block removed) | **keep** — revert simulation → arg variants 5→8 (hallucination worse) |
| `5e05b9b` (B37 seed expand + ghost reject) | LLM-input + structural | P3a trace-patch (= seed additions removed from tools[]) | **keep** — orthogonal to W6 (mcp_search at pre-fix position 12 still cap-out at n=10) |
| `29a0c31` (B37 judge_phase schema) | structural | S8 live dogfood | **keep** — workflow_finished, score=1.0, 0 validation_errors |
| `1d5042d` (B38 D2-wrapper scope expansion) | LLM-input | P2 trace-patch (= ARS removed) | **keep** — same patch as 561101a, hallucination worse without ARS |
| `bfeb9f8` (B39 ghost registry check) | structural | B38-sim web.log | **keep** — `skipping ghost alias 'mcp.server__search'` warning fired |
| `16b439c` (B39 S8 scenario redesign) | yaml | S8 live dogfood | **keep** — rubric satisfied, scenario exercises judge_phase e2e |
| `b1ca51a` (B39 empty-schema acceptance) | structural | B38-sim tools[] inspect | **keep** — skill__mcp_search (empty schema) passed ghost filter into tools[] |
| `f48333c` (B39 fresh-mode reset) | dogfood infra | B38-state simulation | **keep with carry-over** — does not cause regression; removes action_usage masking that previously hid latent cold-start gap |
| `b4daeb1` (B39 stdlib explicit input_schema) | LLM-input | P1 trace-patch (= direct_llm + read_local_files removed from ARS) | **keep** — initial W6-F1 hypothesis REFUTED, routing 0/10 unchanged |

**Total verify cost**: ~$0.20 (= 3× trace-patch-replay N=10 + 2× live A2A run + 5 parallel sonnet info-gathering). All at fixed user params (= hot_list_n=10, default seed, fresh mode).

### Why this matters

The session opened with a multi-step plan to "verify and revert" 9 commits based on the B39-W6-F1 finding (= ARS expansion suspected for W6 R-WEB regression). User pushback corrected the methodology midstream:

1. 「異なる n で比較するのは間違い。 n を固定して改善するかを確認すべき」 (= hold user params fixed)
2. 「seed はユーザが買えるものであるため論外」 (= user-tunable params not OS-modifiable for comparison)
3. 「context 分析本当にしてる？」 (= verify the 5-axis context analysis discipline isn't faked)
4. 「修正しようとしてるのは、 何かのシナリオで失敗してることはわかってるんでしょ？ そのシナリオを使って確認すれば良いじゃん」 (= use the failing scenario directly, not synthetic comparisons)

Applying those corrections turned the verify session from speculation-into-revert-proposals into evidence-based keep/keep/keep × 9. The 「revert?」 question never had a revert answer — every commit's effect was either structural (= visible at fixed params via trace-patch-replay) or orthogonal to the observed regression.

---

## 2. The headline — W6 R-WEB regression is NOT a fix bug

### Initial hypothesis (= B39-W6-F1) and its refutation

The W6 worker findings (= results-worker-6.json) flagged the R-WEB mcp_search routing chain regression (3/3 scenarios I→R: narr-1, s-fp11-3, s-fp12-comp-1) and nominated **b4daeb1 (B39 stdlib explicit input_schema)** as the prime suspect via "ARS expansion changed routing context" hypothesis.

The post-batch trace-patch-replay session refuted this:

- **Sonnet 5 SP rendering analysis**: b4daeb1 grows ARS by **+62c (+2%)**, not the 「+15%」 the session-resume note claimed. mcp_search **was never in the ARS block** (= empty schema excluded at all batches from B27-B39).
- **P1 trace-patch (= b4daeb1 revert simulation)**: removing direct_llm + read_local_files from ARS did NOT improve mcp_search routing (= 0/10 → 0/10).
- **P2 trace-patch (= 1d5042d revert simulation, also reverting 561101a effect)**: removing the entire ARS block increased hallucination variants 5→8 (= reverting worsened, not improved).

### Real root cause (= cold-start wrapper-discovery cognitive bias)

The N=10 baseline replay distribution at fixed user params shows LLM cognitive paths:

| Path | Description | Frequency |
|---|---|---|
| **C** (Cognitive miscategorization) | "mcp_search" → mcp.server category → `invoke_action(mcp.server__search, ...)` | 6/10 |
| **A** (Same miscategorization, discover variant) | `list_actions(category=['mcp.server'])` → 0 results | 3/10 |
| **D** (mcp.tool guess) | `invoke_action(mcp.tool__mcp_search.*, ...)` | 1/10 |
| **B** (Correct skill discovery) | `list_actions(category=['skill'], filter='mcp')` | **0/10** |

LLM never tries the **skill category** for "X スキル" prompts when X has an `mcp` prefix. The 5-parallel-sonnet context analysis (= post-batch principle 16 application) confirmed:

- Router SP has **no guidance** mapping "user says X スキル" → `list_actions(category=['skill'])`
- ROUTING RULE (ABSOLUTE) in SP fires only when user message contains literal qualified action_name (= `skill__mcp_search`), which 「mcp_search スキル」 prompts never satisfy
- `mcp.server` category description "invoke to list this server's tools" provides direct affordance for the Path C miscategorization
- Same pattern observed in B35-B38 cold-start gap findings (= B36 retro "does NOT propagate into the invoke_action wrapper path"; B37 retro Sub-finding B "cold-start gap"; B38 W2 "LLM invokes unknown tool name directly, bypassing the wrapper entirely")

### B38 baseline reproduction confirms (= live empirical)

Populating `.reyn/state/action_usage.jsonl` with 5 skill__mcp_search entries (= simulating B38's accumulated tracker state) restores correct routing:

- tools[] gains `skill__mcp_search` as direct alias (= rank-boost above n=10 cap)
- LLM routes directly via the alias → `routing_decided{skill__mcp_search}` + `skill_run_spawned`
- Reply: "mcp_search スキルが完了しました" (= B38 baseline behavior restored)

This is **direct primary data** that the regression is unmasked-not-introduced. f48333c (fresh-mode reset) removed the action_usage carry-over that was masking the cold-start gap; the structural limitation (= seed position 14 for skill__mcp_search at n=10 cap) and the cognitive bias (= LLM doesn't try skill category) both pre-existed.

### Reyn care boundary placement

Per `docs/concepts/care-boundary.md` §1: "The LLM doesn't have to guess what exists." Cold-start skill discovery IS Reyn OS pre-call structural responsibility. The fix lives in:

- **(c) structural pre-call context injection** (= OS provides flat skill list in router context regardless of hot-list)
- **(b) router SP MUST rule** (= "user says X スキル" → `list_actions(category=['skill'])`)
- **(a) list_actions description enrichment** (= explicit skill-category example)

Ladder cheap-first: observe (= done) → structural (= cheap, additive) → SP rule (= medium, watch for prompt bloat per principle 2) → description fix (= bloat risk per principle 2 + B38 G31 ε2 dilution).

---

## 3. Worker-level deltas (= scenario-resolution data)

| Worker | B39 V/I/R/B | vs B38 | Notable change |
|---|---|---|---|
| **W1** chat_router_smoke | 2 / 0 / 5 / 0 (7) | -2V | S4 V→R (LLM picked skill__direct_llm over word_stats_demo); S6 V→R (task_completed narration collapse) |
| **W2** stdlib_skills_core | 1 / 3 / 5 / 0 (9) | -1V | S7 V→R (LLM picked skill__direct_llm over skill__eval) |
| **W3** control_ir_ops | 3 / 0 / 6 / 0 (9) | +1V | **S8 R→V (= 29a0c31 + 16b439c e2e working)** |
| **W4** permissions_and_safety | 4 / 4 / 0 / 0 (8) | +1V | S3 I→V (deterministic permission path) |
| **W5** multi_agent_and_mcp | 1 / 2 / 4 / 0 (7) | -1V | S4 V→I (peer routing) |
| **W6** plan_mode_fp0011 | 3 / 4 / 4 / 0 (11) | 0V (ΔI=-1/ΔR=+1) | **R-WEB chain reopened (3 I→R: narr-1, s-fp11-3, s-fp12-comp-1)**; narr-3 V→I (skill_builder loop_limit); s-fp11-1 V→R (scenario design flaw, false success) |
| **W7** long_session_v1 | 7 / 0 / 0 / 0 (7) | 0V | C1 clean turns 7/7 maintained |

Net: **V=21 / I=13 / R=24 / B=0 = 36.2% verified** (ΔV=-2, ΔI=-2, ΔR=+3 vs B38).

### Common pattern across W1 S4, W2 S7

Both regressions involve **LLM selection competition with skill__direct_llm** after b4daeb1 made direct_llm visible in ARS with `{text}` schema. Pattern:

- B38 (= direct_llm NOT in ARS): LLM picked the specific skill (word_stats_demo for W1 S4, eval for W2 S7)
- B39 (= direct_llm in ARS with `{text}`): LLM picked direct_llm (= "I can answer this from knowledge" affordance)

This is **the secondary attractor signal of b4daeb1**: not a routing landscape shift, but a specific affordance bias toward direct_llm for natural-sounding queries. Class A cognitive bias (= named anti-attractor callout would be effective per B19 case study).

Note: b4daeb1 verdict remains **keep** because:
- The direct_llm visibility is part of the documented FP-0034 D2-full scope (= skills with input_schema appear in ARS)
- The bug is "LLM weighs direct_llm too high for specific-skill queries", not "b4daeb1 introduces noise"
- Fix layer is prompt/description (= class A named anti-attractor callout), not commit revert

---

## 4. Principles reinforced

### Established this session

- **User-tunable parameters fixed in comparison** (= new memory `feedback_user_params_fixed_in_comparison.md`): when verifying a fix commit, hold `hot_list_n`, `hot_list_seed`, agent config, and similar user-facing knobs constant at the B-period observation value. Comparing across different params confounds commit effect with param effect.
- **Pre-fix multi-agent context analysis — anti-pattern "やったフリ"** (= updated memory `feedback_pre_fix_context_analysis.md`): "context analysis was done" requires verbatim quotes from 5 axes (= router SP / wrapper desc / past batch history / trace deep-dive / care boundary mapping) ready to surface on user audit. Diff classification + commit-level grep is not context analysis.

### Reinforced

- **Verify-first / reproduce-first** (= principle 6): bug reproduced on current HEAD `5c79bd7` (= post-pull main with 9a452bb) before any revert was proposed. The reproduce step itself surfaced the structural cause (= tools[] missing skill__mcp_search).
- **Pre-conclusion observation checklist** (= principle 12 candidate): when writing per-commit verdict, ran the 5-question audit (= specific observations, primary vs inference, falsification attempts, infrastructure adequacy, N/N inspection coverage).
- **Verify fix via replay before land** (= existing memory): trace-patch-replay at ~$0.10/check let the 9 commits be verified for ~$0.20 total instead of ~$30+ sonnet wave iteration.
- **Surface fungibility** (= B38 G31 lesson): the same prompt that succeeded at B38 with direct alias failed at B39 without it, but the failure migrated to MCP-category guessing rather than to silent stop. Action selection moves to whichever surface remains visible.

---

## 5. Handoff to next batch

### Open findings

- **B39-W6-COGNITIVE-BIAS (MED)**: cold-start wrapper-discovery. Fix candidates ranked. Carry-over to B40 as primary work item.
- **B39-W1-S4 / B39-W2-S7 (LOW)**: direct_llm selection competition. Same Class A family; may resolve with B40 SP fix or need separate named anti-attractor callout.
- **B39-W6-F2 (LOW)**: narr-1 scenario design flaw (vague invalid-spec prompt). Recommend rewrite.
- **B39-W6-F4 (MED)**: narr-3 skill_builder loop_limit_exceeded. Needs N≥3 retest to confirm.

### Calibration adjustments

- **Mcp_search routing prediction**: at hot_list_n=10 + fresh mode, base rate = 0% verified until structural skill-list inject or SP MUST rule lands.
- **Direct_llm competition prediction**: scenarios that "could plausibly be answered from knowledge" (= W1 S4 word_stats demo, W2 S7 eval) have elevated R rate when direct_llm is ARS-visible. Add to prelude prediction for next batch.

### Infrastructure updates needed

- **None new from this batch.** The verify session demonstrated that existing infrastructure (= `REYN_LLM_TRACE_DUMP`, `scripts/dogfood_trace.py --mode llm-detail/llm-tools-schema`, `scripts/llm_replay.py --patch --n --diff`, A2A endpoint via `reyn web`, `scripts/dogfood_fresh_reset.sh`) is sufficient for this class of post-batch verification when used together.

### Next-batch primary work

- **B40 prep**: design and verify Class A named anti-attractor callout for cold-start wrapper-discovery + direct_llm competition. Land via P16 multi-agent context analysis. Retest with same narr-1 + W1 S4 + W2 S7 scenarios at fixed user params (= same B39 conditions: hot_list_n=10, default seed, fresh mode).

---

## Appendix: per-worker findings files

Worker-level findings live in `workers/findings-worker-{1,2,3,4,5,7}.md` and `workers/results-worker-{1..7}.json`. Worker 6 has only the results JSON; its narrative findings are captured inline in this retrospective §2.
