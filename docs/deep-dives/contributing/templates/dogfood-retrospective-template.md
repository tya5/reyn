# Batch <N> — Retrospective

<!-- Copy this file to docs/deep-dives/journal/dogfood/<YYYY-MM-DD-batch-N-topic>/retrospective.md
     Fill in all <placeholder> values before committing.
     The retrospective is the batch's durable output — write it after the fix wave
     and retest complete, not before. -->

> **<FP-XXXX or topic description> milestone batch.**
> <N> scenarios × N=<n> = <total> isolated runs, <parallel execution description>.
> Result: **<verified>/<total> = <pct>% verified**, <attractor summary>, **Brier <float>**.
> <gate: production-grade phase N gate PASS / not yet reached>.

---

## 1. Expected vs actual

| Scenario | Baseline (or B<prev>) | Prediction | B<N> actual | Hit/Miss |
|----------|-----------------------|-----------|-------------|----------|
| <S1> | V=<n>/<n> | V≥<n>/<n> | **V=<n>/<n>** | ✅ hit |
| <S2> | V=<n>/<n> | V=<n>/<n> | V=<n>/<n> | ✅ hit |
| <S3> | V=<n>/<n> (= after fix) | V≥<n>/<n> | V=<n>/<n> | ⚠️ hit minimum |
| **Total** | | | | |

**Batch Brier**: <float> (= target <range> — <below/above/in> target)

<calibration interpretation: e.g. "prediction was conservative — 4 scenarios over-performed by 20pp">

---

## 2. Turning points

<!-- Document 2–4 turning points: events that changed the batch's direction
     or revealed something unexpected. Each turning point has a Lesson. -->

### TP1: <name — e.g. "Production-grade phase N milestone achieved">

<What happened and why it mattered. Reference data from findings.md where possible.>

**Lesson**: <Principle takeaway — can be a new principle or reinforcement of existing.>

---

### TP2: <name>

<What happened.>

**Lesson**: <Principle takeaway.>

---

### TP3: <name> (optional)

<What happened.>

**Lesson**: <Principle takeaway.>

---

## 3. Principles reinforced or newly established

<!-- List principles by name or number. Reference memory files where applicable. -->

### <Principle name or number> — <reinforced / newly established>

<One paragraph explaining what was confirmed or discovered in this batch.>

<!-- Reference: memory `feedback_<name>.md` -->

---

### <Principle name or number> — <reinforced / newly established>

<...>

---

## 4. Handoff to next batch

### Fix-wave candidates (entering B<N+1> fix wave)

<!-- HIGH and CRITICAL findings that need a fix before the next stability batch. -->

| Finding | Severity | Fix hypothesis |
|---------|----------|---------------|
| B<N>-<ID>-1 | HIGH | <brief fix plan> |

### Optional carry-over (LOW / MED, deferred)

<!-- Items deferred because they don't block the 80% gate or have low impact. -->

- **B<N>-<ID>-1** (LOW): <description> — <reason for deferral, priority justification>

### Next batch proposal

<!-- What should B<N+1> verify? Link to FP or scenario set. -->

- **Primary goal**: <e.g. "Phase 5 default flip + legacy cleanup regression sanity">
- **Scenario sets**: <set names or "TBD">
- **N**: <recommended repetitions>
- **Gate**: <target verified% at N=<n>>

---

## 5. Cost summary

| Item | Wall-clock | LLM cost (est) |
|------|-----------|---------------|
| <fix design + impl> | ~<N> min | $<float or 0> |
| <retest N=<n> (<k> parallel)> | ~<N> min | ~$<float> |
| <execution wave 1 (<k> parallel)> | ~<N> min | ~$<float> |
| <synthesis (findings + retrospective)> | ~<N> min | $<float or 0> |
| commit + push | ~<N> min | $0 |
| **Total** | **~<N> min** | **~$<float>** |

<!-- prelude target: <target>; actual: <actual>; <under/over> budget -->

---

## 6. Conclusion

Batch <N> achieved:

1. **<Milestone 1>** (= <key metric>)
2. **<Milestone 2>** (= <key metric>)
3. **<Principle established or reinforced>**

**<FP-XXXX> progression plan**:
- ✅ Phase <N> (<description>) landed
- ✅ <milestone> completed (B<N>)
- Next: <Phase N+1 description>
