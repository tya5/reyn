# Batch 8 (cumulative fix verification) — Findings

> 5 scenario (S1-S4 単一 session + S5 dual-input) の e2e 累積 verify。
> 観測 batch (= fix dispatch しない方針) で実行。 期待した「累積 fix の e2e 完走」 は
> 達成せず、 代わりに **新 blocker 4 件発見** (B8-NEW-3 / 4 / 5 / 6) と
> **想定外の router 効率改善 + B8-NEW-2 fix 確認** が headline 成果。

## Summary table

### Scenario verdicts (= scenarios.md S1-S5)

| Scenario | 種別 | Verdict | 主要発見 |
|---|---|---|---|
| [S1](findings/B8-S1-chain-completion.md) | 8 commit 統合効果 verify (primary) | **blocked** | chain 停止地点が `copy_to_work` (B7) → `analyze_skill` (B8) に **earlier shift**。 B8-NEW-2 fix 後に露呈した B8-NEW-3 (= eval_builder の stdlib 読み権限不足) が新 blocker |
| [S2](findings/B8-S2-option-f-empty-stop.md) | Option F 実 LLM 観測 | **blocked** | 0/9 empty stop (vs B7 50%)。 G12 truncation fix が trigger 自体を消失させた可能性、 Option F UX 未到達 |
| [S3](findings/B8-S3-data-validation-field.md) | B7-RETRO-H3 unblock | **blocked** | `copy_to_work` 未到達 (S1 と同 blocker)、 H3 観測経路は依然不可 |
| [S4](findings/B8-S4-truncation-effect.md) | G12 truncation fix 効果 | **partially verified** | system prompt skill 一覧は ≤83 chars 確認 (skill_improver 218→83)。 ただし router tool function descriptions (`invoke_skill` = 349 chars) は **非 truncate** = B8-NEW-4 |
| [S5a](findings/B8-S5a-eval-builder-natural.md) | eval_builder 自然言語 invoke | **refuted** | router enum fix は dot-notation hallucinate を排除 ✅。 ただし `eval を作って` → `eval` skill 誤誘導という **新 hallucinate variant** = B8-NEW-5 |
| [S5b](findings/B8-S5b-eval-builder-structured.md) | eval_builder 構造データ invoke | **refuted** | preprocessor anyOf fix (`3cbe983`) は確認 ✅。 ただし `_extract_skill_name` が `artifact_type="unknown"` を扱えず ValueError = B8-NEW-6 |

### 検出した新 bug (= batch 9 fix 候補)

| ID | 重要度 | 場所 | 内容 |
|---|---|---|---|
| **B8-NEW-3** | HIGH | `eval_builder` permissions | `analyze_skill` phase が `src/reyn/stdlib/skills/<target>/skill.md` 等を read できない。 batch 7 の B8-NEW-1 fix (`f229f6c`) は skill_improver のみ対象、 eval_builder の `run_skill` isolated workspace に同等 perm 付与なし。 chain 完走の primary blocker |
| **B8-NEW-4** | MED | `router_tools.py` | tool function descriptions (e.g. `invoke_skill` = 349 chars) は `MAX_DESC_LEN_FOR_LISTING` の対象外。 system prompt 経路は truncate されているが tool schema 経路は verbose のまま。 Pattern A 復活のリスク (現状 0/9 empty stop なので低優先) |
| **B8-NEW-5** | HIGH | `eval_builder` `when_to_use` or routing system | `eval を作って` 等の自然言語で router が `eval_builder` でなく `eval` skill (run/evaluate) に誤誘導。 dot-notation hallucinate (B7) は消えたが新 variant 出現。 既存 skill が誤起動して silently wrong 動作 |
| **B8-NEW-6** | HIGH | `analyze_skill_resolver.py:_extract_skill_name` | input dict に `type` field なしで `target_skill` だけある場合、 artifact_type="unknown" 経路に落ちて user_message fallback regex で ValueError。 構造データ直接 invoke の e2e blocker |

### 確認できた fix の効果 (= batch 7 wave の partial verification)

| Fix | Commit | 確認内容 |
|---|---|---|
| router enum fix | `9ee6ae1` | dot-notation hallucinate (`eval_builder.eval_md`) 完全消失 ✅ (S5a/S5b 両方で確認) |
| preprocessor anyOf fix | `3cbe983` | eval_builder の compile-time blocking 解消 ✅ (S5b で skill 起動 + analyze_skill 到達確認) |
| B8-NEW-2 fix (PureModeViolation) | `ed9de6c` | `analyze_skill_resolver.py` PureModeViolation が消失、 preprocessor 2 step 完走 ✅ (S1 で確認、 e2e 初確認) |
| G12 truncation fix | `cdbd853` | system prompt の skill description が ≤83 chars (= 80 + `...`) 確認 ✅ (skill_improver 218→83 chars、 -62%) |
| Option F (empty stop UX) | `48125ab` + `0a274fd` | **未確認** (= 0/9 empty stop で trigger 不在)、 batch 9 では `--patch` で synthetic injection 必要 |

### Cost summary

| Session | Tokens | Cost USD |
|---|---|---|
| S1-S4 (single session) | 16,530 | $0.000449 |
| S5a | 32,287 | $0.001246 |
| S5b | 4,089 | $0.000458 |
| **Total** | 52,906 | **$0.002153** |

= 6 scenario の dogfood で 1 セント未満。 weak LLM (gemini-2.5-flash-lite) の cost 効率は引き続き極めて良好。

## Round 別 narrative

### Round 1: scenarios.md A1 + A2 review

scenarios.md は batch 7 retro 教訓 (= 4 区分 prediction) を反映、 `blocked` を
chain 系 20-30% / 単独 skill 系 10-20% に base rate 設定。 user 介入無しで
A2 → A3 へ。

### Round 2: A3 並列実行 (sonnet × 2、 worktree 隔離)

- Sonnet A: S1-S4 single session、 input 共通 `skill_improver で direct_llm を 1 回 review して改善案を出して`
- Sonnet B: S5 dual session、 input 1 (`direct_llm の eval を作って`) + input 2 (`eval_builder で direct_llm を analyze して、 target_skill=direct_llm`)

実行時間: 各 sonnet ~10 分、 並列で ~10 分で完了。 LiteLLM proxy で衝突無し。

### Round 3: 観測結果の paradigm shift

batch 8 は「累積 fix の e2e 完走 verify」 を primary 期待としていたが、 観測結果は:

- S1 chain は **B7 より earlier に止まった** (= B8-NEW-2 fix で `analyze_skill` が
  動くようになり、 その先で B8-NEW-3 が露呈)
- S5a の hallucinate は **形が変わったが消失せず** (= dot-notation は消えたが intent misrouting に変容)
- S5b は **anyOf fix で skill 起動できるようになったが新 blocker 露呈**

これは batch 7 retro で学んだ教訓 (= **「fix 1 件 landing で chain が完走する」 は典型的に
楽観すぎ、 各 fix が 1 layer の blocker を unblock するだけ**) の再確認。

### Round 4: 観測 infra の reliable 利用

batch 7 で整備した 4 道具が今回も活躍:
- `dogfood_trace --mode chain/events/cost` で per-scenario 観測
- `detect_attractor.py` で attractor 自動検出 (S5a で 1 件検出)
- `REYN_LLM_TRACE_DUMP` の per-session 別 path 切替 (S5a/S5b で別 dump)

道具なしでは「LLM が何故 `eval` skill を選んだか」 「`compute_paths` 失敗の payload は何か」
等の詳細観測が成立せず、 仮説スタック化していたはず。 batch 7 で投資した観測 infra の
ROI が batch 8 で実際に発揮された。

## Prediction calibration

batch 7 retrospective で learnings から 4 区分 (verified / inconclusive /
refuted / blocked) に拡張した prediction を試行。 結果:

| Scenario | Top prediction | Actual verdict | Hit / Miss |
|---|---|---|---|
| S1 | 45% verified | blocked | miss (blocked = 20%) |
| S2 | 40% verified | blocked | miss (blocked = 25%) |
| S3 | 40% blocked | blocked | **hit** |
| S4 | 70% verified | partially verified (= inconclusive) | near-hit |
| S5a | 65% verified | refuted | big miss (refuted = 10%) |
| S5b | 55% verified | refuted | big miss (refuted = 15%) |

**1/6 hit (= 17%)、 1/6 near-hit、 4/6 miss**。

Brier score (mean across 6 scenarios):
- 概算: ≈ 0.96
- batch 7 baseline: ≈ 0.45
- = batch 8 で **calibration が悪化した**

### Calibration 悪化の理由

batch 7 retro で「`blocked` カテゴリを含めるべき」 という教訓は反映したが、
**「累積 fix の verified 確率を過大評価する trap」 は batch 8 の独自学習**。

具体的に:
- S5a/S5b で「fix landing 後の retest なら verified が高そう」 と直感的に予測
- 実際は fix が 1 layer 解消するだけで次 layer の blocker が露呈、 verdict は refuted
- chain 完走系 (S1) も「8 commit 累積なら半分くらい verified だろう」 が過大

教訓 (= batch 9 prediction 設計指針):

1. **累積 fix の verify scenario の verified base rate を 20-30% に下げる**
   (batch 8 では 45-65% に設定、 これが overshoot)
2. **「fix landing → 即 verified」 trap**: 各 fix は 1 layer 解消するだけ、
   次 layer の new blocker 確率は >50% と覚悟する
3. **`refuted` の base rate を 20-30% にする** (batch 8 では 10-15% で過小)
4. chain 系 scenario では `verified + inconclusive` の合計を 50% 以下に抑え、
   `blocked + refuted` を 50% 以上に振る

詳細は [`prediction-calibration.md`](prediction-calibration.md) (= batch 9 開始前
更新予定) を参照。

## A4 review (= user 感覚との差分)

- 最大の成果は **新 bug 4 件の構造的発見** + **B8-NEW-2 fix の e2e 初確認**。
  当初の primary 期待 (chain 完走 verify) は未達、 ただし「次の fix 候補が
  data-driven に確定」 という意味で fix wave の motivation が観測 evidence で
  裏付けられた
- prediction 形式: 4 区分 prediction の **`blocked` / `refuted` の base rate を
  上げる** という新 calibration 教訓。 batch 9 では accumulated fix verify の
  scenario で `verified` を 30% 程度に抑える
- **観測 infra の ROI が確認**: batch 7 で投資した 4 道具 (`dogfood_trace`、
  `llm_replay`、 `detect_attractor`、 `REYN_LLM_TRACE_DUMP`) が batch 8 で
  reliable に機能、 sonnet が独立に観測駆動の bug 発見できた
- **router 1-turn 効率改善** という想定外の positive 発見: B7 の 5 turn → B8 の
  1 turn は cost / latency / G12 trigger 機会の三重削減

## 残懸念点 + 次 wave (= batch 9) 候補

| 優先 | 内容 | 関連 finding |
|---|---|---|
| **CRITICAL** | B8-NEW-3 fix: eval_builder の `run_skill` isolated workspace に stdlib path read 許可 | S1, S3 |
| HIGH | B8-NEW-5 fix: `eval を作って` の routing ambiguity 対策 (eval_builder.when_to_use 拡張 or example phrase 強化) | S5a |
| HIGH | B8-NEW-6 fix: `_extract_skill_name` の `unknown` artifact_type ハンドリング (target_skill field 直接参照) | S5b |
| MED | B8-NEW-4 follow-up: tool function descriptions truncation (Pattern A 復活時の保険) | S4 |
| MED | Option F synthetic verify: `llm_replay --patch` で empty stop を engineer して clean failure UX 確認 | S2 |
| LOW | router 1-turn shortcut の再現性 verify (= N=10 session で 5-turn pattern が完全消失したか) | S1 |
| user-side | proxy 強モデル追加 → Wave 3 G4 spike (G12 cost ROI 評価) | tracked, batch 9+ |

batch 9 は **batch 8 で確定した 4 新 bug の fix wave** を中心に組む見込み。
B8-NEW-3 / B8-NEW-5 / B8-NEW-6 は HIGH 3 件 → 各 fix 後に同 scenario retest で
batch 9 sub-wave verify。

## 一言で

> **「fix 1 件 landing は chain の 1 layer 解消、 次 layer の新 blocker は
> >50% 確率で出る」 を batch 8 が data で実証**

batch 7 で観測 infra が整備され、 batch 8 で **「累積 fix verify は深掘りで blocker が
連鎖露呈する」 という dogfood の構造的性質** が初めて言語化された。 道具と原則が
揃った状態で iteration を続けることで、 fix の方向性は確実に data-driven に refine
されていく。
