# B9 Aggregated — Post-Fix Retest Sub-Wave (G15/G16/G17 e2e verify)

| Field | Value |
|---|---|
| Date | 2026-05-05 |
| main HEAD | `330dd2a` |
| Batch | 9 (post-fix retest) |
| Fixes verified | G15 / G16 / G17 |
| Sessions | S1, S5a, S5b (sequential, separate .reyn/ flush) |
| Combined cost | $0.003705 (S1: $0.001891, S5a: $0.001295, S5b: $0.000519) |
| Combined LLM calls | ~60 (S1: 43, S5a: 15, S5b: 2) |

## Verdict summary

| Scenario | B8 verdict | B9 verdict | Fix result | Top finding |
|---|---|---|---|---|
| S1 chain completion | blocked | **inconclusive** | G15 ✅ effective | write_eval validation failure (B9-NEW-1) |
| S5a natural lang eval_builder | refuted | **refuted** | G16 ❌ insufficient | router still picks eval skill |
| S5b structured eval_builder | refuted | **refuted** | G17 ❌ wrong layer | artifact structure mismatch (B9-NEW-2) |

## Fix-by-fix assessment

### G15 (B8-NEW-3) — startup_guard non-interactive auto-approve + run_skill resolver propagation

**Status: EFFECTIVE ✅**

S1 observation confirms G15 works end-to-end:
- `analyze_skill` (eval_builder sub-skill) now reads stdlib paths without `permission_denied`
- File ops approved: `direct_llm/skill.md`, `direct_llm/phases/*.md`, `direct_llm/artifacts/*.yaml`
- Chain progresses past the B8 blocker to a new phase (`write_eval`)

G15 is the most impactful fix of batch 9: it unblocked the primary chain path that has
been blocked since batch 7.

### G16 (B8-NEW-5) — eval_builder description routing wording

**Status: INSUFFICIENT ❌**

S5a confirms G16 did not resolve the natural-language routing:
- Router still selects `eval` over `eval_builder` for input `direct_llm の eval を作って`
- `describe_skill(eval)` chosen after listing, `eval_builder` never described
- G16 wording ("Build" verb + when_not_to_use) not visible in truncated ~70-char listing
- Same routing failure pattern as B7-S5a and B8-S5a

G16 addressed the symptom at the wrong level (description text) vs the root cause
(routing signal for `eval` vs `eval_builder` in weak LLM with short listing).

### G17 (B8-NEW-6) — _extract_skill_name unknown artifact_type handling

**Status: WRONG LAYER ❌ → B9-NEW-2**

S5b attempt 3 confirms G17 reaches analyze_skill but still fails with same ValueError.

Root cause discovered in B9: G17 fix checks `"target_skill" in data` where
`data = artifact.get("data", {})`. But the OS passes the artifact **without** a `data`
wrapper at runtime — the stored artifact is `{"eval_spec": ..., "target_skill": "..."}`.
The fix must check `"target_skill" in artifact` (top-level) first.

Unit tests validated the fix with wrapped form `{"type": "unknown", "data": {...}}` but
the runtime produces unwrapped form. This is a test coverage gap revealing the wrong assumption.

## New bugs found in batch 9

### B9-NEW-1: write_eval Artifact data validation failure (HIGH)

**Triggered in**: S1 (chain completion)
**Phase**: eval_builder.write_eval
**Error**: "Phase 'write_eval' failed after 3 attempt(s): Artifact data validation failed for 'eval_spec_result'"
**Impact**: Blocks eval_builder chain completion even after analyze_skill succeeds
**Fix direction**: Inspect `eval_spec_result` schema vs write_eval LLM output; fix mismatch
in schema definition or phase instructions guiding artifact structure

### B9-NEW-2: G17 fix wrong layer — artifact has no data wrapper (HIGH)

**Triggered in**: S5b (structured eval_builder)
**Phase**: eval_builder.analyze_skill (preprocessor)
**Error**: Same ValueError as B8-NEW-6: "Cannot extract skill name from user_message text: ''"
**Root cause**: G17 fix checks `artifact.get("data", {})["target_skill"]` but OS passes
artifact without `data` key; `target_skill` is at artifact top level
**Fix direction**: Check `"target_skill" in artifact` before falling back to `data` dict;
add runtime integration test (not just unit test) that validates the actual OS artifact form

### B9-NEW-3: router invoke_skill duplication in long chains (MED)

**Triggered in**: S1 (T+141-158s: 3 invoke_skill calls from router)
**Phase**: router (post-run_skill failure propagation)
**Impact**: Multiple redundant skill_improver instances launched; wastes tokens
**Fix direction**: Similar to G3 (deduplication); investigate why router re-invokes after
run_skill failure is propagated back

## Chain progression delta (batch-over-batch)

| Batch | Deepest phase reached | Blocker |
|---|---|---|
| B7 (eeb8ed9) | copy_to_work (preprocessor step[1]) | stdlib path permission_denied |
| B8 (8e15019) | analyze_skill (eval_builder sub-skill) | permission_denied (moved earlier due to B8-NEW-2 fix) |
| B9 (330dd2a) | write_eval (eval_builder sub-skill) | Artifact data validation (B9-NEW-1) |

Each batch advances the chain by one phase layer. At the current rate, full chain completion
will require at minimum 1 more batch (write_eval fix + potential new layers).

## Calibration vs prelude prediction

| Scenario | Predicted | Actual |
|---|---|---|
| S1 retest | 25% verified / 50% blocked | inconclusive (partial verified + new blocker) |
| S5a retest | 30% verified / 35% refuted | refuted ✅ (within prediction) |
| S5b retest | 35% verified / 35% blocked | refuted (G17 wrong layer = refuted not blocked) |

Prelude calibration holds: "fix 1 件 = 1 layer 解消、次 layer で blocked" — confirmed for S1.
S5a and S5b refuted as predicted at moderate probability.

## Batch 10 candidates (priority order)

| Priority | ID | Fix | Scenario |
|---|---|---|---|
| CRITICAL | B9-NEW-2 | G17 fix: check top-level artifact.target_skill | S5b |
| HIGH | B9-NEW-1 | write_eval artifact schema fix (eval_spec_result) | S1 |
| HIGH | Ongoing | S5a routing: stronger signal (keyword list / routing_hint) | S5a |
| MED | B9-NEW-3 | router invoke duplication after run_skill failure | S1 |
| MED | G18 (deferred) | router tool description truncation | monitoring |
