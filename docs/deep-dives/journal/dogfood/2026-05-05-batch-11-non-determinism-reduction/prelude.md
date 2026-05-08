# Batch 11 (non-determinism reduction) — Prelude

> batch 10 で chain 完走 milestone を確認した上で、 残った probabilistic
> non-determinism (G12 25% / B9-NEW-3 50%) + B10-NEW-1 (path typo) を
> structural fix する batch。 theme は「**fix を積む段階 → stability を測る段階**」
> の transition (= batch 10 retro 教訓)。

## 当時の Reyn 状態

| 項目 | 値 |
|---|---|
| Date | 2026-05-05 |
| main HEAD (batch 開始時) | `1b5dae7` (= dogfood README + verify-first memory landing 後) |
| Test suite | 1005 passed / 2 xfailed |
| LiteLLM proxy | localhost:4000、 model `openai/gemini-2.5-flash-lite` |
| 観測 infra | 整備済 (= batch 7 で landing した 4 道具) |
| Chain 完走 milestone | 達成 (= batch 10 S1 で `reyn chat` 経由 6 phase 完走) |

## 課題

batch 10 で chain 完走 path が成立、 ただし:

| 課題 | 発生 rate | 影響 |
|---|---|---|
| **B10-NEW-1**: temp workspace path mismatch | typo level、 deterministic | chain は retry で継続するが eval が degraded data で実行、 結果不正確 |
| **G12 attractor**: `stop_with_must_rule` 確率的発火 | 25% / session | S5b で chain 起動失敗 (= describe_skill 後 empty stop) |
| **B9-NEW-3 / B10-NEW-2**: router text-reply non-determinism | 50% / session | S1 Run 1 で chain 起動失敗 (= router が text-reply で stop、 invoke_skill emit せず) |

= chain 完走 path は構造的に成立、 これらの probabilistic event を解消すると
**stable な chain 完走** に到達。

## Batch 11 の進め方

batch 9-10 で確立した verify-first + reproduce-first principle を運用適用:

### 5 step 構成

```
Step 1 (parallel structural fix): 3 sonnet 並列 dispatch
   R1 (B10-NEW-1, deterministic): eval.run_target の temp workspace path typo fix
      工数 ≈ 0.25 day、 文字列レベル fix
   R2 (G12 attractor, structural): MUST rule 構造分析 + attractor 真因 fix
      工数 ≈ 0.5 day、 N-shot llm_replay で 25% rate 確認 + 改善測定
   R3 (B9-NEW-3, structural): router text-reply 真因 diagnose + structural fix
      工数 ≈ 0.5 day、 verify-first で reproduce 確認後に fix design
   ↓ all landing
Step 2 (integration retest): S1 stability 測定 (= 5-shot 連続実行)
   sonnet 1 体、 worktree 隔離、 完走 rate を data 化
   ↓
Step 3 (findings + retro): batch 11 wrap
```

### 並列性検討

file overlap:
- R1 (path typo): `src/reyn/stdlib/skills/eval/` 系、 独立
- R2 (G12 attractor): `src/reyn/chat/router_system_prompt.py` 等
- R3 (B9-NEW-3): `src/reyn/chat/router_loop.py` 等、 R2 と隣接

R2 + R3 が router 周辺の隣接 file を編集する可能性。 並列 dispatch + cherry-pick
sequential で merge conflict は最小化、 conflict 発生時は手動 resolve。

## verify-first / reproduce-first 適用

各 fix dispatch sonnet に明示:
- **R1 (B10-NEW-1)**: deterministic typo なので reproduce 容易、 fix 後 N=1 dogfood で確認
- **R2 (G12)**: probabilistic なので **N-shot llm_replay** で pre-fix rate 測定 → fix design → post-fix rate 測定 (= 25% → ~5% 以下が target)
- **R3 (B9-NEW-3)**: probabilistic なので **N-shot dogfood** で pre-fix rate 測定 (50%) → 真因 diagnose → fix design → post-fix rate 測定 (= 50% → ~10% 以下が target)

## Prediction (= batch 10 calibration 教訓反映)

batch 10 retro で確立した「resolved-indirectly base rate 20-30%」 + 「累積 fix
verify scenario の verified 30-40%」 を適用、 さらに structural fix の base rate:

| Step | Top prediction | base rate 根拠 |
|---|---|---|
| R1 (B10-NEW-1) | verified 70% | deterministic typo level、 fix 直接、 verified 確率高 |
| R2 (G12) | verified 35% / inconclusive 25% / refuted 25% / blocked 15% | structural attractor fix、 weak LLM 環境で完全消失は困難、 「rate 改善」 の inconclusive が多い |
| R3 (B9-NEW-3) | verified 30% / inconclusive 30% / refuted 25% / blocked 15% | router-level structural fix、 真因 diagnosis 段階で「真の bug でない」 (= resolved-indirectly) 判明可能性 20% |
| Step 2 (integration 5-shot) | 60-70% rate (= 3-4/5 完走) / 100% rate 5-10% / 50% 以下 20% | 上記 fix が部分有効ならこの範囲、 全滅なら baseline 50% 維持 |

batch 10 で確認した **structural fix > layer fix > wording fix** の verified 確率
hierarchy を反映、 R1 (deterministic) は最高、 R2/R3 (probabilistic structural)
は中程度。

## 想定外シナリオ (= 計画外)

batch 10 と同様、 fix landing 後に新 blocker 露呈する可能性は high。 また
"resolved-indirectly" pattern も継続: G12 と B9-NEW-3 が共通 root cause を
持つ可能性 (= 両方とも「LLM が想定外の text-reply で stop」 系) があり、
1 fix で両方解消する可能性も。

これは batch 11 内で sub-wave fix dispatch せず **B11-NEW-N として
giveup-tracker に登録 + batch 12 候補に deferred** する方針 (= batch 9-10 と同じ
scope discipline)。

## Step 1 が部分 refuted の場合

R1 / R2 / R3 のいずれかが refuted:
- R1 refuted (deterministic なのに失敗): 異常事態、 batch 11 を一旦 stop して再 diagnose
- R2 / R3 refuted (probabilistic): inconclusive 扱い、 batch 12 で別 angle で追加 fix 試行

= probabilistic fix の refuted は normal outcome、 deterministic fix の refuted は
abnormal。

## 参照リンク

- batch 10 retro: `../2026-05-05-batch-10-residual-fix-wave/retrospective.md`
- batch 10 findings: `../2026-05-05-batch-10-residual-fix-wave/findings.md`
- B10-NEW-1 / B10-NEW-2 ctx: `../2026-05-05-batch-10-residual-fix-wave/findings/B10-S1-integration.md`
- G12 root cause analysis: `../2026-05-04-batch-7-post-infra-verify/findings/B7-G12-context-root-cause.md`
- giveup-tracker: `../giveup-tracker.md`
