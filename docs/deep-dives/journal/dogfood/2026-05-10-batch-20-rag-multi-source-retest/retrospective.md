# Batch 20 — Retrospective

> 1 scenario × N=3 = 3 runs。 batch 19 self-audit で 「affordance-bias
> hypothesis を valid scenario で再評価」 を carry-over として設定、 batch 20
> で synthetic sources による redesign を実施。 結果、 **scenario design に
> 2 度目の confound** (= prompt が concept-leaning) が判明、 hypothesis は
> 依然 pending。 **真の learn = 「pre-retrospective discipline が batch 19 で
> 確立されたが、 scenario design phase にも前倒し適用すべき」 = 原則 14
> candidate**。

---

## 1. Expected vs actual

| 項目 | 予測 (prelude §6) | 実際 |
|---|---|---|
| verified | 40% | **0% (0/3)** |
| refuted_b_a1 (= 1 source only) | 55% | **100% (3/3)** |
| refuted_b_a2 (= recall non-invoke) | 5% | 0% |
| Brier (5-class) | (= 想定外、 prelude 計算なし) | 0.0730 |
| Affordance-bias hypothesis 判定 | decisive (= 支持/棄却) | **pending、 confound** |

予測 0.0730 Brier (= 5-class 平均) は数値上 prediction が 「100% refuted_b_a1 に 55% 寄せた」 ので misalignment 小さい。 しかし **真の miss は predicted distribution の miss ではなく、 scenario design 前提の miss** (= 「multi-source は behavioral choice」 と暗黙仮定したが、 実際は scenario が structurally 1 source で十分)。

---

## 2. Turning points

### TP1: Pre-retrospective discipline の main agent self-execution

batch 19 self-audit で確立した 「retrospective 執筆前に LLM trace + tool description + scenario design 前提を必読」 を、 batch 20 で **私自身が main agent として first time 実行**。

実行した 3 step (= memory `feedback_pre_retrospective_discipline.md`):

1. ✓ LLM trace dump 全 run 読み (= python parse、 全 run 同 pattern 確認)
2. ✓ recall ToolDefinition description 再確認 (= multi-source active guidance が無いと判明)
3. ✓ scenario design 妥当性 audit (= 「prompt が structurally 何 source 必要か」 を明文化)

結果、 **agent から「verified=0、 refuted_b_a1=3」 報告を受け取った時点で、 retrospective 執筆前に「scenario design に 2 度目の confound あり」 を発見**。 これは batch 19 (= retrospective 執筆後に user 指摘で気づいた) と比べて、 **同 batch 内で self-correct できた discipline 進化**。

### TP2: Scenario design rigour の階層的 confound

batch 19 self-audit で「prompt と data source の semantic match」 を audit、 batch 20 で「scenario と reyn_src_read description の affordance match」 を audit、 だが **3 度目の confound** (= 「prompt が structurally 何 source 必要か」) を **prelude 執筆時に audit し損ねた**。

confound の階層:

| Level | Audit point | Status |
|---|---|---|
| 1 | data source content と prompt topic の semantic match | batch 19 で対応 |
| 2 | tool description の affordance signal vs prompt | batch 19 で対応 |
| 3 | **prompt が structurally 何 source 必要か** | batch 20 で発見、 batch 21 で対応必要 |
| 4 | (将来発見されるかも) | (open) |

**学び**: scenario design audit は 1 dimension では不十分、 **multi-dimensional audit checklist** が必要。 これを **原則 14 candidate** として lift。

### TP3: Affordance-bias hypothesis の decisive 判定 path 仕様確定

batch 20 で valid evidence は取れなかったが、 **真の判定に必要な scenario property** は明文化:

> 「片方の source 単独では物理的に答えられない aspect が prompt に含まれる」

具体例 (= batch 21 候補 prompt):

```
"Give me (a) the conceptual overview of QBP AND (b) the actual class names
 I'll need to import for integration."
```

- (a) = concept doc only
- (b) = code only (= class 名 `Entangler` etc. は code chunk にのみ存在)
- → multi-source picks が rational requirement

これで真の attractor 観測が可能 (= LLM が structural requirement あっても 1 source で satisfied するか)。

---

## 3. 強化 / 新確立された原則

### 原則 14 candidate (= scenario design audit checklist、 batch 20 lift)

dogfood prelude 執筆時に **以下 4 dimension を明文化**:

1. **Data semantic match**: indexed sources の content が prompt topic と match するか
2. **Tool affordance match**: 関連 tool の description が prompt と semantic conflict を起こさないか
3. **Structural source-count requirement**: prompt が structurally 何 source 必要か (= 1 で十分 / 2 必要 / 3+ 必要)
4. **Rational alternative paths**: 同 query に対する rational alternative routing (= file_read / web_search / direct text reply 等) が存在するか、 それぞれの affordance signal はどうか

prelude template に **「Scenario Design Audit」 section** として 4 row 強制。

### 原則 batch 19 (= pre-retrospective discipline) の operational confirm

batch 20 で main agent (= 私) が初実行、 結果 **同 batch 内で confound self-discover** に成功。 batch 19 lesson の operational lift が functional であることを実証。

### Pre-retrospective discipline の前倒し化 (= 原則 14 と統合)

- batch 19: retrospective 執筆前に discipline 実行
- batch 20: scenario design phase にも discipline 前倒し (= prelude 執筆時に audit checklist を実行)

これで dogfood agent (= sub-agent) も main agent (= 自分) も、 過剰一般化 trap を **1 phase 前倒しで防御**。

---

## 4. 次 batch (= batch 21 候補) への申し送り

### Affordance-bias hypothesis decisive 判定

| Path | 工数 | 判定基準 |
|---|---|---|
| **Batch 21 with structurally-multi-source prompt** | ~0.1 day prep + N=3 retest = ~0.5 day | verified ≥ 50% → hypothesis 棄却、 < 30% → 支持、 30-50% → 追加 N |

prompt 仕様確定済 (= 上記 §3 example)、 driver script は `dogfood_s6_b20_driver.py` の prompt 1 行差分のみで作成可能。 **user 投資判断**: affordance-bias hypothesis の decisive 判定は 1.0 release に対し non-blocking、 投機的 follow-up scope。

### 1.0 release scope への影響

batch 20 結論: **1.0 release blocker なし**。 batch 17/18/19 で確立した:

- ✓ Headline (S5) green (= production blocker 解消)
- ✓ Structural foundation 100% (= fix wave + embedding wiring)
- ✓ Cognitive-bias fix template (= S9 named anti-attractor callout)
- ✓ Pre-retrospective discipline (= self-audit infra)
- ✓ Scenario design audit checklist (= 原則 14 candidate)

これらが 1.0 OSS launch narrative の core asset、 affordance-bias の decisive 判定 (= batch 21+) は **post-1.0 fast-follow scope**。

---

## 5. Methodology の自己評価

### 良かった点

- **Pre-retrospective discipline を main agent が初実行**、 batch 19 lesson の operational lift が functional であることを実証
- **agent の self-diagnosis (= scenario flaw) を retrospective 執筆前に確認**、 同 batch 内で confound を self-discover
- **Affordance-bias hypothesis の decisive 判定 path 仕様確定** (= 「structurally-multi-source-requiring prompt」)、 batch 21 で実行可能 state
- **Synthetic source design の partial validity** (= reyn_src_read affordance conflict は確実に排除できた、 step 2 audit は機能した)

### 改善余地

- **Scenario design audit を 1 dimension 単位で進めた**: batch 19 で audit dimension 1+2 (= data semantic + tool affordance) を学び、 batch 20 で dimension 3 (= structural source-count requirement) を発見。 **multi-dimensional checklist を初回 retrospective から先取りする discipline** が必要 (= 原則 14)
- **Prelude prediction logic に 「scenario assumption」 を含めなかった**: 「multi-source picks rate 30-70%」 と behavioral 軸の予測は出したが、 「prompt が structurally 何 source 必要か」 の scenario assumption は明文化せず、 結果 prediction logic の前提が崩壊
- **3 batches 連続 scenario flaw**: batch 18 (= reyn_src_read affordance conflict) → batch 19 self-audit → batch 20 (= prompt concept-leaning confound) は agent design discipline の **学習 cost が予想以上**、 affordance-bias hypothesis decisive 判定に到達していない

---

## 6. Conclusion

batch 20 は **「scenario design audit は multi-dimensional rigour が必要」** という discipline 進化を確立、 **同 batch 内で main agent が self-correct できた first instance**、 これが本 batch の真の価値。

Affordance-bias hypothesis 自体は依然 pending、 decisive 判定には batch 21 で **structurally-multi-source-requiring prompt** で再測定が必要 (= 仕様確定済、 user 投資判断)。 1.0 release に対しては **non-blocking** (= S5 headline + structural foundation + S9 cognitive-bias fix で release-ready state 維持)。

dogfood discipline framework の進化:

- batch 17: structural pre-check 必須
- batch 18: structural × behavioral 軸分離 (原則 11) + verdict false-attribution discipline (原則 12)
- batch 19: cognitive-bias fix template (= named callout) + pre-retrospective discipline (= 原則 batch 19)
- batch 20: **scenario design audit checklist の multi-dimensional 化** (= 原則 14 candidate) + **pre-retrospective discipline の prelude phase 前倒し**

「production grade narrative の sober discipline」 は batch 17 retrospective 末尾の宣言、 batch 18-20 で **agent self-discipline の continuous evolution** という形で具体化された。 1.0 OSS launch narrative の core asset に **「dogfood discipline 自体の continuous improvement infra」** を加えて、 「framework foundation + headline scenario green + cognitive-bias fix template + scenario design rigour」 として defendable。
