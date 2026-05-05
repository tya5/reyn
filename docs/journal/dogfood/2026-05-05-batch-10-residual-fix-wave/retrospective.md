# Batch 10 (B9-NEW residual fix wave) — Retrospective

> verify-first principle で 4-step 構成で運用、 **B9-NEW-2 のみが真の bug、 NEW-1/NEW-3 は
> downstream symptom と判明**。 結果 1 fix のみで Reyn dogfood **史上初の chain 完走 via
> `reyn chat`** を達成。 batch 7 観測 infra → batch 8 累積 verify → batch 9 wrong layer trap
> 発見 → batch 10 chain 完走 milestone という 4 batch progression の最終地点。

## 想定と現実のずれ

### 開始時の想定

batch 9 で確定した 3 残 bug (B9-NEW-1/2/3) を fix wave で解消、 chain 完走 candidates 探索
だが「次 layer blocker 露呈」 が >50% 確率と覚悟。

### 実際の進行

| 想定 | 現実 |
|---|---|
| Step 1: B9-NEW-2 verify (50-60% verified) | ✅ verified、 さらに S5b 史上初の e2e 完走 (bonus) |
| Step 2: B9-NEW-1 fix (HIGH) | **不要** (= resolved-indirectly、 B9-NEW-2 + G15 の downstream symptom) |
| Step 2: B9-NEW-3 fix (MED) | **不要** (= resolved-indirectly、 cascade prolonged execution が trigger、 B9-NEW-2 fix で cascade 自体が消失) |
| Step 3: integration retest で chain 完走候補 | ✅ **history milestone** (= S1 が `reyn chat` 経由で 6 phase + sub-skill 全完走) |
| 次 layer blocker 露呈 | 一部 (B10-NEW-1 temp path、 ただし non-blocking)、 主に **non-determinism issues 露呈** (G12 attractor / B9-NEW-3 text-reply) |

= 「fix 1 件で複数 symptom が同時消失」 という **batch 9 retro で予期しなかった pattern**
が batch 10 で実証。

## ターニングポイント 3 つ

### TP1: B9-NEW-1 / B9-NEW-3 の resolved-indirectly 発見

Step 2 で 2 並列 sonnet が **両方とも reproducer 段階で「再現せず」** を確定:

- B9-NEW-1: 実 LLM dogfood 試行 → write_eval が `case_count=3` で正常 schema validation pass。
  原因: B9-NEW-2 ValueError → 3 回 run_skill 失敗 → analyze_skill が degenerate `test_cases=[]`
  を出力 → write_eval が `case_count=0` で `minimum:1` 制約違反、 という 2 段 chain
- B9-NEW-3: code analysis で「failure cascade による prolonged execution が duplication trigger」
  と判明、 cascade 自体が B9-NEW-2 fix で消える

= **「観測した bug が真の bug でなく downstream symptom」 という pattern**。 batch 7 で
RETRO-H3 で「H3 unblock の真の blocker は B7-S5b (= B7-NEW-1 でなかった)」 と発見した
pattern の Tier 2 版。

教訓: **「reproduce or refute first」 = fix 投資前に「現 HEAD で本当に再現するか」 を
確認する** という discipline の効果が batch 10 で実証。 これを実行しない場合の最悪
シナリオ:
- B9-NEW-1 に対して「write_eval phase instruction 強化」 wording fix を投入 → 効果なし
  (= 真の bug でないので)、 prompt bloat 増加 (= G1 trap)
- B9-NEW-3 に対して dedupe 機構拡張 → 不要な OS complexity 増加 (= P3 violation 寄り)

verify-first principle (= batch 9 retro 教訓) を Step 1 でも Step 2 でも運用したことで、
**不要 fix 投資 2 件を回避**。

### TP2: chain 完走 via `reyn chat` の milestone 観測

Step 3 で S1 (= `skill_improver で direct_llm を 1 回 review して改善案を出して`) が:

```
prepare → copy_to_work → run_and_eval → plan_improvements → apply_improvements → finalize
+ sub-skill (eval_builder/eval) 完了
+ skill_narrator が improvement plan を user に届ける
Total: ~60s
```

を `reyn chat` 経由で完走。 **Reyn dogfood 史上初**。

Reyn vision (= memory `project_reyn_vision.md`) で書かれた「Japanese enterprises with high
constraints — predictability over autonomy」 という設計目標 + Reyn の primary use case
(= 「chat 経由で agent が skill を完走させる」) が **初めて end-to-end で機能した
data 確認**。

これは batch 1-7 で何度も blocker に阻まれた、 batch 8 で「earlier shift」 で停止した、
batch 9 で write_eval まで届いて止まった、 という progression の最終地点。 4 batch
跨いだ blocker 解消の総和として成立。

教訓: **e2e milestone は単一 fix でなく、 多 batch 跨いだ累積 fix の総和で成立する**。
G15 (batch 9) + B9-NEW-2 (batch 10) + 過去 batch の各種 fix が組み合わさって初めて
chain が走る。 各 fix は単独では「次 layer 露呈」 にしか見えなくても、 累積で milestone
に到達する。

### TP3: chain 完走「できた」 と「stable に動く」 の分岐

Step 3 で S1 が完走したのは **Run 2 (= 2 試行目)**。 Run 1 では router が text-reply で
stop して invoke せず失敗。 = **50% 成功率**。

S5b も Step 1 で完走、 Step 3 で G12 attractor (= `stop_with_must_rule`、 25% rate) で
失敗。 = **75% 成功率**。

= chain 完走 path は構造的に成立した、 が **probabilistic stability** が次の課題。
これは weak LLM (gemini-2.5-flash-lite) の本質的限界の露呈で、 短期は:
- G12 attractor の真因 fix (= MUST rule wording? logic? structural?)
- B9-NEW-3 router text-reply の structural fix
で改善、 中期は G4 trigger (= 強モデル併用) で抜本的解消。

教訓: **dogfood progression は「機能成立」 → 「stability 確保」 → 「production-ready」
の 3 段階**。 batch 10 で第 1 段階 (= 機能成立) 達成、 batch 11+ が第 2 段階
(= stability) に focus する分岐点。 これは Reyn の **production-grade 開発フェーズ**
(= memory `project_reyn_vision.md`) との整合性が高い。

## 観測 infra の継続利用

batch 7-10 で 4 batch 連続使用、 reliable: ✅
- 並列 sonnet × 4 (Step 1 + 2 並列 + 3 + 4) で全部活用
- `dogfood_trace --mode chain/events/cost` が per-step verdict 確定の primary tool
- `detect_attractor` が S5b の G12 attractor 検出
- `REYN_LLM_TRACE_DUMP` が per-session per-scenario の payload 観測

道具自体は完成、 batch 7 で投資 → 4 batch で利用回収。 **`llm_replay`** はまだ batch 10
で未使用、 batch 11 で G12 真因 fix の N-shot 検証で活用候補。

## prediction calibration の継続改善

3 batch 連続改善:

| Batch | Brier score |
|---|---|
| Batch 8 | ≈ 0.96 |
| Batch 9 | ≈ 0.55 |
| Batch 10 | ≈ 0.30 |

batch 9 retro 教訓 (= 「fix の層で base rate を切り分け」 + verify-first principle) が
calibration accuracy に直接寄与。 さらに batch 10 新教訓:

- **「resolved-indirectly」 pattern を prediction に含める**: 直前 fix landing 後の
  「次 layer 露呈」 base rate を 30-40%、 「resolved-indirectly」 base rate を 20-30% に
  振る (= batch 10 では B9-NEW-1 / NEW-3 両方が後者)
- **non-determinism は single session verify で確定しない**: probabilistic event
  (G12 25% / router text-reply 50%) を含む scenario の verify は N≥3 session で評価

## チームダイナミクス (= user vs assistant)

batch 10 は user 介入が **2 箇所**:
- TP1 (= 「次は?」): 残候補整理を assistant 側に委譲、 推奨 path 提示後 user が承認
- TP2 (= 「subagent も活用してね」): 並列 sonnet 投資の明示承認

= batch 10 は user-recommended option を assistant が prelude → 4-step 自律実行で完遂。
batch 7 (= 設計レベル介入) → batch 8-9 (= 残件可視化介入) → batch 10 (= 実行委譲) という
user 介入の質的 progression。 framework が成熟するとこの方向に shift。

## 次 batch (= batch 11) への申し送り

### Theme: non-determinism reduction (= chain 完走の stable 化)

batch 10 で chain 完走 path が機能、 次は確率的 fail を構造的に減らす:

| 優先 | 内容 | scope |
|---|---|---|
| HIGH | B10-NEW-1 fix: temp workspace path mismatch (`reyn-workspace` vs `reyn_workspace`) | 文字列 typo level、 quick fix |
| HIGH | G12 attractor 真因 fix: `stop_with_must_rule` の 25% rate を構造的に下げる | wording? structural? llm_replay N-shot 必須 |
| HIGH | B9-NEW-3 / B10-NEW-2: router text-reply non-determinism (50% rate) | 別 layer の structural fix 候補 |
| MED | G16 follow-up: natural language routing (= G4 trigger 合流 or 構造的 router decision logic 化) | 別 ADR or wait |
| MED | meta: Tier 2 fixture audit (wrong layer trap 予防) | batch 11 並走 wave |
| LOW | dogfood milestone 文書化 (= production_e2e_milestone memory) | meta |

### prediction 設計
- non-determinism scenario は N≥3 session で評価
- 「resolved-indirectly」 base rate 20-30% を chain 系 fix 後の prediction に含める

### 設計原則の運用
- batch 7-10 で確立した 4 原則 + verify-first principle を継続運用
- 新原則候補: **「reproduce or refute first」 = fix 投資前に現 HEAD 再現確認**
  (= verify-first principle の前段、 「fix の前に bug が真に存在するか」 を確認)

## 一言で

> **B9-NEW-2 fix 1 件のみで chain 完走 via `reyn chat` が史上初成立、 NEW-1/NEW-3 は
> downstream symptom — 「観測した bug ≠ 真の bug」 pattern が batch 10 で実証**

— verify-first principle + reproduce-first discipline で不要 fix 2 件回避
— Reyn dogfood の primary use case (= chat 経由 skill chain) が機能した milestone
— calibration 3 batch 連続改善 (Brier 0.96 → 0.55 → 0.30)
— 残課題は probabilistic non-determinism (G12 25% / B9-NEW-3 50%)、 batch 11 で structural fix

batch 10 で Reyn dogfood が **「fix を積む段階」 から「stability を測る段階」 に移行する
分岐点** を data で確定した batch。 4 batch progression (7→8→9→10) の milestone 達成。
