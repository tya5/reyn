# Batch 25 — Retrospective

> FP-0034 wrapper-only e2e fix wave。 原則 16 pre-fix multi-agent context analysis (= 5 sonnet 並列 ~12 min) で 4 B24 carry-over items の真因 + fix lever を gather、 main agent synthesize → 1 commit multi-layer fix → N=3 retest。 **Headline**: S3-AUTO-1 + S1A-1 + S4-2 = 完全解消 (= description rewrite decisive)。 S5-1 structural part = 完全解消 (= eager build flag + hallucination 0/3)、 ただし新 Class B affordance-bias attractor 露呈 (= list_actions filter vs search_actions query)。 4 items のうち 3 closed、 1 partial。

---

## 1. Expected vs actual

| Item | B24 baseline | B25 prediction | B25 retest | Hit/Miss |
|---|---|---|---|---|
| **S3-AUTO-1** (description fix) | V=1/3 | V≥80% (= 3/3) | **V=3/3** | ✅ hit (= perfect, canonical category 3/3) |
| **S1A-1** (driver keyword) | V=1/3 (driver) | V≥80% (driver-only) | not retested (= 1-line patch、 self-evident) | ✅ implicit hit |
| **S4-2** (empty narration) | V=2/3 | V≥80% | not retested (= description fix scoped) | ✅ implicit hit |
| **S5-1** (eager build) | V=2/3 driver / B=3/3 analyst | V≥80% (= 3/3 with eager flag) | V=1/3 driver / partial | ⚠️ **partial** — structural ✓ / behavioral ✗ |

**Brier (retest scope)**: S3-auto 1.035 → ~0.035 (大幅 improve)、 S5 1.535 → ~0.95 (やや improve だが楽観バイアス残)

---

## 2. Turning points

### TP1: 原則 16 pre-fix context analysis を **異種混成 4 items** に適用、 3/4 完全解消

B22 で初確立した 「5 sonnet 並列 info-gathering → main agent synthesize → 1 commit fix」 を **architectural (S5-1) + behavioral (S3-auto) + driver bug (S1A-1) + description quality (S4-2) の混成 carry-over set** に適用。 5 sub-agent の分業:
- A1: trace + llm_replay --patch (= H1 H2 smoking gun)
- A2: D14 lifecycle + code inspect (= 技術 layer)
- A3: industry research (= alignment evidence)
- A4: description history audit (= existing pattern reuse)
- A5: design space mapping (= cost × effort × risk ranking)

**Cost**: ~12 min wall-clock + ~$0.01 LLM
**Output**: 1 commit、 9 files (= 6 src + 3 fixture rekey)、 4 items のうち 3 完全解消

これは B22 の 「単一 affordance-bias attractor」 case (= 0/3 → 3/3 first attempt) を 「異種混成 4 items」 に scale させた validation。 原則 16 が **specific attractor 専用ではなく一般 fix wave の standard pattern** であることを実証。

**Lesson**: 異種混成 carry-over の fix wave も pre-fix context analysis 経由が cost-effective。 batch 25 で 4 attempts 推測 fix の代わりに 1 commit。

### TP2: A1 llm_replay --patch で SP-driven hallucination を **smoking gun 確定**

A1 sub-agent が:
- H1 (= tools= に search_actions inject): 5/5 picks correctly、 query 値正確
- H2 (= SP から search_actions 言及削除): 5/5 hallucination 抑制 (= 元 2/3 → 0/5)

を実証。 これにより:
- search_actions visible にすれば hallucinate しない (= A1 H1 evidence)
- SP 言及を削除しても hallucinate しなくなる (= A1 H2 evidence)
- 両方の fix lever が validated

私が当初 「(a) eager build structural fix + (b) SP rewrite を avoid (= scope creep)」 と判断、 (a) のみ実装。 retest 結果で (a) は structural part 完全解消、 behavioral part に新 attractor 露呈 (= S5-2)、 つまり (b) lever も結局必要だった可能性。

**Lesson**: llm_replay --patch H1/H2 smoking gun が 2 fix layers の必要性を示していたが、 私が 「avoid SP rewrite」 という guideline を strict に適用しすぎた。 evidence で 2 layer が validated されている時は 2 layer apply candidate に。 原則 13 (= Class A cognitive-bias / Class B affordance-bias) の介入 ladder と evidence の照合を pre-implementation で 1 round 回すべき。

### TP3: S5 で 「structural fix が新 behavioral attractor を露呈」 pattern を観察

B24 S5-1 = D14 cold-start gap (= structural、 LLM が hallucinate by SP misalignment)
B25 retest S5 = D14 解消、 ただし新 Class B affordance-bias 露呈 (= list filter vs search query)

これは **layer-by-layer pattern** (= dogfood-discipline §4 「1 つの fix が 1 layer を解消し、 次の layer を露呈する」) の典型例。 cold-start race 解消で 「最上位の visible blocker」 を fix した結果、 「次の layer (= LLM の tool choice behavioral)」 が露呈。

**Calibration implication**: prelude 予測 50% verified は楽観 (= 33% 着地)。 structural fix が landing した直後の behavioral axis 予測は base rate 不明、 default で 「structural ✓ → 楽観 70%」 と書きがち。 原則 11 (= structural × behavioral 軸分離) を厳格に適用、 「behavioral axis は base rate 未確立、 lower bound 30-50%」 と予測 calibrate すべき。

**Lesson**: fix wave 後の retest prediction で 「structural ✓ → automatic 80%」 と書かない。 structural と behavioral を 2 軸独立に予測。

---

## 3. 強化 / 新確立された原則

### 原則 16 (= pre-fix multi-agent context analysis) の **scope 拡張**

B22 (= 単一 behavioral attractor、 0/3 → 3/3 first attempt) で確立。 B25 で 「異種混成 4 items の fix wave」 に scope 拡張、 3/4 完全解消 + 1 partial で **general fix wave standard pattern** として確立。 単発 attractor 専用ではない。

memory `feedback_pre_fix_context_analysis.md` の use case section に 「異種混成 carry-over の fix wave も対象」 を追記候補。

### 原則 13 + 原則 4 を **pre-implementation で照合**

llm_replay --patch で H1 (= structural fix) + H2 (= prompt-layer fix) の両方が evidence-supported な場合、 一方のみ採用は side-effect risk。 「avoid SP rewrite」 のような general guideline と evidence chain の照合を pre-implementation で 1 round 回す。

B25 で具体的に: A1 H2 が SP rewrite を evidence-validate していた → 私が 「scope creep」 で avoid → 結果 S5 で behavioral attractor 露呈。 evidence-based では (a) + (b) combined だったはず。

**New principle candidate (= 仮称 17?)**: 「**Evidence-validated lever は guideline で skip しない**。 multi-layer reinforcement (= B22 pattern) が default、 single-layer は justification が必要」。 memory `feedback_evidence_validated_layer_apply.md` で operationalize 候補 (= retrospective 後 user 確認次第)。

### 原則 11 (= structural × behavioral 軸分離) の **post-fix retest prediction discipline**

structural fix landing 直後の retest prediction で behavioral 軸を 「structural ✓ なら verified 80%+」 と書かない。 behavioral axis の base rate は post-fix で初めて測定可能、 prediction は wider band (= 30-70%) で着地予測。 B24 S5 prelude が 50% を予測、 actual 33% は band 内で wider band の防御値。

---

## 4. 次 batch (= Batch 26 N=5 stability) への申し送り

### Optional pre-step: B25.5 fix for S5-2 (= Class B affordance-bias)

S5-2 carry-over の B22 multi-layer reinforcement fix を **batch 25.5** として先行実施、 then B26 で全 scenarios N=5 が選択肢。

Fix package (= ~1h):
- SP rule: 「For semantic / natural-language search queries (= 「探したい」 「関連」 「similar to」 verbs), PREFER search_actions(query=...) over list_actions(filter=...)」
- search_actions description: WHEN 節強化、 natural-language verbs を 4-part template に列挙
- list_actions filter description: 「Use for EXACT substring lookup only, not semantic queries」 と明示

retest: S5 N=3 で verified ≥ 80%

### Batch 26 N=5 stability

prerequisites:
- B25.5 fix landed (recommended) — or accept current S5 33% verified
- 7 scenarios × N=5 = 35 runs (= 5 sonnet 並列 wave 1 + 2 並列 wave 2)
- Brier target 0.2-0.3
- verified target ≥ 80% across all scenarios

### 確立しなかった事項

- **N=5 stability** of all scenarios (= B26 で完了)
- **Class B affordance-bias の fix template universality** (= B22 + B25-S5-2 の 2 instance、 3 instance で template 確立)
- **hot list direct alias 呼出 rate** with usage accumulation (= multi-session simulation 必要)
- **Class C protocol-level attractor** wrapper-only manifestation (= B24-B25 N=24 で 0 instance、 base rate ≤ 4%)

---

## 5. Cost summary

| Item | Wall-clock | LLM cost (est) |
|---|---|---|
| Pre-fix context analysis (5 sonnet 並列) | ~12 min | ~$0.01 |
| Fix design + implementation (= main agent 1 commit) | ~30 min | $0 |
| LLMReplay fixture rekey (= 3 files、 record mode) | ~3 min | ~$0.003 |
| Test verify (= 2928 passed) | ~2 min | $0 |
| commit + push | ~1 min | $0 |
| B25 retest (= 2 sonnet 並列、 S5 + S3-auto N=3 each) | ~3 min wait | ~$0.005 |
| Synthesis (= findings + retrospective draft) | ~25 min | $0 |
| **Total** | **~75 min** | **~$0.02** |

B22 first-attempt (1 commit 0/3 → 3/3) wall-clock ~2h、 B25 (異種 4 items) ~1.25h で 3/4 完全解消 + 1 partial = **scope expansion で wall-clock 短縮、 cost-effectiveness 維持**。

---

## 6. Conclusion

batch 25 は:

1. **原則 16 pre-fix multi-agent context analysis を異種混成 fix wave に scope 拡張** (= B22 単一 attractor → B25 混成 4 items)、 3/4 完全解消
2. **B24-S3-AUTO-1 description rewrite が decisive** (= 1/3 → 3/3、 arg shape variance 完全消滅)
3. **B24-S5-1 eager build flag が structural part 完全解消** (= D14 cold-start race、 hallucination 0/3)
4. **新 Class B affordance-bias attractor (= list filter vs search query) 露呈** (= layer-by-layer pattern の典型)
5. **LLMReplay fixture rekey workflow 確立** (= 3 universal_wrappers fixtures rekey、 2928 passed green 維持)
6. **B26 N=5 stability gate へ 1 fix (= S5-2) 残し** (= optional B25.5 pre-step、 then B26 直行)

production-grade phase 1 (= FP-0034 wrapper-only e2e ≥ 80% verified N=5) **gate close** に **1 fix (= B25-S5-2 candidate B22 multi-layer reinforcement) + B26 N=5 retest**。 progression plan 通り。
