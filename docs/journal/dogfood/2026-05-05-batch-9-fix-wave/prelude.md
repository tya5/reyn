# Batch 9 (B8-NEW fix wave) — Prelude

> batch 8 で発見された 4 新 bug (3 HIGH + 1 MED monitor) のうち、 HIGH 3 件
> (= G15 / G16 / G17) を fix dispatch する batch。 観測 batch でなく **fix batch**、
> 各 fix 後に per-fix retest で landing 効果を verify。

## 当時の Reyn 状態

| 項目 | 値 |
|---|---|
| Date | 2026-05-05 |
| main HEAD (batch 開始時) | `3708b55` (= batch 8 narrative + giveup-tracker G15-G18 整備後) |
| Test suite | 979 passed / 2 xfailed |
| LiteLLM proxy | localhost:4000、 model `openai/gemini-2.5-flash-lite` |
| 観測 infra | 整備済 (= batch 7 で landing した 4 道具) |

## Batch 8 で確定した課題

### 必修 fix (= batch 9 で landing)

| ID (giveup) | bug ref | 優先 | 内容 |
|---|---|---|---|
| **G15** | B8-NEW-3 | CRITICAL | eval_builder の skill.md permissions が stdlib path read に対応せず、 `analyze_skill` phase が `src/reyn/stdlib/skills/<target>/skill.md` 等を読めない。 chain 完走の primary blocker |
| **G16** | B8-NEW-5 | HIGH | router intent misrouting (= `eval を作って` → `eval` skill 誤誘導)。 enum fix で名前空間は守られるが semantic ambiguity 別問題 |
| **G17** | B8-NEW-6 | HIGH | `_extract_skill_name` (analyze_skill_resolver.py) が `artifact_type="unknown"` で `target_skill` field を直接参照しない。 構造データ invoke の e2e blocker |

### Defer (= 別 batch / monitor)

- **G18** (B8-NEW-4): router tool function description 非 truncate。 batch 8 で 0 empty stop なので urgency 低、 Pattern A 復活 trigger 待ち

## Batch 9 の進め方

batch 9 は **fix batch**。 構成:

1. **A1**: prelude (= 本 doc) + fix plan
2. **A2**: user review (= "進めて" で OK の場合は skip 可)
3. **A3**: 3 fix を並列 sonnet dispatch、 各 worktree で実装 + Tier 2 test 追加
4. **A4**: 全 fix landing 後、 fix 単位 retest (= S1/S5a/S5b の per-fix sub-wave verify)
5. **A5**: findings + retrospective

### Fix sub-wave 詳細

| Fix | scope | file 影響 | test | 並列性 |
|---|---|---|---|---|
| G15 (B8-NEW-3) | `src/reyn/stdlib/skills/eval_builder/skill.md` の `permissions:` block に stdlib path read 追加 | 1 file | +Tier 2 (= permission grant 確認 + e2e でも確認) | 並列 OK |
| G16 (B8-NEW-5) | `src/reyn/stdlib/skills/eval_builder/skill.md` の `description` + `when_to_use` wording fix (= eval skill との semantic distinction 明示) | 1 file (G15 と同 file、 異なる field) | +Tier 3 LLMReplay (= 自然言語 input で eval_builder 起動確認) | G15 と sequential 推奨 |
| G17 (B8-NEW-6) | `src/reyn/stdlib/skills/eval_builder/analyze_skill_resolver.py` の `_extract_skill_name` に unknown artifact_type + target_skill 直参照分岐追加 | 1 file (resolver) + 1 file (test) | +Tier 2 (= unknown type で target_skill が直接取得される確認) | 独立、 並列 OK |

= G15 と G16 が同 skill.md を編集するため sequential cherry-pick が安全、 G17 は完全独立。

実行順:
- **並列 dispatch**: G15 + G17 を並列 sonnet (worktree 隔離)
- **G15 landing 後**: G16 を main HEAD に対して dispatch (= G15 fix が含まれた skill.md を base に編集)

## Retest 設計

各 fix landing 後の retest:

| Fix landed | Retest scenario | 期待 |
|---|---|---|
| G15 only | S1 (chain 完走) | analyze_skill 通過 → copy_to_work 到達 (= B7 までで届いていた layer) |
| G15 + G17 | S5b (構造データ invoke) | eval_builder analyze_skill 完走 + eval.md 生成 |
| G15 + G16 | S5a (自然言語 invoke) | router が eval_builder を選択 (= eval skill 誤誘導消失) |
| G15 + G16 + G17 (= 全 landing) | S1 + S5a + S5b | chain 完走 candidates 探索 |

## Prediction (batch 8 calibration 教訓反映)

batch 8 retro で「累積 fix verify scenario の verified base rate を 20-30%、
blocked + refuted を 50% 以上」 という calibration 指針を確立。 batch 9 は **fix が
1 件ずつ landing する sub-wave 構成** なので scenario ごとに calibration を切り分け:

| Fix sub-wave retest | verified | blocked | refuted | inconclusive |
|---|---|---|---|---|
| G15 単独 → S1 retest | 25% | 50% | 15% | 10% |
| G15 + G17 → S5b retest | 35% | 35% | 20% | 10% |
| G15 + G16 → S5a retest | 30% | 25% | 35% | 10% |
| 全 fix → S1+S5 統合 retest | 15% | 50% | 25% | 10% |

= **「fix 1 件 = 1 layer 解消、 次 layer で blocked が >50% 確率で出る」** という
batch 8 で実証した構造的観察を reflect。 verified を高く予測しない。

## 想定外シナリオ (= 計画外）

batch 8 で出たような新 bug が batch 9 でも発見される可能性は high (= base rate 30-50%):
- G15 fix で analyze_skill 通過後、 copy_to_work で B8-NEW-1 とは別の permission gap が露呈
- G17 fix で `_extract_skill_name` 通過後、 別 phase で類似の artifact type assumption violation
- G16 wording fix で別の eval_builder/eval ambiguity 発生

これらは batch 9 内で sub-wave fix dispatch せず **B9-NEW-N として giveup-tracker に
登録 + batch 10 候補に deferred** する方針。 batch 9 は「batch 8 で確定した 3 fix の
landing + verify」 にスコープ限定。

## 参照リンク

- batch 8 prelude: `../2026-05-04-batch-8-cumulative-verify/prelude.md`
- batch 8 findings: `../2026-05-04-batch-8-cumulative-verify/findings.md`
- batch 8 retrospective: `../2026-05-04-batch-8-cumulative-verify/retrospective.md`
- giveup-tracker G15-G18: `../giveup-tracker.md`
- batch 8 S1 finding (G15 ctx): `../2026-05-04-batch-8-cumulative-verify/findings/B8-S1-chain-completion.md`
- batch 8 S5a finding (G16 ctx): `../2026-05-04-batch-8-cumulative-verify/findings/B8-S5a-eval-builder-natural.md`
- batch 8 S5b finding (G17 ctx): `../2026-05-04-batch-8-cumulative-verify/findings/B8-S5b-eval-builder-structured.md`
