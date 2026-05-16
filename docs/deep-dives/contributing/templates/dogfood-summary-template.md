# Batch <N> — Summary

<!-- Copy this file to docs/deep-dives/journal/dogfood/<YYYY-MM-DD-batch-N-topic>/summary.md
     Fill in all <placeholder> values before committing. -->

**Date**: <YYYY-MM-DD>
**Batch ID**: B<N>
**Topic**: <short topic description>
**Framework version**: <FP-XXXX> `<commit_hash>`
**Scenario sets**: <set_name_1> (<count> scenarios) [+ <set_name_2> (<count> scenarios)]

---

## Headline metrics

| Metric | Value |
|--------|-------|
| Total runs | <count> (= <scenarios> scenarios × N=<n>) |
| Verified | <count> / <total> = <pct>% |
| Inconclusive | <count> |
| Refuted | <count> |
| Blocked | <count> |
| Brier score | <float> |
| Wall-clock | ~<N> min |
| LLM cost (est) | ~$<float> |
| Driver | `<script or reyn dogfood run command>` |

---

## Baseline comparison

| Metric | Baseline (B<prev_N>) | This batch (B<N>) | Delta |
|--------|---------------------|-------------------|-------|
| Verified % | <prev_pct>% | <pct>% | <delta>pp |
| Brier | <prev_brier> | <brier> | <delta> |
| Regressed scenarios | <count or —> | <count> (<scenario_id if any>) | — |

<!-- If this batch covers a different scenario set than the baseline, note that here.
     A verified-rate drop from set expansion is not a regression. -->

---

## Carry-over from B<prev_N>

<!-- List open items inherited from the previous batch. -->

- <B<prev_N>-finding-id>: <description> — <severity>, <fix plan or "deferred">

<!-- If no carry-over: -->
<!-- (none) -->

---

## Next steps

<!-- List immediate actions after this batch completes. -->

- [ ] File GitHub Issue for: <HIGH/CRITICAL finding descriptions>
- [ ] Post Discussion thread: `Batch <N> (<YYYY-MM-DD>): <topic> — <pct>% verified, <regressed> regressed`
- [ ] Fix-wave candidates: <list or "none">
- [ ] Next batch proposal: <brief description or "TBD">
