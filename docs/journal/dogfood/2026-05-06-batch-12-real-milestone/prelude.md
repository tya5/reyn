# Batch 12 (provisional → real milestone) — Prelude

> batch 11 で確定した「batch 10 milestone は N=1 lucky case」 を踏まえ、 N≥5
> measurement で **weak LLM 環境での真の stability ceiling** を data 化する batch。
> G4 spike は cost 観点 (10x increase) で deferred、 weak LLM 路線で押さえ込める
> 範囲を明確化。

## 当時の Reyn 状態

| 項目 | 値 |
|---|---|
| Date | 2026-05-06 |
| main HEAD (batch 開始時) | `2b7ec49` |
| Test suite | 1016 passed / 2 xfailed |
| LiteLLM proxy | localhost:4000、 model `openai/gemini-2.5-flash-lite` (weak、 G4 spike は deferred) |
| 観測 infra | 整備済 (= batch 7 で landing した 4 道具) |
| Strong model | `gemini-3.1-flash-lite-preview` proxy 追加済、 cost 10x で deferred |

## Batch 11 で確定した課題

- **B11-NEW-1** (CRITICAL): `copy_to_work` preprocessor step[1] `run_op (file)` が permission_denied、 G15 fix は preprocessor `run_op` 経路に効かず。 batch 11 5-shot で 2/2 partial sessions の真の dominant blocker
- **B11-NEW-2** (HIGH): R3 routing fix 60% rate 残存、 weak LLM の wording fix 限界の可能性
- **batch 10 milestone is provisional**: N=1 lucky case、 N≥5 measurement で再確定必要
- **Tier 2 fixture audit gap**: G17 wrong-layer trap が他 fix にも潜在する可能性、 systematic check 未実施

## Batch 12 の進め方

option 2 (= core fix + meta wave 並走) で進行:

```
Step 1 (CRITICAL):  B11-NEW-1 fix (sonnet R1) [structural permission path fix]
Step 2 (parallel):  B11-NEW-2 diagnose-only (sonnet R2) [N=10 reproducer + G4-trigger 判断]
Step 4a (parallel): batch 10 milestone hygiene (sonnet M1) [retrospective doc 訂正]
Step 4b (parallel): Tier 2 fixture audit (sonnet M2) [systematic wrong-layer trap 検出]
   ↓ Step 1 landing 後
Step 3 (PRIMARY):   N=5 stability retest (sonnet S3) [真の milestone 確定]
   ↓
Step 5: findings + retro
```

**並列性**:
- R1 / R2 / M1 / M2 は file overlap なし、 同時 background dispatch
- R1: `src/reyn/op_runtime/file/` + `src/reyn/preprocessor/runner.py` + `src/reyn/permissions/`
- R2: docs only (diagnose-only, no code change)
- M1: `docs/journal/dogfood/2026-05-04-batch-10-residual-fix-wave/*` doc only
- M2: `tests/` audit (code reading) → audit doc output

S3 は R1 完了 (= B11-NEW-1 fix landed) 後 sequential。

## Step 詳細

### Step 1: B11-NEW-1 fix (R1、 sonnet)

**Hypothesis** (verify-first 適用):
- (A) `run_op` が permission_resolver を持たない (= G15 fix が child resolver 伝播するが run_op まで届かない)
- (B) `run_op` の path resolution が異なる (= absolute vs relative で auto-approve scope 外)
- (C) startup_guard が `run_op` declaration を読まない (= skill.md frontmatter の宣言形式が異なる)

**Tier 2 + Tier 3 LLMReplay test** で fixture が runtime artifact 構造と一致することを cross-check (= batch 9 wrong-layer trap 教訓)。

### Step 2: B11-NEW-2 diagnose-only (R2、 sonnet)

**進め方**:
- N=10 reproducer for 60% text-reply rate
- Hypothesis verify (Available skills injection / Japanese routing example / weak LLM ceiling)
- **判断**: structural-fixable → batch 12 内で fix dispatch (= step 追加)、 weak LLM ceiling → **G4-trigger-required** classification + giveup-tracker tracking

= **diagnose-only**。 batch 11 R3 fix が wording layer で既に試行済み、 同 layer での更なる試行は bloat trap (G1)。

### Step 3: N=5 stability retest (S3、 sonnet)

**判定 framework**:
- complete: 6 phase 全完走 + improvement plan delivered
- partial: prepare 通過、 中間 phase で停止
- routing-fail: router text-reply / empty stop で skill 起動せず
- router-fail: router 起動後即失敗

**真の milestone 達成基準**: N=5 で **complete rate ≥ 60%** (= 3/5 以上)

### Step 4a: batch 10 milestone hygiene (M1、 sonnet)

- batch 10 retrospective.md の「Reyn 史上初 chain 完走 milestone」 claim に **provisional (N=1) 注記** 追加
- batch 10 findings.md の同様 claim も訂正
- 「真の milestone は batch 12 N=5 ≥60% で確定」 と forward reference

### Step 4b: Tier 2 fixture audit (M2、 sonnet)

- 既存 Tier 2 test の fixture が runtime artifact 構造と一致するか systematic check
- 重点 area: skill 系 (eval_builder / skill_improver / direct_llm)、 router 系
- 乖離発見時は B12-NEW-N として記録、 fix 判断は別 batch

## Prediction (= batch 11 calibration 教訓反映)

| Step | Top prediction | base rate 根拠 |
|---|---|---|
| Step 1 (B11-NEW-1) | verified 60-70% | structural deterministic fix、 root cause clear、 batch 11 教訓反映で base rate 控えめ |
| Step 2 (B11-NEW-2 diagnose) | structural-fixable 35% / G4-trigger 50% / inconclusive 15% | weak LLM の wording fix limit を batch 9-11 で実証、 G4-trigger 判断確率高 |
| Step 3 (N=5 retest) | 0/5: 10% / 1-2/5: 35% / **3/5: 25%** / 4-5/5: 15% / inconclusive: 15% | weak LLM ceiling estimate ~35-45% complete rate、 milestone 達成 (3/5+) は 40% 確率 |
| Step 4a (milestone hygiene) | verified 90% | doc-only、 quasi-deterministic |
| Step 4b (fixture audit) | verified 70% (B12-NEW-N 1-3 件発見想定) / inconclusive 20% / refuted 10% | systematic audit、 latent issue 発見見込み高 |

Brier target: ≤ 0.40 (batch 11 0.65 から復帰)

## 想定外シナリオ + fall-back

- **Step 1 で B11-NEW-1 fix refuted**: 異常事態、 batch 12 一旦 stop して再 diagnose
- **Step 3 で N=5 全 fail (0/5)**: 「**weak LLM ceiling は実は 0-20% complete**」 evidence、 G4 trigger 必須性 data 化、 batch 13 で G4 spike (cost 増加受容) 判断トリガー
- **Step 3 で N=5 全 complete (5/5)**: batch 10 milestone を真に達成、 「production-grade stability」 declaration

## Out-of-scope

- G4 spike (= `gemini-3.1-flash-lite-preview` 評価): user 判断で deferred (cost 10x)
- B11-NEW-2 fix dispatch (= diagnose-only)、 batch 13+ 候補
- Tier 2 fixture audit で見つかった B12-NEW-N の fix (= 別 batch)

## 参照リンク

- batch 11 retro: `../2026-05-05-batch-11-non-determinism-reduction/retrospective.md`
- batch 11 findings: `../2026-05-05-batch-11-non-determinism-reduction/findings.md`
- B11-NEW-1 ctx: `../2026-05-05-batch-11-non-determinism-reduction/findings/B11-step2-stability-5shot.md`
- giveup-tracker: `../giveup-tracker.md`
