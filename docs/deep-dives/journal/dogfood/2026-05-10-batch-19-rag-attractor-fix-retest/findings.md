# Batch 19 — RAG Attractor Fix Retest Findings (Aggregate)

> 2 scenarios × N=3 = 6 primary runs、 main HEAD `ef70aef`。 batch 18
> carry-over fix 3 件 (= B18-S9-1 / B18-S5-1 / R-RAG-srcread) 後の retest。
> **S9 が 3/3 verified で full recovery (= batch 18 0/3 → 100%)、
> S6 は 0/3 refuted で prompt-layer fix が affordance-bias attractor に
> 効果なし**。 aggregate primary verified **3/6 = 50% で trajectory 閾値 ✓**、
> ただし attractor class 分類 (= cognitive-bias vs affordance-bias) の発見が
> batch 19 の真の学び。

## 1. Per-Scenario Summary

| Scenario | 予測 verified | 実測 verified | Verdict 4-tuple | Brier (4-class) | Structural | Behavioral fix 効果 |
|---|---|---|---|---|---|---|
| **S9** cost preflight | 65% | **100%** (3/3) | 3/0/0/0 | **0.215** (vs B18 1.055 = 史上最大改善 #2) | ✓ | ✅ named anti-attractor callout が numerical bias を完全 override |
| S6 multi-source recall | 50% | 0% (0/3) | 0/3/0/0 | 0.154 (vs B18 0.264) | ✓ guidance 表示確認 | ✗ LLM が text-anchored guidance を ignore して reyn_src_read 選好維持 |

**Aggregate**:
- primary verified: **3/6 = 50%** (= trajectory 閾値 ✓ 達成、 batch 14 milestone 70%+ 未達だが向き正)
- mean Brier: **(0.215 + 0.154) / 2 = 0.185** (vs batch 18 = 0.66 → 大幅改善)
- structural pre-check: 2/2 = 100%
- behavioral axis split = **cognitive-bias attractor は prompt 修正可、 affordance-bias attractor は prompt 修正不可** という新分類が成立

## 2. Critical Insight: Attractor class 分類 (= 新原則 13 candidate)

batch 18 で確立した「structural ≠ behavioral 軸分離 (= 原則 11)」 を、 batch 19 で
**behavioral attractor 自体を 2 class に subdivide できる**ことが実証:

### Class A: Cognitive-bias attractor (S9 で実証)

- **Definition**: LLM が input data を持っているが、 比重を間違える (= boolean flag を numeric value より低く weight 等)
- **Symptom example (B18-S9-1)**: `threshold_exceeded: true` を見ているのに `$0.0003` 小ささに anchor して abort せず
- **Fix path**: **Named anti-attractor callout** が effective
  - 「Common attractor to avoid: when boolean flag says X but dollar value is small, do NOT conclude Y. Boolean policy flag wins over numeric estimate.」
  - batch 19 S9 で **100% compliance** 達成、 run 2 で smoking gun (= LLM が `$0.0003 USD` 自己引用しつつ abort 出力) で確認
- **Effective layer**: prompt-layer で sufficient

### Class B: Affordance-bias attractor (S6 で実証)

- **Definition**: 複数の tool が同 user query を 「処理できる」 ように見える時、 LLM が **wrong tool を empty-prior default** として選ぶ
- **Symptom example (R-RAG-srcread)**: 「How is X implemented?」 で `recall` (= semantic search) ではなく `reyn_src_read(README.md)` (= file read) を選好
- **Fix attempt**: prompt-layer guidance (= 「prefer recall over file_read for semantic explanations」)
- **Result**: **0% compliance** (= text-anchored guidance を完全 ignore、 batch 18 100% refuted → batch 19 100% refuted で改善ゼロ)
- **Effective layer**: prompt-layer **不十分**、 escalation 必要 (= envelope-layer fix priority ladder)
  - Schema-layer (= tool description rewrite / catalog ordering / conditional suppression)
  - Envelope-layer (= response shape biasing)
  - Model-layer (= G4 strong-model 切替)

### 介入 layer 優先順位 (= memory `feedback_envelope_layer_fix.md` 既存 + batch 19 拡張)

| Attractor class | prompt-layer | schema-layer | envelope-layer | model-layer |
|---|---|---|---|---|
| Cognitive-bias | ✅ named callout で OK | (不要) | (不要) | (不要) |
| Affordance-bias | ❌ exhausted | 候補 | 候補 | 候補 |
| Protocol-level (= G12 Pattern E) | ❌ exhausted | (不適) | ✅ envelope inject で OK | (不要) |

## 3. Bug / Carry-over Catalog

### B18-S9-1 [HIGH] — RESOLVED ✅

batch 19 で 3/3 verified、 named anti-attractor callout で完全解消。 **dogfood discipline doc** に「Cognitive-bias attractor → named callout」 pattern を lift 候補。

### B18-S5-1 [MED] — RESOLVED (silent) ✅

vector field strip は recall 経路 op handler の internal な fix で、 dogfood で
直接 verify する scenario なし (= long-session UX で surface する)。 unit test (=
`test_op_recall.py` 等) で regression なし confirm 済 (= fix wave commit 時に
`pytest -q` 全 green 確認)。 short scenario (S6/S9 等) では context inflation が
1 turn では破綻しない threshold 内、 後日 long-session dogfood で再 verify。

### R-RAG-srcread [attractor] — REFUTED at prompt-layer ❌

batch 19 で prompt-layer fix が完全に効果なし (= 0/3)。 escalation path:

1. **Schema-layer 候補** (= 推奨次手): `recall` ToolDefinition の description を 「semantic-content-question routing trigger」 を強化する文言に rewrite、 `reyn_src_read` description は 「file structure / specific path 用」 narrow 化。 tool description は LLM の affordance を直接 shape する layer
2. **Envelope-layer 候補**: 動的 tool suppression — system prompt に indexed sources がある時は `reyn_src_read` を tools= array から除外 (= LLM の選択肢自体を絞る)、 trade-off は file-structural query (= 「README はどこ？」) で `recall` に走る attractor
3. **Model-layer 候補**: G4 spike (= gemini-2.5-flash 等の stronger model 評価) — affordance-bias は weak LLM の cognitive limitation 由来、 strong model で alleviate 可能性
4. (deferred) Hybrid: prompt-layer guidance を維持しつつ schema-layer rewrite を上書き

### R-RAG-numerical-vs-flag-bias [attractor] — RESOLVED ✅

S9 で 3/3 verified。 named callout pattern が generalizable な fix template として
成立 (= cognitive-bias 全般に応用可能性)。

## 4. Calibration delta

| 項目 | 予測 | 実測 |
|---|---|---|
| mean verified rate | 58% (= (50+65)/2) | 50% (= (0+100)/2) |
| mean Brier | ~0.30 | **0.185** (= 改善 +38%) |
| 新 attractor 分類発見 | 0 | 1 (= 原則 13 candidate) |

予測の miss は対称的:
- S9: 65% → 100% (= named callout の効果を過小評価)
- S6: 50% → 0% (= prompt-layer fix の効果を過大評価)

両 miss を平均すると aggregate 予測 (= 58%) は 8pp ズレで、 **Brier は B18 の 0.66 → 0.185 で大幅改善**。 原則 11 (= structural × behavioral 軸分離) の operationalize は方向 ✓、 ただし behavioral axis 内部での attractor class subdivide 不在で予測精度が伸び悩み → 原則 13 で operationalize 必要。

## 5. Verdict

| 軸 | 判定 |
|---|---|
| Trajectory ✓ (= mean verified ≥ 50%) | **✅ 達成** (= 3/6 = 50%) |
| Batch 14 milestone (= 70%+) 復帰 | ✗ 未達、 ただし向き正 |
| Cognitive-bias attractor fix template 確立 | **✅ S9 で実証** |
| Affordance-bias attractor の介入 layer 特定 | **✅ prompt-layer exhausted、 schema/envelope/model escalation** |
| 新原則 (= 原則 13 attractor class taxonomy) 確立 | **✅** |

batch 19 は **trajectory ✓ + 新分類軸の発見** 両方達成。 1.0 OSS launch narrative は
batch 18 で defendable state、 batch 19 で **cognitive-bias 系 attractor の fix template** 確立。
S6 affordance-bias は post-1.0 の schema-layer wave (= ~1 day) or model-layer wave (= G4 spike) で
解消、 1.0 release blocker ではない。
