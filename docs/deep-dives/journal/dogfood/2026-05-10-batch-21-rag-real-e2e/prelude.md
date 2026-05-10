# Batch 21 — RAG Real E2E Smoke Prelude

> Batch 17-20 は FakeEmbeddingProvider + driver-side `write_index_directly` での
> 配線検証だった。 user 指摘 「実 project doc を real embedding で index → recall
> という end-to-end flow は未検証」 を受けて、 main agent (= 私) が直接 e2e
> dogfood を実行する batch。 **synthetic でなく real content + real embedding
> での initial smoke**、 1.0 release narrative 「framework foundation green」 が
> 真に成立するかを ground truth で測定。

## 1. Batch 21 直前の Reyn 状態

main HEAD `49c44c1`、 2223 passed / 2 xfailed。 batch 17-20 で確立した:
- structural foundation (= fix wave 5 + embedding wiring)
- Headline (S5) verified with synthetic content + FakeEmbeddingProvider
- 4-dimension scenario design audit checklist (= 原則 14)
- Pre-retrospective discipline (= 原則 batch 19)

batch 18-20 の **3 batches 連続 scenario design flaw** で affordance-bias hypothesis
は依然 pending、 batch 21 は **real content + real embedding** という本質的に
異なる scenario で副次的 hypothesis 検証も兼ねる。

## 2. Batch 21 のゴール

1. **Real e2e flow primary verification**: `reyn run index_docs <project_docs>` →
   `reyn chat <natural query>` が user 視点で機能するか
2. **Real embedding wiring verification**: gemini-embedding-001 via LiteLLM proxy
   での chunk embedding + SQLite write + cosine query が動作するか
3. **Affordance-bias hypothesis 4 度目の attempt**: real content + 概念 prompt で
   LLM が recall を picks するか (= Class B 評価の valid evidence 候補)

## 3. Scenario Design Audit (= 原則 14、 4 dimension)

**Source**: `reyn_concepts` (= `docs/concepts/*.md` 21 EN files、 real
gemini-embedding-001 via LiteLLM proxy `localhost:4000`)

**Description**: 「Reyn's design concepts and architectural principles」

| Q | Prompt | Dim 1 (data semantic match) | Dim 2 (tool affordance match) | Dim 3 (source-count) | Dim 4 (rational alternatives) |
|---|---|---|---|---|---|
| Q1 | What is the care boundary in Reyn? | ✓ care-boundary.md indexed | ⚠️ reyn_src_read description claim と「what is X in Reyn」 が semantic 近接 | ✓ 1 source | reyn_src_read / web_search / memory; reyn_src_read が rational alternative |
| Q2 | Explain Reyn's permission model. | ✓ permission-model.md | ⚠️ 同上、 「Explain X」 = 概念 ask だが gemini-flash-lite の文脈 distinction 弱い可能性 | ✓ 1 source | 同上 |
| Q3 | What is plan mode in Reyn? | ✓ plan-mode.md | ⚠️ 同上 | ✓ 1 source | 同上 |

**Audit 判定**: dimension 2 全 query で ⚠️、 ただし `reyn_src_read` description の
claim は 「how does Reyn / how does Reyn's X work?」 で **prompt の 「what is」
/ 「explain」 とは textbook match していない**。 weak LLM (gemini-flash-lite) が
fine-grained semantic distinction を持つかは不確実、 batch 21 がこれを測る。

**Approval**: 全 dim ✓ ではないが、 **dim 2 ⚠️ 自体が batch 21 の measurement
target** (= affordance-bias hypothesis evaluation)。 prelude 段階で trade-off を
明示記録し execution 承認。 valid evidence への前進、 過去 3 batches の confound
を回避する design。

## 4. Predictions (= 原則 11 軸分離)

| 軸 | 予測 |
|---|---|
| Structural pre-check | ✓ (= recall in catalog、 SP Indexed sources section、 既存 verified) |
| Behavioral: recall invoke rate | **未知**、 batch 21 measurement target |
| Verified prediction (recall + indexed chunks 含む reply) | **40-60%** (= 中央値 50%、 dim 2 ⚠️ で affordance-bias 残存リスク) |
| Refuted (= reyn_src_read picks、 affordance-bias direct evidence) | **30-50%** |
| Inconclusive (= subprocess error、 indexing fail) | **5-10%** |

> 根拠: batch 18 S5 (= 「Search the docs」 explicit instruction) で 83% verified を
> 達成、 ただし 「Search the docs」 が absent な natural query で base rate がどう
> shift するかは未測定。 weak LLM の 「explicit search hint なき場合の recall
> 自律 invoke 率」 は本 batch の novel data。

## 5. Sample size

N=3 primary、 result が ambiguous (= 30-60% range) なら N=5 まで拡張。 indexing
は 1 度のみ実行 (= cost preflight passes、 ~$0.001 で十分)。

## 6. Pre-retrospective discipline (= 原則 batch 19、 self-impose)

main agent (= 私) が retrospective 執筆前に必ず実行:

1. ✅ N=3 全 run の LLM trace dump (= `/tmp/reyn_e2e_traces2/q*.jsonl`) を読む
2. ✅ `recall` ToolDefinition + `reyn_src_read` ToolDefinition description 再確認
3. ✅ Scenario design audit が actual 観測と整合するか self-check (= dim 2 ⚠️ が
   surface したか、 別 dimension で confound あるか)

batch 20 で先例、 batch 21 でも厳格適用。

## 7. Execution log

```
1. Workspace: /tmp/reyn_e2e_smoke/ (= isolated /tmp、 main repo .reyn 不汚染)
2. Indexing:
   reyn run index_docs '{"source": "reyn_concepts", "path": "<repo>/docs/concepts/*.md", "description": "Reyn's design concepts and architectural principles"}'
3. Source state verification: reyn source list / describe
4. N=3 chat queries (history.jsonl wipe between runs):
   Q1: "What is the care boundary in Reyn?"
   Q2: "Explain Reyn's permission model."
   Q3: "What is plan mode in Reyn?"
5. Trace capture: REYN_LLM_TRACE_DUMP=/tmp/reyn_e2e_traces2/q<i>.jsonl
6. Pre-retrospective discipline self-execution
7. Findings + retrospective write-up
```

## 8. Calibration discipline

batch 21 は **single batch single scenario class** (= concept question against
indexed concept docs)、 真の attractor base rate は N=3 のみ、 follow-up batch
(= 別 prompt class、 例 「Search docs for X」 explicit instruction) で
cross-validation 候補。
