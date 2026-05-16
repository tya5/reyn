# Batch <N> — Findings

<!-- Copy this file to docs/deep-dives/journal/dogfood/<YYYY-MM-DD-batch-N-topic>/findings.md
     Fill in all <placeholder> values before committing.
     Remove finding sections that are not needed (e.g. if no HIGH findings, delete that subsection).
     Severity levels: CRITICAL / HIGH / MED / LOW / INFO -->

> <One-line summary of batch purpose and headline result. E.g.:
> "FP-0036 chat router smoke — 7 scenarios × N=3 = 21 runs. Verified 18/21 = 85.7%.">

---

## 0. Run summary

| Item | Value |
|------|-------|
| Branch HEAD | `<commit_hash> <commit_message>` |
| Tests | <N> passed / <N> skipped / <N> xfailed |
| Total runs | **<N>** (= <scenarios> scenarios × N=<n>) |
| Wall-clock | ~<N> min total |
| LLM cost (est) | ~$<float> |
| Driver | `<script path or reyn dogfood run command>` |

---

## 1. Verdict matrix

| Scenario | V/I/R/B | Verified % | Status |
|----------|---------|-----------|--------|
| **<S1>** (<short description>) | <v>/<i>/<r>/<b> | <pct>% | ✅ |
| **<S2>** (<short description>) | <v>/<i>/<r>/<b> | <pct>% | ✅ |
| **<S3>** (<short description>) | <v>/<i>/<r>/<b> | <pct>% | ⚠️ (target met) |
| **Total** | **<v>/<i>/<r>/<b>** | **<pct>%** | **PASS / FAIL** |

**Production-grade gate**: ≥80% verified at N=<n> — **achieved / not achieved** across all <N> scenarios.

<!-- V = verified, I = inconclusive, R = refuted, B = blocked -->

---

## 2. Brier score

<!-- Include only if outcome_prediction was declared in scenario YAML. -->

| Scenario | Per-scenario mean Brier |
|----------|------------------------|
| <S1> (<outcomes>) | <float> |
| <S2> (<outcomes>) | <float> |
| **Batch mean** | **<float>** |

**Calibration trajectory**:

| Batch | Brier | Notes |
|-------|-------|-------|
| B<prev> | <float> | <context> |
| **B<N>** | **<float>** | **<context>** |

---

## 3. Findings

<!-- One subsection per finding. Order: CRITICAL → HIGH → MED → LOW → INFO.
     Use the finding ID format: B<N>-<SCENARIO_ID>-<seq> -->

### B<N>-MILESTONE-1 (INFO, success) — <milestone description>

<!-- Use INFO for success milestones, not bug reports. -->

**Severity**: INFO (= milestone achieved)

**Observation**:
- <specific observation 1>
- <specific observation 2>

**Carry-over**: none (= success milestone)

---

### B<N>-<S1>-1 (<SEVERITY>) — <short description of finding>

**Severity**: CRITICAL / HIGH / MED / LOW

**Observation**: <What was observed — be specific. Reference run number (R1, R2 ...) if relevant.>

**Pattern**: <If this is a recurrence of a known attractor or bug pattern, reference the prior batch.>
<!-- If new pattern: delete this line -->

**Risk / Impact**: <User-visible effect. If production-only or noop-specific, note that.>

**Carry-over**: <fix plan (e.g. "SP rule wording fix") or "deferred — priority LOW, does not impact 80% gate">

---

### B<N>-<S2>-1 (<SEVERITY>) — <short description>

**Severity**: CRITICAL / HIGH / MED / LOW

**Observation**: <...>

**Impact**: <...>

**Carry-over**: <...>

---

## 4. Attractor base rate matrix (= N=<total> total)

<!-- Include when N≥15 and multiple attractor types are observed. -->

| Type | Count | Rate | Severity |
|------|-------|------|----------|
| <attractor type 1> | <n>/<total> | <pct>% | <severity> |
| <attractor type 2> | <n>/<total> | <pct>% | <severity> |
| **Total attractor** | **<n>/<total>** | **<pct>%** | — |

---

## 5. Methodology notes

<!-- Optional: note anything notable about how this batch was run
     (e.g. new isolation pattern, new driver flag, parallel wave structure). -->

<notes or delete this section>
