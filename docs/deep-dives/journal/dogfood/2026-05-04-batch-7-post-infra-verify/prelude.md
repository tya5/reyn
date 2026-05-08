# Batch 7 (post-infra-fix verification) — Prelude

> 当時の Reyn 状態と batch 開始経緯。 batch 6 wave で landing した
> 6 commit の累積効果を chat 経由 e2e で verify するのが primary 目的。

## 当時の Reyn 状態

| 項目 | 値 |
|---|---|
| Date | 2026-05-04 |
| main HEAD (batch 開始時) | `578bb03` (= batch 6 narrative integration 後) |
| Test suite | 775 passed / 2 xfailed |
| LiteLLM proxy | localhost:4000、 model `openai/gemini-2.5-flash-lite` |
| 強モデル proxy 設定 | 未整備 (= G4 spike 引き続き blocked) |

## Batch 6 wave の到達点

batch 6 の dispatch wave で 6 commit が main に積まれた:

| Commit | 内容 | test 増分 |
|---|---|---|
| `e6de782` | eval_builder D1+D2+D3a fix (preprocessor 経由 OS path resolution) | +8 |
| `0fd6d0b` | skill_improver decide-turn instructions strengthening (B5-M2) | +4 |
| `9763ecf` | copy_to_work validation judgment Tier 3 LLMReplay test (B6-S1-M1 仮説 a) | +2 |
| `07e16ca` | B6-S1-M1 仮説 (a) dogfood retest doc (= inconclusive + 新 infra bug 2 件発見) | 0 |
| `07ee851` | reyn chat `--allow-untrusted-python` flag (infra fix #1) | +4 |
| `f666acb` | Workspace.glob_files() perm consultation (infra fix #2) | +4 |

(整理 commit `578bb03` は batch 6 narrative integration、 retrospective + findings + giveup-tracker 更新で test 増分なし)

合計 +22 test、 0 regression。

## なぜ batch 7 が必要か

batch 6 の dogfood retest (= B6-S1-M1 仮説 a) は **inconclusive** で終わった:
- copy_to_work 到達は確認できた (eval_builder fix の効果)
- しかし preprocessor で fail (= 2 件の infra bug が原因)、 LLM 未呼び出し

infra bug 2 件は同 session 内で fix されたが、 fix 後の e2e verify は **未実施**。
batch 7 は:

1. **6 commit の累積 e2e 効果** を chat 経由で primary verify
2. 副次的に MED 残件 (B5-M1 / B4-M1 / B6-S1-M1 実 LLM 確認) を観測
3. eval_builder fix の独立効果 (= union input 両形式) を verify

## 試行: 分布形式 prediction

batch 7 では prediction 形式を変更:
- 旧: 点 prediction (= 確率 1 値、 例 「70% で完走」)
- 新: 3 区分分布 (= 「verified / inconclusive / refuted」 の確率分布、 合計 100%)

例: `internal metric: 60% verified / 30% inconclusive / 10% refuted`

weak LLM 挙動の本質的不確実性を distributional に記録し、 batch 間の
prediction calibration を改善する試行。

## 当時の心境

batch 6 では「dogfood 中に意図しない infra bug が見つかる」 経験があり、
本来の仮説 (a) verification より副次的 finding の方が大きな成果になった。
batch 7 は「6 commit の累積効果」 を見届ける batch。 verify されれば「chat
経由で skill_improver が動く」 という Reyn の primary use case が完全に
動くようになった証拠。 inconclusive / refuted なら次の attractor (= G12
family) との地道な戦いが続く。

> dogfood は予測通りには進まないが、 だからこそ価値がある。

— assistant の internal state、 batch 7 開始直前
