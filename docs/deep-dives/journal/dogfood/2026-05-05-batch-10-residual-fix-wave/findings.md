# Batch 10 (B9-NEW residual fix wave) — Findings

> verify-first principle で運用、 Step 1 (B9-NEW-2 e2e) → Step 2 (B9-NEW-1 + B9-NEW-3
> diagnose) → Step 3 (integration retest) の sequence で進行。 **B9-NEW-2 のみが真の bug、
> B9-NEW-1 + B9-NEW-3 は downstream symptom で resolved-indirectly**。 結果 **Reyn dogfood
> 史上初の chain 完走 via `reyn chat`** を確認 (S1)。

> **⚠️ Provisional milestone (= N=1 sample)** — batch 11 5-shot retest revealed
> this Run 2 completion was a non-deterministic lucky case. batch 11 N=5 showed
> 0/5 complete rate due to B11-NEW-1 (preprocessor `run_op` permission denied).
> Real milestone confirmation is target of batch 12 (= N≥5 with ≥60% complete).
> See `../2026-05-06-batch-12-real-milestone/` for resolution.

## Summary table

### Step 1: B9-NEW-2 e2e verify (= verify-first principle)

| Fix under test | e2e Verdict | 結果 |
|---|---|---|
| `8f3bccf` (B9-NEW-2 / G17 wrong-layer) | **verified** | S5b 単独 invoke で `compute_paths` 成功、 analyze_skill → write_eval → finish 完走、 eval.md 生成 (= S5b 史上初の e2e 完走) |

### Step 2: B9-NEW-1 + B9-NEW-3 diagnose (= 並列 sonnet 投資)

| Bug | 結論 | 理由 |
|---|---|---|
| **B9-NEW-1** (write_eval schema validation) | **resolved-indirectly** | downstream symptom of B9-NEW-2 + G15 失敗 cascade。 fixed 状態では `case_count=0` でなく正常 `case_count=3` で write_eval が schema validation pass |
| **B9-NEW-3** (router invoke duplication) | **resolved-indirectly** | run_skill 失敗の cascade で発生していた現象、 B9-NEW-2 fix で cascade 自体が消えたため再現せず。 既存 G3 dedupe + G10 (immediate router return on error) が構造的にカバー |

### Step 3: integration retest (= 統合 chain 確認)

| Scenario | B9 Verdict | B10 Verdict | 主要発見 |
|---|---|---|---|
| **S1** (chain 完走 via chat) | inconclusive | **verified ⚠️ provisional** | **Reyn 史上初 (= N=1 Run 2 のみ、provisional)**: skill_improver 6 phase 完走 + sub-skill (eval_builder/eval) 完了 + narrator 経由 user 通知。 Run 1 で router text-reply 失敗 (= B9-NEW-3 pattern 残存)、 Run 2 で完走。 batch 11 5-shot で 0/5 (B11-NEW-1 blocked) |
| **S5a** (自然言語 invoke) | refuted | **refuted (継続)** | router が eval_builder を identify するが「clarification text-reply」 で stop。 G16 wording fix 依然 no-effect |
| **S5b** (構造 invoke) | refuted | **refuted (non-deterministic)** | Step 1 で完走、 Step 3 で G12 attractor (stop_with_must_rule) 発火 → describe_skill 後 empty stop。 25% rate の確率的事象、 B9-NEW-2 fix は構造的に sound |

### 検出した新 bug (= batch 11 候補)

| ID | 重要度 | 内容 |
|---|---|---|
| **B10-NEW-1** | MED | `eval.run_target` の `run_skill` が temp workspace path mismatch (`/tmp/reyn-workspace` vs `/tmp/reyn_workspace`、 hyphen vs underscore)。 chain は retry で継続するが eval は degraded data で実行 |
| **B10-NEW-2** | MED | router text-reply non-determinism (= B9-NEW-3 が完全には消えず、 run_skill cascade と無関係な context でも text-reply で stop することがある)。 Run 1 で発火、 Run 2 で正常 |

### Cost summary

| Step | Tokens | Cost USD |
|---|---|---|
| Step 1 (S5b) | 42,358 | $0.001615 |
| Step 2 B9-NEW-1 diagnose (real LLM repro) | ~80,000 | ~$0.005000 |
| Step 2 B9-NEW-3 diagnose (code-only) | 0 | $0.000000 |
| Step 3 integration (3 session) | 165,748 | $0.002264 |
| **Total** | ~290,000 | **~$0.009000** |

## Round 別 narrative

### Round 1: prelude + Step 1 verify-first

batch 9 retro 教訓 (= 「fix verify は per-fix Tier 3 e2e cross-check 必須」) を運用適用。
B9-NEW-2 fix (`8f3bccf`) を Step 1 で e2e 確認 → wrapped form `{"type":"eval_builder_request",
"data":{"target_skill":...}}` で priority 2 path が動作、 fix が **e2e で真に effective**
を確認。 さらに **eval_builder S5b 史上初の chain 完走** という想定外の bonus。

### Round 2: B9-NEW-1 / B9-NEW-3 並列 diagnose

Step 2 を並列 sonnet 2 体で:

- B9-NEW-1 sonnet: 実 LLM dogfood で reproducer 試行 → **再現せず**。 root cause analysis で
  「B9-NEW-2 ValueError → run_skill 全失敗 → analyze_skill が degenerate skill_analysis 出力 →
  write_eval が `case_count=0` で schema 不適合」 という 2 段 downstream chain と判明。
  fix 不要、 documentation 化のみ。

- B9-NEW-3 sonnet: structural code analysis で「failure cascade による prolonged execution
  window が duplication trigger」 と判明、 cascade 自体が B9-NEW-2 fix で消えるため再現せず。
  既存 G3 + G10 が構造的にカバー、 fix 不要。

= **並列 sonnet 投資で 2 件を効率的に diagnose**、 結果両方 resolved-indirectly。

### Round 3: integration retest (Step 3)

Step 3 で `reyn chat` 経由の S1 chain 完走を **史上初観測**:

```
[T+2s]  router invoke → skill_improver
[T+3s]  run_skill(eval_builder) → analyze_skill → write_eval (eval.md written) ✅
[T+20s] copy_to_work completed (workspace dir 作成 + skill files copy)
[T+22s] run_skill(eval) → run_target → evaluate (×2 cycle)
[T+30s] plan_improvements completed
[T+37s] apply_improvements completed (1 phase_retry recovered)
[T+39s] finalize completed (improvement_result emit)
[T+39s] skill_narrator ran (improvement plan delivered to user)
Total wall time: ~60s
```

= **「chat 経由で skill_improver が動く前提が揃った」 という Reyn の primary use case が
初めて完全に機能した data 確認**。 batch 6 retrospective で書いた「地味だが確かな前進」
の表現がそのまま当てはまる milestone。

ただし:
- **non-deterministic**: S1 Run 1 は router text-reply で失敗、 Run 2 で完走 (= 50% 成功率)
- **G16 unresolved**: S5a 自然言語 invoke は依然 refuted
- **G12 unresolved**: S5b で attractor 発火 (= 25% rate)

= 「chain 完走 path が成立した」 と「stable に動く」 は別次元。 batch 11 は確率的
non-determinism の structural fix が中心になる。

## Prediction calibration

batch 10 prelude で予測:

| Step | Top prediction | Actual | Hit? |
|---|---|---|---|
| Step 1 (B9-NEW-2 e2e) | 50-60% verified | verified | **hit** |
| Step 3 S1 | 30-40% verified | verified | **hit** |
| Step 3 S5a | refuted (G16 継続) | refuted | **hit** |
| Step 3 S5b | inconclusive (non-determ) | refuted (non-determ) | near-hit |

= 3/4 hit、 1/4 near-hit。 Brier ≈ 0.30 (batch 9: 0.55、 batch 8: 0.96 から継続改善 ✅)。

batch 9 retro 教訓 (= 「fix の層で base rate を切り分け」 + 「verify-first principle」) が
calibration accuracy にに直接効いた。

新教訓 (= batch 11 への継承):
- **「fix 1 件で複数 downstream symptom が resolved-indirectly される」 pattern** を予測
  prediction 設計時に意識: chain blocker が「同じ root cause の symptom 群」 だった場合、
  1 fix で複数 bug が同時消失する
- **non-determinism は単一 session verify では確定しない**: S5b は Step 1 で verified、
  Step 3 で refuted、 真の verdict は N≥10 session で probability 確定が必要

## A4 review (= user 感覚との差分)

- **headline**: chain 完走 via `reyn chat` 史上初確認 (= Reyn の primary use case が機能、
  batch 7 の「観測 infra」、 batch 8 の「累積 fix verify」、 batch 9 の「wrong layer trap
  発見」 を経て、 batch 10 で「dogfood の primary use case 達成」 という progression)
- **fix discipline 改善**: B9-NEW-1 / B9-NEW-3 を「reproduce or refute first」 で診断 →
  両方 resolved-indirectly に分類、 不要な fix 投資を回避。 batch 9 retro の「verify-first
  principle」 を運用で実証
- **non-determinism の言語化**: chain 完走「できた」 と「stable に動く」 は別、 確率的
  fail (G12 attractor 25% / B9-NEW-3 router text-reply 50%) が次の課題
- **calibration 継続改善**: Brier 0.96 → 0.55 → 0.30 で 3 batch 連続改善

## 残懸念点 + 次 wave (= batch 11) 候補

| 優先 | 内容 | 関連 |
|---|---|---|
| HIGH | B10-NEW-1 fix: temp workspace path mismatch (`reyn-workspace` vs `reyn_workspace`) | S1 |
| HIGH | G12 attractor 真因 fix: `stop_with_must_rule` の 25% rate を構造的に下げる (= MUST rule の wording? logic? truncate?) | S5b non-determinism |
| HIGH | B9-NEW-3 / B10-NEW-2: router text-reply non-determinism の structural fix (= run_skill cascade と無関係な context でも発火) | S1 Run 1 |
| MED | G16 follow-up: natural language eval_builder routing (= G4 trigger 合流 or 構造的 router decision logic 化) | S5a |
| MED | meta: Tier 2 fixture audit (wrong layer trap 予防) | batch 11 並走 |
| LOW | Reyn dogfood 史上初 chain 完走の milestone 文書化 (= production_e2e_milestone memory? release readiness checklist?) | meta |

batch 11 は **non-determinism reduction** wave: G12 / B9-NEW-3 / B10-NEW-1 の 3 件で
chain 完走の安定化が中心。

## 一言で

> **⚠️ Provisional milestone (= N=1 sample)** — batch 11 5-shot retest revealed
> this Run 2 completion was a non-deterministic lucky case. batch 11 N=5 showed
> 0/5 complete rate due to B11-NEW-1 (preprocessor `run_op` permission denied).
> Real milestone confirmation is target of batch 12 (= N≥5 with ≥60% complete).
> See `../2026-05-06-batch-12-real-milestone/` for resolution.

> **B9-NEW-2 fix のみが真の bug、 NEW-1/NEW-3 は downstream symptom — 1 structural fix で
> chain 完走 via `reyn chat` が史上初成立**

— verify-first principle が batch 9→10 で完全運用、 不要 fix 投資を回避
— resolved-indirectly classification framework で「downstream symptom」 を pattern 化
— Reyn dogfood の primary use case (= chat 経由 skill_improver chain) が機能した milestone
— 残課題は確率的 non-determinism (G12 25% / B9-NEW-3 50%)、 batch 11 で structural fix

batch 10 で Reyn dogfood が **「fix を積む段階」 から「stability を測る段階」 に移行する
分岐点** を data で確定した batch。
