# Batch 14 (stability extension + meta hygiene) — Prelude

> batch 13 で達成した real milestone (= 80% complete) を **95%+ stability** に
> 引き上げる + M2 audit の wrong-layer fixture 残件を解消 + dogfood pre-approval
> pattern を doc 化する batch。 全 3 fix とも **🔵 不具合修正** で 仕様変更なし。

## 当時の Reyn 状態

| 項目 | 値 |
|---|---|
| Date | 2026-05-06 |
| main HEAD | `a6c2b75` |
| Test suite | 1010 passed / 2 xfailed |
| Real milestone | ✅ confirmed (= batch 13 N=5 で 4/5 = 80% complete) |
| Brier score (= calibration) | 0.20 (= 13 batch 中 best) |

## Batch 13 で残った課題 (= 全 production-impact 軽微)

| ID | 重要度 | 内容 | 種別予定 |
|---|---|---|---|
| **B13-NEW-1** | MED | `eval.run_target` が target skill copy 内 literal model string (`gpt-3.5-turbo`) を使う、 LiteLLM proxy が時々 reject (= S4 session 3 で abort 1 件) | 🔵 不具合修正 (= proxy model class 経由化、 意図と実装の乖離訂正) |
| **B12-NEW-2** | HIGH (test-only) | `tests/test_replay_skill_improver.py` の `_candidate_copy_to_work` が存在しない `work_config` schema 使用 (= wrong-layer trap) | 🔵 不具合修正 (test fixture を runtime 構造に合わせる、 production code 無変更) |
| **B12-NEW-3** | HIGH (test-only) | 同 file の `iteration_state.session` fixture が `_resolved_paths` field 欠落 | 🔵 同上 |

= **production user に影響する仕様変更はゼロ**、 全 fix が「documented design / runtime
invariant への復帰」 として進む。

## Batch 14 の進め方

batch 13 で確立した「**修正分類を明示**」 discipline + 「**verify-first / reproduce-first**」
原則を継続適用。

### Step 構成

```
Step 1 (parallel fix):
   R1: B13-NEW-1 fix (eval.run_target model class 経由化)
   R2: B12-NEW-2 + B12-NEW-3 wrong-layer fixture 修正 (= 同 file、 sonnet 1 体担当)
   R3: dogfood pre-approval pattern doc 化 (= reyn.local.yaml setup convention)
   ↓
Step 2: N=5 stability retest (= 95%+ complete rate target)
   ↓
Step 3: findings + retro
```

### 並列性

- R1: `src/reyn/stdlib/skills/eval/` 周辺
- R2: `tests/test_replay_skill_improver.py` のみ
- R3: `docs/` のみ (=概念 doc 追加 / 既存 doc 拡張)

= file overlap なし、 3 並列 background dispatch OK。

## 各 fix の詳細

### R1: B13-NEW-1 fix (= 🔵 不具合修正)

**現実装の問題**: `eval.run_target` phase が target skill (= 例: direct_llm) を実行する際、
target skill の skill.md / phase 設定にある **literal model string** (= `gpt-3.5-turbo` 等)
をそのまま LiteLLM に渡している。 結果 proxy 設定 (= reyn.yaml の `models:` mapping、
例: `light: openai/gemini-2.5-flash-lite`) を bypass してしまう。

**期待挙動 (= documented intent)**: target skill の model 指定は **model class** (= `light`
/ `standard` / `strong`) で、 reyn.yaml mapping 経由で実 model string に解決される。

**Fix**: target skill の model resolution path を proxy model class 経由に統一。 literal
model string を含む skill.md (= `direct_llm/skill.md` 等) も model class に書き換え。

= **意図と実装の乖離訂正**、 user 視点で proxy 透過性が確保される (= cost / model choice
の operator control 維持)。 仕様変更ではない。

### R2: B12-NEW-2 + B12-NEW-3 fixture fix (= 🔵 不具合修正)

**現状**: `tests/test_replay_skill_improver.py` の test fixture が runtime 構造と乖離:
- B12-NEW-2: `_candidate_copy_to_work` が存在しない `work_config` schema 使用
- B12-NEW-3: `iteration_state.session` fixture が `_resolved_paths` field 欠落

**Fix**: fixture を runtime artifact shape に修正 (= G17 wrong-layer trap pattern の
予防的修正)。

= production code / public API / artifact schema 無変更、 test 自己整合性のみ修正。
仕様変更ではない、 wrong-layer trap risk の解消。

### R3: dogfood pre-approval pattern doc 化 (= 🔵 doc 追加)

batch 13 で確立した dogfood 自動化 pattern (= `reyn.local.yaml` に layer 3 pre-approval
を temporary 追加 → run → revert) を **dogfood README + permission-model concept doc**
に追記。

= documented design 内の運用 convention 化、 仕様変更ではない。

## Prediction (= batch 13 calibration 教訓反映)

| Step | Top prediction | base rate 根拠 |
|---|---|---|
| R1 (B13-NEW-1) | verified 60-70% | structural fix、 ただし model resolution path 改修で意図せぬ cascade 可能性 |
| R2 (fixture fix) | verified 85-90% | test-only 修正、 deterministic |
| R3 (doc 化) | verified 95% | doc only、 quasi-deterministic |
| Step 2 (N=5) | 4-5/5 (80-100%): 50% / 3/5: 25% / inconclusive: 15% / 0-2/5: 10% | batch 13 baseline 80%、 R1 fix で improve 期待 |

Brier target: ≤ 0.25 (= batch 13 0.20 から微調整)

## 想定外シナリオ + fall-back

- **R1 fix で別 layer cascade**: model resolution 改修で別 path に regression、
  reproduce-first で確認後 fix 設計やり直し
- **Step 2 で 5/5 complete**: batch 14 で **production-grade phase 1 完了** declaration、
  phase 2 移行を batch 15 で検討
- **Step 2 で 3/5 以下**: B13-NEW-1 fix が effective でない / 別 blocker 露呈、
  batch 15 で再 diagnose

## 修正分類サマリ

| 修正 | 分類 |
|---|---|
| R1 (B13-NEW-1) | 🔵 不具合修正 (= 意図と実装の乖離訂正、 production user の proxy 透過性確保) |
| R2 (B12-NEW-2/3) | 🔵 不具合修正 (= test fixture を runtime 構造に合わせる、 production code 無変更) |
| R3 (doc 化) | 🔵 doc 追加 (= 既存 convention の文書化) |

= batch 14 は **仕様変更ゼロ batch**、 全 fix が production user 影響なし。

## 参照リンク

- batch 13 retro: `../2026-05-06-batch-13-revert-and-real-milestone/retrospective.md`
- batch 13 findings: `../2026-05-06-batch-13-revert-and-real-milestone/findings.md`
- M2 audit (= B12-NEW-2/3 source): `../2026-05-06-batch-12-real-milestone/findings/B12-M2-fixture-audit.md`
- B13-NEW-1 source: `../2026-05-06-batch-13-revert-and-real-milestone/findings/B13-S4-stability-5shot.md`
- documented permission model: `../../en/concepts/runtime/permission-model.md`
