# H7 action_usage reorder ablation

**Date**: 2026-05-17  
**Hypothesis**: `action_usage.jsonl` reorders hot-list aliases between turns within a
scenario based on this-session usage. After the first invocation, freq+recency promotes
the just-used action to the top, demoting seed entries the LLM hasn't called yet. If
true, freezing action_usage during a scenario should improve multi-turn scenarios'
verified rate.

---

## Observation: per-turn ranking shift (= primary data)

### Structural analysis (deterministic)

The scoring formula (`score = freq * (1 + 1/(1+age_days))`) promotes a freshly recorded
action to score ≥ 2.0 (freq=1, age_days≈0) while unrecorded seed items score 0.0 and
appear only as fill. This means any tool call recorded in Turn N promotes that action
to the front of the hot-list for Turn N+1.

Example from simulation (fresh session, n=16, DEFAULT_HOT_LIST_SEED=13 items):

**Turn 1 tools** (no prior records):
```
[file__read, file__list, reyn.source__list, web__search, web__fetch,
 memory.operation__remember_shared, skill__skill_builder, skill__skill_improver,
 skill__skill_importer, skill__mcp_search, skill__read_local_files,
 skill__index_docs, skill__eval]
```

**Turn 2 tools** (after Turn 1 recorded `reyn.source__list`):
```
[reyn.source__list, file__read, file__list, web__search, web__fetch,
 memory.operation__remember_shared, skill__skill_builder, skill__skill_improver,
 skill__skill_importer, skill__mcp_search, skill__read_local_files,
 skill__index_docs, skill__eval]
```

Order shift: `reyn.source__list` moved from position 3 → position 1.  
All 13 seed items remain present in both turns.

### Critical finding: n=16 vs 13 seeds — NO eviction occurs

`DEFAULT_HOT_LIST_SEED` has **13 items**. B30-NEW-1 bumped `hot_list_n` to **16**.
Since n=16 >= 13, even after freq-ranked items occupy slots, all 13 seed items remain
in the list as seed-fill. The H7 hypothesis (seed entries "demoted" = invisible to LLM)
does NOT apply in the B32 configuration.

Eviction would require > 3 distinct freq-ranked non-seed actions to be recorded
within a session. Simulated scenario with 4 non-seed freq items:
```
EVICTED: skill__skill_importer
EVICTED: skill__mcp_search
EVICTED: skill__read_local_files
```
This regime requires substantial cross-session pollution (prior action_usage.jsonl
accumulation). Per the wipe recipe (`dogfood-discipline.md` §6.5.6):
```bash
rm -f  .reyn/state/action_usage.jsonl
```
`action_usage.jsonl` IS wiped between dogfood scenarios. Cross-session pollution is
therefore reset at scenario boundaries.

### B32 primary data cross-check

B32 W7 results.json (`long_session_v1`):
- `multi_turn_survival`: 0 empty responses / 53 LLM calls across 7 multi-turn scenarios
- S1 regression ("Turns 2-5 all refused: same-description tools ambiguity") was
  attributed in B32 retrospective §1 to **B23-PRE-1 description ambiguity widened by
  NEW-2** (skill__eval joining the same description class as skill_builder/improver/importer)
  — not hot-list reordering.
- W7 `new1_verify`: 51/51 router turns show both `skill__index_docs` and `skill__eval`
  visible. No seed entry dropped out across all multi-turn turns.

No B32 finding cites action_usage reordering as a turn failure cause in any scenario.

---

## Patch summary

**No patch applied.**

Pre-patch discipline gate (memory `feedback_observe_before_speculate_llm.md`) required
verifying that ranking shift actually causes turn-N failures before patching. The
structural analysis shows:

1. Reordering occurs (order shifts, confirmed analytically).
2. Eviction does NOT occur under B32 configuration (n=16 >= 13 seeds).
3. B32 multi-turn failures attribute to B23-PRE-1 description ambiguity and wipe-recipe
   gaps (wal.jsonl + history.jsonl), not to action_usage reorder.

Patching the tracker to a no-op (= freezing it) would address an ordering shift that
does not cause eviction and has no observed causal link to B32 turn failures. The patch
was not applied.

---

## Per-scenario before/after

No frozen-tracker rerun was performed (patch gate: causal link not established in
primary data). The table below uses B32 baseline vs structural prediction.

| Scenario | Turn-N tools (B32) | Turn-N tools (frozen) | Verdict B32 | Verdict frozen (predicted) |
|---|---|---|---|---|
| long_session_v1 / scenario_1 | All 13 seeds visible across 5 turns (51/51 W7 check) | Identical (no eviction to fix) | Refuted (B23-PRE-1) | No change expected |
| long_session_v1 / scenario_5 | All 13 seeds visible | Identical | Verified (Q2 anomaly) | No change expected |
| long_session_v1 / scenario_7 | web__search called T1; promotes to pos 1 on T2+ | All seeds same order T2+ | Inconclusive | Marginal or no change |
| chat_router_smoke / multi_turn_pronoun | No tools called (inline replies) | Identical (nothing to freeze) | Verified | No change expected |
| plan_mode / plan_compare_two_concepts | Multi-step tool calls across turns | Minor order shift frozen | Verified | No change expected |

---

## Quantitative

- **N multi-turn scenarios re-run**: 0 (patch gate — no causal evidence; rerun not warranted)
- **N flipped to verified/inconclusive**: 0 (predicted)
- **Per-turn seed-demotion rate (observed in B32)**: 0% — no seed item was evicted in
  any B32 turn. W7 verified 51/51 turns both seed entries visible.
- **Within-session order-shift rate**: HIGH — any tool call reorders the hot-list for
  the next turn. This is structural and confirmed analytically.

**Conclusion: not-reorder-bound (flip_rate = 0/N, eviction rate = 0% under n=16 config)**

The flip_rate cannot be measured at 0/0 (no patch applied), but the structural analysis
establishes that:
- The ordering-shift effect exists but does not cause eviction under current config.
- B32 multi-turn failures have documented causal attributions that are not reorder-bound.
- The hypothesized mechanism (seed entries becoming invisible) does not operate when
  n=16 and seed has 13 items.

---

## Notes for future batches

### When H7 would become relevant

H7 becomes a live hypothesis if:
1. `hot_list_n` is reduced below 13 (the seed count), OR
2. The seed grows to > hot_list_n (e.g., new seed items added without bumping n), OR
3. Cross-session `action_usage.jsonl` is no longer wiped between scenarios.

### Adjacent finding: ordering-position affordance bias (Class B)

The ordering shift (reordering without eviction) is a real effect. Whether LLM tool
selection is biased by position in the tools[] array (= "first tool in array gets more
attention") is a **Class B affordance-bias hypothesis** (memory
`feedback_attractor_class_taxonomy.md`: HYPOTHESIS only, valid retest pending). This
is distinct from H7's eviction hypothesis and would require a separate positional-bias
ablation.

### Cross-session pollution risk (distinct from H7)

If `action_usage.jsonl` is NOT wiped (e.g., a dogfood worker that skips the wipe), then
cross-session freq data from prior runs pre-populates the tracker from Turn 1 of a new
session. With 4+ non-seed freq items accumulated, seed items at the tail (skill_importer,
mcp_search, read_local_files) would be evicted from Turn 1. This is the real eviction
risk, but it is a wipe-recipe gap issue, not a within-session reorder issue.

---

## Cross-reference

- `src/reyn/tools/action_usage_tracker.py` — scoring formula, `get_top_n`, `record`
- `src/reyn/chat/session.py:567-580` — tracker lifecycle (session-level singleton)
- `src/reyn/chat/router_loop.py:597` — `get_top_n()` called once per `run()` (= per turn)
- `src/reyn/chat/router_loop.py:860-863` — `record()` called within turn's tool-call loop
- `docs/deep-dives/contributing/dogfood-discipline.md` §6.5.6 — wipe recipe includes
  `action_usage.jsonl`
- B32 results-worker-7.json — W7 multi_turn_survival: 0 empty / 53 LLM calls
- B32 findings.md §4.4 — S1 regression = B23-PRE-1 description ambiguity (not reorder)
- B32 retrospective §1 — "B23-PRE-1 description ambiguity widened by NEW-2"
