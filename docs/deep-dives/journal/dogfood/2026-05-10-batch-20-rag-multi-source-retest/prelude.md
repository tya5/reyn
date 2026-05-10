# Batch 20 — RAG Multi-Source Retest Prelude (= S6 redesign)

> Batch 19 self-audit で S6 が **scenario design flaw** (= prompt 「How is recall
> implemented?」 が code-reading query で indexed concept docs と semantic
> mismatch、 LLM の `reyn_src_read` 選好は tool description との textbook match
> で正しい routing) と判明、 affordance-bias attractor の存否は **valid retest
> 待ち** に。 batch 20 は S6 を redesign して **affordance conflict を排除した
> 純粋な multi-source recall 検証** を実施。

## 1. Batch 20 直前の Reyn 状態

main HEAD `12832f2` (= batch 19 self-audit + R-RAG-srcread guidance revert + memory + plan 修正)、 2223 passed / 2 xfailed。

## 2. Batch 20 のゴール

**S6 を valid scenario で再測定**:

1. multi-source recall の **真の base rate** (= 2 source 両方 picks する rate) を測定
2. **affordance-bias hypothesis の存否** を valid evidence で評価 (= LLM が 「multi-source あっても 1 source で満足する」 attractor が存在するか)
3. valid retest 結果を batch 19 retrospective Class B (= hypothesis pending) ステータスの解消材料に

## 3. Out of scope

- S5 / S9 (= 既に valid evidence で確立済、 retest 不要)
- S8 (= reyn web interactive=False、 release-readiness wave 別 lump)
- B17-S5-1 ctrl42 (= phase 2 model selection)

## 4. Embedding 経路

batch 18-19 と同 `FakeEmbeddingProvider` 路線継続 (= LLM-visible 配線 only に focus)。

## 5. S6 Redesign — synthetic sources で affordance conflict 排除

### Setup

2 source seed (= 既存 `write_index_directly` 利用、 FakeEmbeddingProvider):

| Source name | Content type | Chunks |
|---|---|---|
| `quantum_concepts` | Fictional system concept docs | 「quantum bridge protocol とは」 「2 つの主要 component (= entangler / decoherence_buffer)」 「typical use cases」 等 5-6 chunks |
| `quantum_code` | Fictional system code chunks | `def entangler(...)`、 `class DecoherenceBuffer:`、 `def bridge_protocol_handshake(...)` 等 5-6 chunks |

両 source とも **Reyn 実装と完全独立** (= reyn_src_read で見えない、 web search で見えない)。

### Prompt

```
How does the quantum bridge protocol work?
```

### Why this design works

| Routing path | Status |
|---|---|
| `recall(sources=["quantum_concepts", "quantum_code"])` | ✅ 期待 (= multi-source) |
| `recall(sources=["quantum_concepts"])` | △ partial、 verdict "verified" 候補 (= 1 source picks も recall 経路完走) |
| `recall(sources=["quantum_code"])` | △ partial、 同上 |
| `reyn_src_read("README.md")` | ✗ 物理的に答えが出ない (= description claim の 「how does Reyn X work?」 ではない、 README に quantum bridge は無い) |
| `web_search("quantum bridge protocol")` | △ 可能だが indexed sources がある以上 recall 優先するべき affordance |

**affordance conflict 排除**: `reyn_src_read` description (= 「how does Reyn X work?」) は user query と semantic match しないので、 LLM が選ぶ理由なし。

### Verdict criteria (= 原則 11 軸分離)

**Structural axis** (= deterministic):
- ✓ recall in tool catalog (batch 17/18/19 で confirmed)
- ✓ Indexed sources section に 2 source 表示

**Behavioral axis** (= stochastic、 N runs base rate):

| Verdict | 条件 |
|---|---|
| **verified** | recall invoke + sources field に 2 source 含む (= 順序問わず) |
| **refuted (Class B-A1)** | recall invoke だが sources field に 1 source のみ (= multi-source attractor 仮説の direct evidence) |
| **refuted (Class B-A2)** | recall 非 invoke で web_search や text-only 返答 (= 別系統) |
| **inconclusive** | driver / subprocess error |
| **blocked** | structural pre-check fail |

## 6. Predictions (= 原則 11 + 13 hypothesis 再評価)

| 軸 | 予測 |
|---|---|
| Structural pre-check | ✓ (= 確認済) |
| Behavioral: 「recall を invoke する rate」 | ~95% (= affordance conflict 排除済、 recall は唯一の rational path) |
| Behavioral: 「multi-source picks rate」 (= recall invoke 中で 2 source 含む rate) | **未知**、 これが本 batch の測定対象。 仮説範囲: 30-70% |
| **Verified prediction** | **40%** (= 0.95 × 0.45 想定中央値) |
| Refuted (Class B-A1 = 1 source only) | ~55% |
| Refuted (Class B-A2 = recall 非 invoke) | ~5% |

> 根拠: weak LLM (gemini-2.5-flash-lite) は 「複数 source を網羅 picks」 という
> meta-cognitive task に弱い base rate (= batch 6-12 で MUST rule compliance 60-80%、
> 「multi-source 綜合」 は 1 step 重い)。 50% 中央値想定で N=3 measurement。

**verified ≥ 50% で affordance-bias hypothesis 棄却** (= multi-source recall が natural に動く)、 **verified < 30% で hypothesis 支持** (= 1 source 満足 attractor 観測)、 **30-50% range で追加 N 測定推奨**。

## 7. Sample size

N=3 primary、 trajectory が ambiguous (= 30-50% range) なら N=5 まで拡張。 single scenario なので 1 sonnet agent 担当。

## 8. Pre-retrospective discipline (= 原則 batch 19 self-audit)

retrospective 執筆前に必ず:

1. **LLM trace dump** (= `/tmp/reyn_s6_b20/run_*.jsonl`) を全 run 読む
2. **`recall` ToolDefinition description** を再確認
3. **scenario design 前提** (= prompt と indexed content の semantic match) を確認

memory `feedback_pre_retrospective_discipline.md` の 3 step を operationalize、 batch 19 で違反した過剰一般化 trap を batch 20 で再発させない。

## 9. Calibration discipline (= 原則 11 + 12 + 13 統合)

- **原則 11 (= structural × behavioral 軸分離)**: §5/§6 で 2 row 予測
- **原則 12 (= verdict false-attribution discipline)**: §5 verdict criteria で refuted 内部を Class B-A1 / A2 で subdivide (= 「multi-source で 1 source 満足」 vs 「recall 非 invoke」 を別物として記録)
- **原則 13 (= attractor class taxonomy、 hypothesis pending)**: 本 batch で affordance-bias hypothesis を valid evidence で評価、 結果次第で memory `feedback_attractor_class_taxonomy.md` の Class B status を更新
