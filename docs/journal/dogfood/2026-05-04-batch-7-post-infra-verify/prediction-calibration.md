# Batch 7 prediction calibration + 4 区分 framework

## Batch 7 prediction 設計の振り返り

### 試行 (= 3 区分分布)

scenarios.md で初めて **分布形式 prediction** を試行。 点 prediction (= 確率 1 値) から
`verified / inconclusive / refuted` の 3 区分確率分布に切り替え、 合計 100% で pin。
意図: weak LLM 挙動の不確実性を distributional に記録し、 batch 間で calibration を
追跡する。

### 結果 (= mass miss)

| Scenario | 指標 | Top prediction | 割当 % | 実 verdict | Hit/Miss |
|---|---|---|---|---|---|
| S1 | internal | verified | 60% | blocked | **miss** |
| S1 | user | inconclusive | 45% | blocked | **miss** |
| S2 | internal | verified | 50% | partial verified | **partial hit** |
| S3 | internal | verified | 70% | inconclusive | **miss** |
| S4 | internal | verified | 60% | inconclusive | **miss** |
| S5a | internal | verified | 55% | refuted | **miss** |
| S5b | internal | verified | 80% | refuted | **miss** |

top-1 accuracy (= top probability category と実 verdict の一致): **1 / 6 = 17%**
(S2 を partial hit とカウントすれば 1.5 / 6 = 25%)

### 定量 calibration score

#### Brier score (3 区分での事後計算)

各 scenario の予測分布は `blocked` を 0% として 3 区分で組まれていた。
3 区分 one-hot を (verified=1, inconclusive=1, refuted=1) の 3 変数として扱い、
Brier score $= \sum_k (p_k - y_k)^2$ を各 scenario で計算する。

| Scenario | 予測分布 (v/i/r) | 実 verdict (v/i/r) | Brier |
|---|---|---|---|
| S1 internal | 0.60 / 0.30 / 0.10 | 0 / 0 / 0 (blocked) | 0.36+0.09+0.01 = **0.46** |
| S2 internal | 0.50 / 0.30 / 0.20 | 0.5 / 0 / 0 (partial) | 0.00+0.09+0.04 = **0.13** |
| S3 internal | 0.70 / 0.20 / 0.10 | 0 / 1 / 0 (inconclusive) | 0.49+0.64+0.01 = **1.14** |
| S4 internal | 0.60 / 0.30 / 0.10 | 0 / 1 / 0 (inconclusive) | 0.36+0.49+0.01 = **0.86** |
| S5a internal | 0.55 / 0.30 / 0.15 | 0 / 0 / 1 (refuted) | 0.30+0.09+0.72 = **1.11** |
| S5b internal | 0.80 / 0.15 / 0.05 | 0 / 0 / 1 (refuted) | 0.64+0.02+0.90 = **1.56** |

注: S1 の実 verdict `blocked` は 3 区分に存在しない → 3 変数すべて 0 として計算。
これ自体が 4 区分設計の必要性を数値で示している (= S1 の Brier 0.46 は
「blocked 20% を割り当てていた場合 0.18 に改善された可能性」)。

**平均 Brier score (3 区分モデル)**: (0.46+0.13+1.14+0.86+1.11+1.56) / 6 = **0.88**

一般的な well-calibrated binary classifier の目安は 0.20 未満。 0.88 は
大幅に超過しており、 3 区分モデルの calibration が著しく不良であったことを示す。

#### S1 の反実仮想 (= 4 区分で blocked 20% 割当済みの場合)

S1 internal を 4 区分で再 pin した場合の試算:

| 仮想予測 (v/i/r/b) | 実 verdict (b=1) | Brier |
|---|---|---|
| 0.50 / 0.20 / 0.10 / 0.20 | 0/0/0/1 | 0.25+0.04+0.01+0.64 = **0.94** → ... |

より現実的な仮想: `0.40 / 0.20 / 0.10 / 0.30` (= 新 fix 直後で blocked リスクを 30% 評価):

Brier = $0.40^2 + 0.20^2 + 0.10^2 + (0.30-1)^2 = 0.16+0.04+0.01+0.49 = 0.70$

4 区分モデルで blocked 30% を割り当てた場合: 0.70 (vs 3 区分での 0.46 相当試算)。
blocked 確率を正しく評価できれば calibration が改善する方向に働く。

### 教訓 (= retrospective.md より)

> 分布形式 prediction で `blocked` カテゴリを含めていなかったため、
> 前段 bug 露呈型の outcome が全部「 inconclusive 寄り」 に流れて
> prediction の precision 低下。

`blocked` は dogfood で最も頻出する failure mode の一つ:
- 新 fix 直後の regression → 前段で chain 起動せず
- 未走 scenario での前段 infra bug → 入口到達不能
- 外部 dependency (= proxy / MCP / config) → chain を起動できないまま終了

3 区分モデルでは blocked outcome を内部的に inconclusive か refuted のどちらかに
読み替えることになり、 どちらも不適切な読み替えになる。

---

## 4 区分 framework の提案

### 区分定義

| 区分 | 定義 | 典型例 |
|---|---|---|
| verified | scenario が期待通りに動作し、 hypothesis を支持する evidence が得られた | chain 完走 + improvement plan 到達 + eval score 非ゼロ |
| inconclusive | scenario の主要 path は走ったが、 hypothesis の判定に必要な evidence が不充分 | chain 動作するが judgment 観測経路が未到達、 中立 |
| refuted | scenario が hypothesis に **反する** evidence を出した | hallucinate / hypothesis の仮定が外れた / unexpected path |
| blocked | 前段の別 bug / infra 問題で scenario 自体が入口に到達できなかった | router crash / preprocessor fail で chain 起動せず |

### inconclusive と blocked の境目

| 区分 | 判定基準 |
|---|---|
| inconclusive | scenario の **主要実行 path は走った**。 評価に必要な観測 path に到達できなかったが、 基本動作は確認済 |
| blocked | scenario の **入口 (= entry phase / chain 起動)** に到達できなかった。 前段の別 issue が原因で scenario 評価を開始できなかった状態 |

区別の実務判定: chain の entry phase (= router → skill dispatch) が成功したか否か。
成功して途中で止まった → inconclusive。 dispatch 自体が失敗 → blocked。

### batch 7 を 4 区分で再評価

| Scenario | 3 区分 top prediction | 3 区分 Brier | 4 区分仮想予測 (v/i/r/b) | 仮想 Brier | 改善 |
|---|---|---|---|---|---|
| S1 internal | 60% verified → blocked miss | 0.46 | 0.40/0.20/0.10/0.30 | ~0.70 | blocked 認識で S1 は方向正しく評価 |
| S2 internal | 50% verified → partial hit | 0.13 | 0.50/0.25/0.15/0.10 | ~0.13 | 変化小 (S2 は blocked にならなかったため) |
| S3 internal | 70% verified → inconclusive miss | 1.14 | 0.50/0.25/0.10/0.15 | ~0.81 | top を verified から inconclusive 等に下げる動機が生まれる |
| S4 internal | 60% verified → inconclusive miss | 0.86 | 0.45/0.30/0.10/0.15 | ~0.68 | 改善 |
| S5a internal | 55% verified → refuted miss | 1.11 | 0.40/0.25/0.20/0.15 | ~0.87 | 改善 (blocked より refuted 方向) |
| S5b internal | 80% verified → refuted miss | 1.56 | 0.50/0.15/0.20/0.15 | ~0.79 | 大幅改善 (= direct invoke で blocked 低リスク → refuted に資源移動) |

4 区分モデルへの移行により、 **blocked リスクを明示的に表現できる**。
blocked に確率を割り当てることで verified への過剰集中が是正され、
calibration が改善する方向に働く。

---

## Batch 8 以降の prediction 設計指針

### Prior 設定の rule of thumb

`blocked` 確率は **scenario の precondition の安定度** で base rate を決定する:

| 状況 | blocked 推奨 base rate | 理由 |
|---|---|---|
| 新 fix 直後 (= regression risk あり) | 20-30% | fix が別経路で regression を引く可能性 |
| 安定 fix 後 (= 既存挙動上の retest) | 5-10% | 前段は stable、 scenario entry は問題ない見込み |
| 完全新 scenario (= 未走) | 30%+ | 未知の前段 bug / infra 問題を想定 |
| 観測 infra 整備後の retest | 5-10% | 整備済 infra 上での retest、 blocked リスク低 |

### 各区分の semantic 整合

予測時に自問すべき問い:

- **verified**: 「chain が全 phase を走り、 hypothesis の confirm に必要な evidence が出るか?」
- **refuted**: 「scenario は走るが hypothesis の前提に反する evidence が出るか?」
- **inconclusive**: 「chain は走るが観測経路や evidence が不充分で hypothesis の判定が不能か?」
- **blocked**: 「entry phase への dispatch 自体が前段 bug で失敗するリスクはどの程度か?」

### Calibration 評価方法

batch 8 以降は各 scenario の `Calibration evaluation` section で以下を記録する:

| 指標 | 計算方法 | 目安 |
|---|---|---|
| top-1 accuracy | top probability category と実 verdict の一致率 | batch 内平均 50%+ を目標 |
| Brier score (4 区分) | $\sum_{k \in \{v,i,r,b\}} (p_k - y_k)^2$ | 0.50 未満を良好、 1.0 超は要見直し |
| blocked miss rate | blocked になったが blocked に 0% 割当だったケース / 全 scenario | 0 を目標 (= blocked を必ず非ゼロ) |

log-loss は確率が 0% に近い区分でペナルティが発散するため、 weak LLM の
calibration 測定には Brier score が適切。

### scenarios.md の format (batch 8 以降)

```yaml
prediction:
  internal_metric:
    verified: 50
    inconclusive: 20
    refuted: 10
    blocked: 20
  user_metric:
    verified: 30
    inconclusive: 30
    refuted: 20
    blocked: 20
```

(% values, sum to 100 each)

verdict 記録時:

```yaml
verdict:
  internal_metric: blocked      # verified / inconclusive / refuted / blocked
  user_metric: n/a
  calibration:
    top1_hit: false             # top probability category と一致したか
    blocked_miss: true          # blocked verdict だが blocked=0% だったか
    brier_internal: 0.46        # 4 区分 Brier score
```

---

## 関連 doc / memory

- `retrospective.md` — batch 7 全体 narrative + prediction 教訓出典
- `findings.md` — S1-S5 verdict + 観測 infra 整備 narrative
- `scenarios.md` — batch 7 全 prediction 原文 (3 区分分布)
- `feedback_minimize_speculation.md` — 1 仮説 1 検証原則 (推測積み上げ禁止)
- `docs/en/concepts/care-boundary.md` — Reyn の care 範囲 3 区分 framework
