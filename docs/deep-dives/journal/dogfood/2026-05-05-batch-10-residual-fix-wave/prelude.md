# Batch 10 (B9-NEW residual fix wave) — Prelude

> batch 9 で確定した 3 残 bug (B9-NEW-1/2/3) の fix landing + chain 完走候補探索。
> Step 1 (verify-first): B9-NEW-2 (= 直前 commit `8f3bccf`) の e2e 確認、 G17 wrong layer
> trap 再発防止。 Step 2-3: B9-NEW-1 + B9-NEW-3 fix + 統合 retest。

## 当時の Reyn 状態

| 項目 | 値 |
|---|---|
| Date | 2026-05-05 |
| main HEAD (batch 開始時) | `8f3bccf` (= B9-NEW-2 fix landed) |
| Test suite | 1005 passed / 2 xfailed |
| LiteLLM proxy | localhost:4000、 model `openai/gemini-2.5-flash-lite` |
| 観測 infra | 整備済 (= batch 7 で landing した 4 道具) |

## Batch 9 で残った課題

batch 9 retest sub-wave で確定した 3 新 bug:

| ID | 種別 | 状態 |
|---|---|---|
| **B9-NEW-1** | write_eval phase artifact validation 失敗 | 未着手、 batch 10 fix 候補 (HIGH) |
| **B9-NEW-2** | G17 wrong layer trap (= test 通過 + e2e 失敗) | **fix landed `8f3bccf`、 e2e verify 未達** |
| **B9-NEW-3** | router invoke duplication after run_skill failure | 未着手、 batch 10 fix 候補 (MED) |

## Batch 10 の進め方

batch 9 retro で確立した教訓 **「fix verify は per-fix Tier 3 e2e cross-check 必須」**
を運用適用。 fix wave に進む前に **直前 commit (B9-NEW-2) の e2e 確認** を Step 1
で先行実施。

### 4 step 構成

```
Step 1 (verify-first):  B9-NEW-2 e2e retest (= S5b dogfood)
   sonnet: ~5 分、 cost ~$0.001
   出力: B10-S5b-retest.md (= G17 wrong layer fix が真に effective か確認)
   ↓ verified なら →
Step 2 (parallel fix):  B9-NEW-1 + B9-NEW-3 sonnet 並列 dispatch
   B9-NEW-1: eval_builder write_eval schema or instruction fix (HIGH)
   B9-NEW-3: router invoke_skill dedupe 拡張 (MED)
   ↓
Step 3 (integration retest):  S1 + S5a + S5b dogfood (sonnet 1 体 sequential)
   ↓
Step 4 (wrap):  findings + retrospective
```

### verify-first の動機

batch 9 の G17 fix (= `d1f2d30`) は **5/5 Tier 2 test 通過したが e2e で wrong layer
trap**。 この前科を考慮すると、 B9-NEW-2 fix (`8f3bccf`) も e2e で確認しないと
「また wrong layer」 の risk。

batch 7 で確立した「観測駆動」 原則:
> 「LLM がおかしい」 と疑う前に、 LLM に渡したものを観測する道具を作る

の Tier 2 版:
> 「fix が landing した」 と宣言する前に、 fix が e2e で効くことを観測する

= verify-first を Step 1 として明示。

## Prediction (= batch 9 calibration 教訓反映)

batch 9 retro で確立した「fix の層で base rate を切り分け」 calibration を適用:

| Fix | 種別 | verified base rate |
|---|---|---|
| B9-NEW-2 e2e (Step 1) | layer fix (= resolver level handler) の retest | 50-60% verified (= test 通過済 + 構造的に明確な fix、 ただし wrong layer 前科で notch down) |
| B9-NEW-1 fix → S1 retest | structural fix candidate (= schema or instruction level) | 30-40% verified |
| B9-NEW-3 fix → S1 retest | structural fix (= router loop level) | 35-45% verified |
| 統合 retest (= S1 + S5a + S5b) | accumulated 4 fix の e2e | 20-30% verified、 50% blocked |

batch 9 で観察した「fix の層で verified 確率が桁違い」 教訓に従い、 **layer fix の
retest は前科ありで base rate 50%**、 structural fix は 35-45% に振る。

## 想定外シナリオ (= 計画外)

batch 9 と同様、 各 fix landing 後の retest で次 layer の new blocker 露呈する
可能性は high (= base rate 30-50%):

- B9-NEW-1 fix で write_eval 通過後、 次 phase (= analyze_skill 完了 → write_eval →
  ?) で別の schema/instruction gap
- B9-NEW-3 fix で router dedupe 拡張後、 chain 完走しても plan_improvements / apply_improvements / finalize の next layer で blocker

これらは batch 10 内で sub-wave fix dispatch せず **B10-NEW-N として giveup-tracker に
登録 + batch 11 候補に deferred** する方針 (= batch 9 と同じ scope discipline)。

## Step 1 が refuted の場合 (= 想定 fail-fast 分岐)

B9-NEW-2 e2e で「fix が e2e で効かない」 verdict が出た場合:
- Step 2 は **dispatch しない**
- 代わりに B9-NEW-2 fix の wrong layer 部分を再 diagnose
- batch 10 を再構成 (= 「verify-first principle が失敗例として functioned」)

これも観測駆動原則の Tier 2 版運用例として記録。

## 参照リンク

- batch 9 prelude: `../2026-05-05-batch-9-fix-wave/prelude.md`
- batch 9 findings: `../2026-05-05-batch-9-fix-wave/findings.md`
- batch 9 retrospective: `../2026-05-05-batch-9-fix-wave/retrospective.md`
- B9-NEW-1 (write_eval ctx): `../2026-05-05-batch-9-fix-wave/findings/B9-S1-retest.md`
- B9-NEW-2 (G17 wrong layer): `../2026-05-05-batch-9-fix-wave/findings/B9-S5b-retest.md`
- B9-NEW-2 fix commit: `8f3bccf`
- giveup-tracker: `../giveup-tracker.md`
