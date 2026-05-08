# B8 Aggregated — Batch 8 S1-S4 Single-Session Dogfood Summary

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `8e15019` |
| Session | Single session, input: `skill_improver で direct_llm を 1 回 review して改善案を出して` |
| Total cost | $0.000449 (16,530 tokens, 9 LLM calls) |
| Wall time | ~18s |

## Scenario Verdict Table

| Scenario | Topic | Verdict | Top Observation |
|---|---|---|---|
| S1 | chain completion (6 phases) | **blocked** | Chain stopped at `analyze_skill` (eval_builder sub-skill) due to permission_denied on `src/reyn/stdlib/skills/direct_llm/`. B8-NEW-1 remains the primary blocker. B8-NEW-2 (PureModeViolation) is now fixed, moving the failure surface earlier. |
| S2 | Option F empty stop UX | **blocked** | 0/9 empty stops. `router_empty_response` event never emitted. G12 truncation fix likely eliminated the trigger. Option F code path not exercised. |
| S3 | data.validation field (RETRO-H3) | **blocked** | `copy_to_work` never reached. Blocker is same as S1. H3 hypothesis unobservable. `analyze_skill` preprocessor did complete (B8-NEW-2 fix confirmed). |
| S4 | truncation fix — description ≤80 chars | **partially verified** | System prompt inline descriptions: all ≤83 chars (80+`...`). `skill_improver` 218→83 chars. Router tool schema descriptions NOT truncated (invoke_skill=349). 0/9 empty stops (vs ~50% batch 7). |

## Key Observations

### What improved since batch 7

1. **Router 1-turn direct invocation**: In B7-S1 the router needed 5 turns (list→list→describe→describe→invoke). In B8-S1, it jumped directly to `invoke_skill(name="skill_improver")` in 1 turn. No dot-notation hallucinate. Significant UX improvement.

2. **B8-NEW-2 confirmed fixed**: `analyze_skill_resolver.py` PureModeViolation is gone. Both preprocessor steps for `analyze_skill` completed successfully. This is the first e2e confirmation of this fix.

3. **G12 truncation working**: System prompt skill descriptions are truncated to ≤83 chars. The verbose `skill_improver` description (formerly 218 chars, implicated in Pattern A empty stops) is now 83 chars.

4. **0 empty stops**: First session with 0/9 empty stop rate vs batch 7's ~50% rate. Directionally positive; requires N≥10 replay for statistical confidence.

### What is still blocked

1. **B8-NEW-1 persists** (`analyze_skill` and `copy_to_work` cannot read `src/reyn/stdlib/skills/*/` files). This is the primary blocker for chain completion across S1/S2/S3.

2. **Option F unverifiable without empty stops**: Cannot confirm `router_empty_response` UX without triggering an empty stop. Requires synthetic injection via `llm_replay.py --patch`.

3. **H3 unresolvable**: `data.validation` transparency cannot be observed without reaching `copy_to_work` in LLM fallback mode.

## Prediction Calibration Delta

Predicted top outcomes vs actuals:

| Scenario | Top prediction | Actual | Hit? |
|---|---|---|---|
| S1 | 45% verified | blocked | miss (blocked was 20%) |
| S2 | 40% verified | blocked | miss (blocked was 25%, but scenarios.md explicitly called this as the canonical "fix works too well" outcome) |
| S3 | 40% blocked | blocked | hit |
| S4 | 70% verified | partially verified | near-hit |

Calibration: 1 full hit (S3), 1 near-hit (S4), 2 misses (S1, S2). Brier score improvement vs
batch 7 baseline (≈0.45) likely modest. The key calibration lesson is that `blocked` base rate
for chain-completion scenarios is higher than predicted — B8-NEW-1 is a deeper blocker than
expected, preventing even `analyze_skill` completion (not just `copy_to_work`).

## Blockers for batch 9

| Priority | Blocker | Impact |
|---|---|---|
| CRITICAL | B8-NEW-1: stdlib file.read permission in `run_skill` isolated workspace | Blocks S1/S2/S3 chain verification |
| HIGH | Option F synthetic trigger verification | S2 cannot be verified without engineering empty stop artificially |
| MED | H3 `copy_to_work` LLM fallback observation | S3 requires special setup (preprocessor bypass) |

## Surprising Finding

The most surprising finding is the **router 1-turn shortcut**: the router jumped directly to
`invoke_skill(name="skill_improver")` in a single LLM call without any `list_skills` or
`describe_skill` exploration. In B7-S1, this required 5 turns. The mechanism is not fully
explained — possibly the enum constraint in `invoke_skill` combined with system prompt skill
listing allows the LLM to pattern-match the user's request to the skill name without exploration.
This behavior reduces per-session cost dramatically (1 router call vs 5) and reduces the
opportunity window for G12 attractors.

## Cost

```
Total: $0.000449 | 16,530 tokens | 9 LLM calls (4 paying + 5 cached/no-charge)
  gemini-2.5-flash-lite: $0.000449  4,092 tokens  (2 calls)
  openai/gemini-2.5-flash-lite: $0.000000  12,438 tokens  (5 calls)
```

$0.000449 for a full skill_improver session attempt — extremely low. Even at batch scale
(10 sessions), cost would be $0.0045.
