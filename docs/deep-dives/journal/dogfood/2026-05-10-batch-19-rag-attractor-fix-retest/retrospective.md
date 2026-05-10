# Batch 19 — Retrospective

> 2 scenarios × N=3 = 6 primary runs。 S9 で **0/3 → 3/3 = 100% full recovery** + S6 で **0/3 → 0/3 prompt-layer fix exhausted**、 cognitive-bias / affordance-bias の attractor 2 class subdivide を実証。 batch 14 milestone への trajectory ≥50% ✓ + 新原則 13 (= attractor class taxonomy) 確立で 1.0 narrative 強化。

---

## 1. Expected vs actual

| 項目 | 予測 (prelude §6) | 実際 |
|---|---|---|
| mean verified rate | 58% (= (50+65)/2) | 50% (= (0+100)/2) |
| mean Brier (4-class) | ~0.30 | **0.185** (= 改善 +38% vs B18) |
| Trajectory ✓ (= ≥50%) | 達成見込み | **✅ 達成** |
| 新 attractor / 学び | 0-1 | **1 + 1 (= 原則 13 cognitive-vs-affordance + named callout pattern)** |

予測の対称的 miss (= S9 +35pp、 S6 -50pp) を平均すると aggregate 予測は 8pp ズレで、 結果は trajectory ✓。 ただし内訳が「両 scenario で 50-65% 圏内に converge」 と想定していた予測 logic が **誤り**だった = behavioral attractor 内部での class subdivide 不在で predict 精度が伸び悩む。 原則 13 で operationalize 必要。

---

## 2. Turning points

### TP1: S9 で named anti-attractor callout が 100% compliance を達成

batch 18 retrospective で「strategy.md prompt 強化 (= boolean flag explicit priority)」 を carry-over fix と提案、 batch 19 で landed (= `ef70aef` の `phases/strategy.md` 編集)。 想定は 65% verified だったが **actual 100%、 run 2 では LLM が `$0.0003 USD` を自己引用しつつ abort 出力**で smoking gun。 これは:

1. weak LLM (gemini-2.5-flash-lite) でも **named anti-attractor callout** に対する compliance は high
2. 「common attractor to avoid」 という meta-level guidance は LLM の reasoning trace に直接 lift される
3. cognitive-bias 系 attractor は prompt-layer で sufficient

batch 6-12 で 「individual MUST bullet rule」 が 60-80% compliance だったのに対し、 named callout pattern は **100% compliance** を出したのは **dogfood log 史上最大の prompt-layer compliance score**。

### TP2: S6 で prompt-layer guidance が完全 ineffective

batch 18 retrospective で「router system prompt に R-RAG-srcread guidance 追加」 を carry-over fix と提案、 batch 19 で landed (= `ef70aef` の `router_system_prompt.py` 編集、 「prefer recall over file_read for semantic explanations」)。 prompt の **structural 表示は 3/3 confirmed (= offset ~4150)**、 ただし LLM が **3/3 ignore して `reyn_src_read(README.md)` を選好維持**。

S9 success と対照的な S6 failure は **同 batch / 同 fix layer / 同 model** で起きたので、 違いは attractor class 自体にある。 これが 原則 13 の発見根拠。

### TP3: Brier 改善 (= prediction discipline 進化)

batch 18 mean Brier 0.66 → batch 19 mean Brier 0.185 (= 38% 改善)。 原則 11 (= structural × behavioral 軸分離) を prelude で operationalize した効果。 ただし behavioral 軸 50-65% 予測が両方とも off-target で、 「予測精度の伸び余地は behavioral 軸の attractor class subdivide にある」 ことを示唆。

---

## 3. 強化 / 新確立された原則

### 原則 13 (= 新): Behavioral attractor class taxonomy

batch 19 の S9 / S6 対照で実証された分類:

#### Class A: Cognitive-bias attractor

- **Definition**: LLM が input data を全部見ているが、 evidence の比重を間違える
- **Example**: B18-S9-1 (= boolean flag を numeric value より低く weight)
- **Fix layer**: **Prompt-layer で sufficient**
- **Fix pattern**: Named anti-attractor callout (= 「Common attractor to avoid: when X, do NOT Y. Z wins over W.」)
- **Compliance**: ~100% (= batch 19 S9)

#### Class B: Affordance-bias attractor

- **Definition**: 複数 tool が同 query を 「処理できる」 ように見える時、 LLM が wrong tool を **empty-prior default** として選ぶ
- **Example**: R-RAG-srcread (= recall vs reyn_src_read で reyn_src_read 選好)
- **Fix layer**: **Prompt-layer 不十分**、 schema / envelope / model layer escalation 必要
- **Fix pattern candidates**:
  - Schema-layer: tool description rewrite (= affordance signal を直接 shape)
  - Envelope-layer: 条件付き tool suppression (= 選択肢自体を絞る)
  - Model-layer: G4 strong-model 切替 (= cognitive limitation 解消)
- **Compliance with prompt-only fix**: 0% (= batch 19 S6)

#### Class C: Protocol-level attractor (= 既存、 reference)

- **Definition**: LLM API の protocol-level quirk (= G12 Pattern E post-tool empty-stop 等)
- **Fix layer**: Envelope-layer (= adapter / translator pattern)
- **Reference**: memory `feedback_envelope_layer_fix.md`

### 介入 layer 優先順位 ladder (= 原則 13 含めて update)

```
prompt-layer → schema-layer → envelope-layer → model-layer
  (cheap)       (semi-cheap)    (medium)         (expensive)
```

**Decision rule (= prelude / 設計 phase で適用)**:

1. attractor が cognitive-bias → prompt-layer named callout で 1 round 試行、 数字計測
2. attractor が affordance-bias → prompt-layer skip、 schema-layer から開始
3. attractor が protocol-level → envelope-layer 直行 (= prompt 試行は時間 waste)

class 識別は prior batch の symptom analysis から判定:
- LLM が wrong-evidence-weighting で reasoning trace に矛盾あり → cognitive-bias
- LLM が wrong-tool-selection で reasoning trace は coherent (= 単に default 選択) → affordance-bias
- LLM が API-level anomaly (= empty stop / format leak / role artifact) → protocol-level

### 原則 11 + 12 + 13 統合

batch 17 (= 教訓 10 structural pre-check) → batch 18 (= 原則 11 軸分離 + 原則 12 verdict false-attribution) → batch 19 (= 原則 13 attractor class taxonomy) で **dogfood prediction framework が 4 階層** に成熟:

1. **Structural axis** (= deterministic、 binary)
2. **Behavioral axis** (= stochastic、 N runs で base rate 測定)
   - **Class A cognitive-bias** (= prompt-layer fixable)
   - **Class B affordance-bias** (= schema/envelope/model escalation)
   - **Class C protocol-level** (= envelope-layer 直行)
3. **Verdict 区分 false-attribution discipline** (= refuted vs inconclusive vs blocked)
4. **Brier calibration** (= prediction logic 自己 audit)

---

## 4. 次 batch (= batch 20 候補) への申し送り

### Carry-over fix queue

| Item | Severity | 工数 | 着手 trigger |
|---|---|---|---|
| R-RAG-srcread schema-layer fix (= recall ToolDefinition description rewrite + reyn_src_read narrowing) | MED | ~0.5 day | batch 20 prep wave (= cheapest 次手) |
| R-RAG-srcread envelope-layer fix (= 動的 tool suppression、 indexed sources ある時は reyn_src_read 除外) | MED | ~1 day | schema-layer 不十分時 |
| G4 strong-model spike (= gemini-2.5-flash trial) | LARGE | ~0.5-1 day | affordance-bias model-layer escalation 候補、 ROI 評価 |
| R1 (= reyn web interactive=False) | UX | 1 day | release-readiness wave (= 別 lump) |
| B17-S5-1 ctrl42 | LOW | (deferred) | phase 2 model selection 連動 |

### Carry-over calibration

- prelude prediction で **attractor class identification** を明示 (= 原則 13)
- behavioral prediction を class A / B / C 別 base rate で組立
- `dogfood-discipline.md` に attractor class taxonomy + 介入 layer ladder section 追加 (= 別 wave)
- `feedback_named_callout_pattern.md` 候補 memory を作成

### Batch 20 trigger

- R-RAG-srcread schema-layer fix landed 後の S6 retest を batch 20 とする。
- 目標: S6 で N=3、 verified 60%+ (= schema-layer fix が affordance-bias を抑制するか測定)。
- 同時に S9 retest 不要 (= batch 19 で 100% verified、 regression リスクのみ確認なら batch 21 以降の post-1.0 wave で十分)。

---

## 5. Methodology の自己評価

### 良かった点

- **2 scenario 並列 sonnet (= ~12 min wall-clock)** で N=6 を完走、 4 scenario × N=3 の batch 18 (= ~30 min) と比べて scope 縮小に応じた dispatch 最小化
- **Prelude で 原則 11 operationalize** (= structural + behavioral 軸分離) で predict 構造改善、 Brier 0.66 → 0.185 (= 38% 改善)
- **対照的 result (= S9 100% / S6 0%)** が attractor class subdivide の発見を 1 batch で driver、 dogfood 設計で同 fix-layer / 同 model / 同 prompt-update timing を保ったのが信号 isolation に貢献
- **Smoking gun verification** (= S9 run 2 で LLM が `$0.0003 USD` 自己引用しつつ abort) は named callout の active override 効果を確証、 batch 6-12 の MUST rule よりも強力

### 改善余地

- **Prelude S6 prediction 50% は楽観バイアス** — batch 18 で R-RAG-srcread が 100% surface を観測していたのに、 「prompt fix で 50% に改善」 と推定したのは prior の behavioral base rate を override する勢いが強すぎた。 原則 13 確立後は 「affordance-bias は prompt-layer fix で 0-20% 程度」 が prior になる
- **S6 で fix attempt が 1 layer のみ** — schema-layer / envelope-layer / model-layer の同時 trial を batch 20 で並列に走らせて、 各 layer の attractor 抑制効果を比較する 設計が dogfood-as-experiment の真の活用
- **介入 layer ladder は memory に既存 (= envelope_layer_fix)** だが、 batch 19 まで prompt-layer 試行が default だった。 attractor class identification を prelude で明示するルールを batch 20 から強制

---

## 6. Conclusion

batch 19 は **「fix が効く scenario と効かない scenario が同 batch / 同 layer で対照的 result」** を初観測、 attractor class 自体を subdivide する **新原則 13** を確立。 dogfood discipline は

- batch 17: structural pre-check 必須
- batch 18: structural × behavioral 軸分離 (原則 11) + verdict false-attribution discipline (原則 12)
- batch 19: behavioral attractor class taxonomy (原則 13)

の 3 batch で **prediction framework 4 階層化** に到達。

S9 cognitive-bias は **prompt-layer fix template (= named anti-attractor callout)** が確立、 同 pattern は他 cognitive-bias 系 attractor (= 将来の新発見) にも transferable。 S6 affordance-bias は **prompt-layer 不十分** が batch 19 で実証、 batch 20 で schema-layer fix を 1st choice escalation で試行する。

「production grade landed」 narrative は batch 18 で release-blocker 解消 + batch 19 で **cognitive-bias 系 fix template + affordance-bias 系の正しい escalation path** が確立、 1.0 OSS launch narrative は **「framework foundation + headline scenario green + secondary scenarios with documented escalation paths」** で defendable。
