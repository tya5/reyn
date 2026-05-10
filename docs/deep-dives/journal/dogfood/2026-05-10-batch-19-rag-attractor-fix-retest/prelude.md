# Batch 19 — RAG Attractor Fix Retest Prelude

> Batch 18 で surface した 3 件 LLM-behavioral attractor の fix wave (= 1 commit、
> `ef70aef`) 後の retest。 目的: S6 / S9 で 60%+ verified 復帰 trajectory を
> 確認、 batch 14 milestone (= 70%+) parity への進捗を測る。 mini-retest 構成
> (= S6 / S9 のみ N=3 each、 total ~6 runs)。

## 1. Batch 19 直前の Reyn 状態

main HEAD `ef70aef`、 2223 passed / 2 xfailed (= replay fixture 4 件再録音含む)。
Fix wave 着地内訳 (= 1 commit に 3 fix bundle):

| Bug ID | Severity | Fix layer | 影響 scenario |
|---|---|---|---|
| B18-S9-1 | HIGH | `strategy.md` cost gate decision rule に **strict ordered rule + 「boolean policy flag wins over numeric estimate」 explicit callout** | S9 |
| B18-S5-1 | MED | `recall.py` macro op で `vector` field を ChunkRecord から strip (= ~40KB/call leak 解消) | (long-session UX、 short scenario では surface しない) |
| R-RAG-srcread | attractor | `router_system_prompt.py` で「how is X implemented?」 系 prompt → recall 優先 explicit guidance 追加 | S6 |

## 2. Batch 19 のゴール

1. **S6**: multi-source recall で sources field に 2 source 含むこと (= batch 18 では R-RAG-srcread で 0/3、 fix 後 60%+ 復帰判定)
2. **S9**: cost preflight で LLM が `decision: "abort"` 出力すること (= batch 18 では B18-S9-1 で 0/3、 fix 後 60%+ 復帰判定)

batch 14 milestone (= verified 70%+) への trajectory が正しい方向か確認する batch。

## 3. Out of scope

- S5 (= headline): batch 18 で full recovery (3/3 primary、 拡張 N=12 で 83%) confirmed、 retest 不要
- S8 (= drop_source via web): R1 (= reyn web interactive=False) は別 wave (= release-readiness UX)
- B17-S5-1 (= ctrl42 quirk): phase 2 model selection wave で対応
- 残 MED/LOW deferred: B17-S3-2 / S9-2 / S10-1 / S7-1/S7-2 / S10-2 = sweep wave で

## 4. Embedding 経路

batch 18 と同 `FakeEmbeddingProvider` 路線継続 (= LLM-visible 配線 only に focus、
real proxy 経路は phase 1.5 dogfood 担当)。 fix 3 件はすべて **prompt + op handler
layer** で、 embedding layer 無関係。

## 5. 2 シナリオ + 予測

各 scenario は独立 worktree + 独立 `.reyn/` state、 sonnet sub-agent が driver。
N=3、 total ~6 runs。

### S6: Multi-source recall

**Prompt**: 「How is recall implemented?」 (= batch 18 と同)、 reyn_docs + reyn_src 2 source seed
**期待**: tool_call args の sources field に 2 source 含む (= 順序問わず)
**Sample**: N=3

**Predictions (原則 11 = structural + behavioral 軸分離)**:

| 軸 | 予測 |
|---|---|
| Structural pre-check | ✓ (= batch 18 で確認済、 recall in catalog) |
| Behavioral attractor base rate | R-RAG-srcread が batch 18 で 100% surface、 R-RAG-srcread guidance fix で **50% 程度残存想定** (= prompt fix の効果は確実だが gemini-flash-lite の affordance bias は durable) |
| **Verified prediction** | **50% (= 1.5/3)** / refuted 40% / inconclusive 5% / blocked 5% |

> 根拠: prompt fix で 「'how is X implemented' で recall 優先」 explicit guidance を
> 追加したが、 weak LLM (gemini-2.5-flash-lite) は MUST rule への compliance が
> 50-70% range が batch 1-14 で確立した base rate。 batch 19 で 50% を目標に、
> 達成すれば S6 carry-over close、 未達なら envelope-layer fix or strong model 切替の
> 判断材料に。

### S9: Cost preflight gate

**Prompt**: `reyn run index_docs --source large --path "src/reyn/**/*.py"` + cost_warn_threshold=5
**期待**: Phase 1 LLM が `control.type: "abort"` 出力 (= abort candidate 利用可能 + boolean priority rule で flag を ignore しない)
**Sample**: N=3

**Predictions (原則 11)**:

| 軸 | 予測 |
|---|---|
| Structural pre-check | ✓ (= batch 18 で abort candidate 出現確認済) |
| Behavioral attractor base rate | R-RAG-numerical-vs-flag-bias が batch 18 で 100% surface、 strict ordered rule + explicit callout fix で **30% 程度残存想定** (= 「boolean wins over numeric」 が text-anchored guidance、 attractor を直接対抗) |
| **Verified prediction** | **65% (= 2/3)** / refuted 30% / inconclusive 5% / blocked 0% |

> 根拠: B18-S9-1 fix は 「strict ordered decision rule」 + 「common attractor to avoid」
> 名指し callout の 2 重 reinforcement。 weak LLM への explicit anti-attractor guidance は
> batch 6-12 で 60-80% compliance を達成した実績あり。 ただし numerical anchoring は
> gemini-flash-lite の cognitive bias レベルなので 100% 解消は phase 2 (= strong model)
> 領域。

## 6. Aggregate prediction summary

| 項目 | 予測 |
|---|---|
| total runs | 6 (= 2 scenarios × N=3) |
| mean verified rate | **~58%** (= (50+65)/2)、 batch 14 milestone 70%+ 未達だが trajectory ✓ |
| Brier (scenario 平均) | ~0.30 想定 (= batch 18 = 0.66 から改善見込み) |
| 新 bug count | 0-1 (= attractor partial residual の rate 測定) |

batch 14 milestone への **trajectory ✓ 判定** を **mean verified ≥ 50%** で確定する
(= 50%+ で fix 効果 confirm、 65%+ で strong recovery、 70%+ で milestone parity 復帰)。

## 7. R-attractor 候補 (= 原則 10 強化、 prior batch refuted rate 列含む)

| ID | Description | Prior batch refuted rate | Fix landed | 候補 scenario |
|---|---|---|---|---|
| R-RAG-srcread | reyn_src_read 親和性 vs recall (= 「how is X」 系) | 100% (B18 S6) | ✓ B19 prompt guidance | S6 |
| R-RAG-numerical-vs-flag-bias | boolean flag を numeric value より弱く weight | 100% (B18 S9) | ✓ B19 strict ordered rule | S9 |
| R-RAG-ctrl42 (B17-S5-1) | gemini ctrl42 code-hallucination | ~17% (B18 S5 拡張) | (deferred) | S6 (= 場合により) |

## 8. 並列実行構成

2 sonnet sub-agents、 worktree isolation、 各 agent が 1 scenario 担当 (= S6 / S9)。
user 制限により sonnet 最大並列は 6 (= 2 << 6 で safe)。

## 9. Calibration discipline (= 原則 11 + 12 operationalize)

batch 18 で確立した **新原則 11 (= structural ≠ behavioral 軸分離)** を batch 19 prelude で
operationalize: prediction を **「Structural prediction」 + 「Behavioral prediction」 の 2 row** で書き、
verified rate は両者の積で推定。 上記 §5 の表で実装済。

batch 18 で確立した **新原則 12 (= verdict false-attribution discipline)** も継続:

- LLM が intended tool を invoke せず別 path → **refuted** (= R-attractor 観測)
- intended tool は invoke されたが driver / infra で完走しない → **inconclusive** (= verification path gap)
- structural pre-check 自体が fail → **blocked** (= structural bug)
- intended path が完走 + 期待 outcome 達成 → **verified**

batch 17 「production grade landed」 撤回からの再構築の **secondary axis fix wave 効果検証**
batch。 trajectory ✓ で batch 19 close、 trajectory ✗ なら attractor が prompt-level fix で
解消困難な層 (= envelope / model) かを判断する材料に。
