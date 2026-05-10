# Batch 17 (RAG-extensible OS Phase 1 — first real dogfood) — Prelude

> ADR-0033 RAG-extensible OS Phase 1 (= 2026-05-10 landed、 12 commits、
> commit chain `d2db332..62fd21b`、 +131 net new tests) を **初めて real LLM +
> 統合経路で観測** する batch。 5 op + IndexBackend + EmbeddingProvider +
> SourceManifest + index_docs stdlib skill + recall/drop_source ToolDefinition
> + reyn source CLI + UX gap fix 5 件 が production grade で動作するかを
> observe-first で記録する。

---

## 1. Batch 17 直前の Reyn 状態

| 項目 | 値 |
|---|---|
| Date | 2026-05-10 |
| main HEAD | `62fd21b` |
| Test suite | 2204 passed / 2 xfailed |
| 最終 real LLM batch | batch 16 (2026-05-08、 plan-mode validation) |
| ADR-0033 commits | 12 (= d2db332..62fd21b、 全て Tier 2/3 で validated だが integration dogfood は本 batch が初) |
| Embedding API access | **OPENAI_API_KEY 不在、 ANTHROPIC のみ set。 LiteLLM proxy = chat (gemini-2.5-flash-lite) のみ、 embedding endpoint なし** |

### ADR-0033 landing 履歴 (新着順)

```
62fd21b  docs(rag): ADR-0033 → Accepted + FP-0002 → done
b4f68f5  style: ruff import-sort fixes for ADR-0033 wave
1e6f153  feat(rag): recall + drop_source ToolDefinitions + reyn source CLI
7a16f8c  docs(rag): user-facing documentation for ADR-0033
20f82ad  feat(rag): router system prompt — Indexed sources section + empty state hint
1e18c8e  feat(rag): 5 op handlers + permission gate
4a9677e  feat(rag): index_docs stdlib skill + chunkers + UX B/C/D
8829264  feat(rag): reyn.yaml embedding section parser + defaults
72dd28d  feat(rag): EmbeddingProvider + LiteLLM impl + cost estimator
05a24b1  feat(rag): SourceManifest singleton + sources.yaml SSoT
f9abcda  feat(rag): IndexBackend protocol + SqliteIndexBackend impl
d2db332  feat(rag): ADR-0033 + RAG op schemas + registry — Phase 1 foundational layer
```

---

## 2. Batch 17 のゴール (= 観測したい問い)

ADR-0033 Phase 1 が Tier 2/3 test で closed-form に検証された設計どおりに、
**実 LLM + 実 chain (= chat / CLI / OS layer)** で動作するかを観測する。 問いを 8 つに絞る:

| # | 問い |
|---|---|
| G1 | `reyn run index_docs` の Phase 1 LLM が valid `chunk_strategy` artifact を emit するか (= boundary が enum 内、 size 範囲内) |
| G2 | Skill.postprocessor (= chunkers.apply_strategy → embed → index_write) が deterministic に走り SQLite に書込まれるか |
| G3 | `SourceManifest` が `index_docs` 完了時に atomic 更新され、 次 turn の router system prompt 「Indexed sources」 section に現れるか |
| G4 | Empty state UX (= 0 source 時の getting-started hint) が router system prompt + `reyn source list` 双方に出るか |
| G5 | Router LLM が user の質問意図に応じて `recall` tool を自律的に invoke + 適切 sources picks するか |
| G6 | Router LLM が destructive request (= 「remove ...」) で `drop_source` tool を invoke + permission ask が起動するか |
| G7 | Memory inline behavior (= Phase 1.5 で migration 予定だが Phase 1 は不変) が回帰なし working するか |
| G8 | `reyn source list/describe/rm` CLI が permission gate + audit event 含めて期待通り動作するか |

---

## 3. Out of scope

以下は batch 17 で観測しない:

- **Real LiteLLM embedding API integration** (= OPENAI_API_KEY 不在のため、 FakeEmbeddingProvider 経由で代替。 production user 環境での実 API 動作は phase 1.5 dogfood の scope に move)
- **Phase 1.5 memory migration** (= router system prompt の inline → recall fetch、 後続 wave)
- **Phase 2 features** (= sub-record cache / parallel batches / sqlite-vec / advanced retrieval)
- **Multi-process concurrency** (= phase 2 file mtime poll)
- **Override pattern (= AST chunker)** (= 差別化の核 narrative だが Phase 1 stdlib のみ test、 override は user 側 work)
- **Cost preflight 実 API 経由 (= S9 で fake estimate のみ confirm)**

---

## 4. Embedding 制約への対応 = FakeEmbeddingProvider 経由

OPENAI_API_KEY 不在のため、 real LiteLLM embedding endpoint へ届く dogfood は不可。
代わりに **`tests/test_embedding_provider.py` 由来の `FakeEmbeddingProvider` を
`register_provider("fake", FakeEmbeddingProvider)` で登録**、 各 scenario の driver script
が startup 時に register、 reyn.local.yaml で `embedding.provider: fake` 指定 (= phase 2
plugin path の先取り使用)。

FakeEmbeddingProvider は text 内容から deterministic hash-based vector を生成 (= dimension
1536、 cosine similarity は意味薄いが pipeline 全体は exercise される)。

これで verify される項目:
- ✓ Phase 1 LLM の ChunkStrategy 生成
- ✓ Skill.postprocessor の python step → embed op → index_write op chain
- ✓ EmbeddingProvider abstraction の plugin path 動作
- ✓ SQLite write / query / drop 全 path
- ✓ SourceManifest atomic update + system prompt rebuild
- ✓ recall macro op の sub-op dispatch (embed → iterate index_query → merge)
- ✓ Router LLM の recall / drop_source tool selection
- ✓ CLI smoke
- ✓ Permission gate (`permissions.embed: ask`、 `permissions.index_drop: ask`)

verify されない項目 (= phase 1.5 dogfood に move):
- ✗ Real OpenAI embedding API response shape / latency / 429 retry
- ✗ Real cosine similarity precision (= production retrieval 品質)
- ✗ LiteLLM proxy embedding endpoint の実存性

---

## 5. 10 シナリオ + 予測

各 scenario は独立 worktree + 独立 `.reyn/` state で run、 sonnet sub-agent が driver。
N=3 が default、 critical scenario (S5/S6) のみ N=5。 total ~33 runs。

### S1: Empty state UX

**1-line goal**: 0 source の fresh workspace で system prompt + CLI 両方に getting-started hint が出ることを確認。

**Driver**: 新規 worktree、 `.reyn/` 空。 `reyn source list` 実行 + `reyn chat` 1 turn (= "what can I do?")。
**観測**: CLI hint 出力、 router system prompt に `## Indexed sources (0 available)` + `reyn run index_docs` example が含まれる、 LLM が source 0 件を認識して hallucinate しない。
**Sample**: N=3。
**予測**: verified 80% / refuted 10% (= LLM が source あると hallucinate) / inconclusive 10%。

### S2: Index small memory layer

**1-line goal**: `.reyn/memory/*.md` を memory source として indexing、 Phase 1 LLM の strategy 決定 + Phase 2 chain 完走を確認。

**Driver**: worktree に test memory file 3-5 件 seed、 `reyn run index_docs --source notes --path ".reyn/memory/*.md" --description "User notes"` を subprocess で実行 + `.reyn/index/sources.yaml` + SQLite confirm。
**観測**: Phase 1 LLM が `chunk_strategy` artifact emit (= boundary in [heading, blank_line, sentence], max_chunk_size_tokens 100-4000)、 Phase 2 で chunks.jsonl + chunks_with_vectors.jsonl 生成、 SQLite に N chunks 書込み、 sources.yaml entry 自動生成、 events log に `index_dropped` ではなく postprocessor step events emit。
**Sample**: N=3。
**予測**: verified 60% / refuted 20% / inconclusive 15% / blocked 5%。

### S3: Index Reyn docs (medium scale)

**1-line goal**: `docs/concepts/*.md` (~10 files、 ~50 chunks 想定) を indexing、 heading-based chunking + larger scale 動作確認。

**Driver**: `reyn run index_docs --source reyn_docs --path "docs/concepts/*.md" --description "Reyn concepts"` 実行、 chunks の structure と分布を確認。
**観測**: Phase 1 LLM が `boundary: heading` を選択 (= Markdown 構造から推測)、 ~50 chunks 程度に分かれる、 chunks の `parent_context` field に heading label が入る (= preserve_parent_context: true 時)。
**Sample**: N=3。
**予測**: verified 55% / refuted 25% (= heading attractor 弱い) / inconclusive 15% / blocked 5%。

### S4: Index Python source

**1-line goal**: `src/reyn/op_runtime/embed.py` を indexing、 default chunkers (= AST not、 blank_line / sentence) で Python に対応するか確認。

**Driver**: `reyn run index_docs --source rag_code --path "src/reyn/op_runtime/embed.py" --description "RAG embed op handler"` 実行。
**観測**: Phase 1 LLM が `boundary: blank_line` 選択 (= AST 不在で default fallback)、 chunks size がコード密度に応じて妥当 (= 関数 1 個程度に近い)、 chunks indexed > 0。
**Sample**: N=3。
**予測**: verified 50% / refuted 30% (= LLM が enum 外を hallucinate) / inconclusive 15% / blocked 5%。

### S5: Recall via chat (= headline scenario、 narrative の核)

**1-line goal**: indexed source に対して chat で関連質問 → router LLM が `recall` tool を自律 invoke + valid sources を picks、 結果 chunks を回答に組込む。

**Driver**: 事前に `reyn_docs` source を seed (= S3 が成功した state を直接 SQLite injection で再現)、 `reyn chat` で「What does the recall tool do? Search the docs.」 type prompt を turn 1 に送信、 events log で tool_call が `recall` であること + sources field が `["reyn_docs"]` であることを confirm。
**観測**: router system prompt に `## Indexed sources (1 available)` + `reyn_docs` 表示、 LLM が `recall(query=..., sources=["reyn_docs"], top_k=5)` invoke、 result chunks が next turn の context に届く、 reply text が retrieved chunks 内容と consistent。
**Sample**: N=5 (= R5/R6 attractor 解消の base rate 測定で headline)。
**予測**: verified 45% / refuted 40% (= R1-attractor 系で recall invoke 忘れ) / inconclusive 10% / blocked 5%。

> **リスクノート**: batch 16 で plan tool invoke rate 0/25 を観測。 recall tool は
> 用途が直接的 (= 「search docs」 prompt) なので invoke rate は plan より高いが、
> 弱モデル (gemini-flash-lite) は training-data から答えてしまう attractor が継続。

### S6: Multi-source recall

**1-line goal**: 2 source (reyn_docs + reyn_src) indexed の状態で「How is recall implemented?」 prompt → LLM が両方の sources を picks + global top-K merge。

**Driver**: 事前に reyn_docs + reyn_src の 2 source seed、 `reyn chat` 1 turn、 tool_call args の sources field が `["reyn_docs", "reyn_src"]` (= 順序問わず両方含む) を確認。
**観測**: LLM が両 source 必要と判断、 merged top-K 5 chunks に両 source 由来が混在、 mode field が `"semantic"` (= sources 全 indexed なので fallback ではない)。
**Sample**: N=5。
**予測**: verified 30% / refuted 50% (= LLM が 1 source だけで満足) / inconclusive 15% / blocked 5%。

### S7: Memory inline regression check

**1-line goal**: Phase 1 で memory layer は不変 (= Phase 1.5 で migration 予定)、 既存の inline memory behavior が回帰なし動作。

**Driver**: 新規 worktree + 既存 memory entry (= `.reyn/memory/feedback_*.md` 3 件 seed) + indexing しない、 `reyn chat` で「最近の deterministic split feedback について教えて」 prompt → existing memory が system prompt 経由で見える (= inline)、 LLM が回答に組込む。
**観測**: router system prompt に既存の `## Memory` section 表示 (= ADR-0033 後も不変)、 LLM が memory 内容を直接利用、 recall tool は invoke しない (= memory 専用検索は phase 1.5)。
**Sample**: N=3。
**予測**: verified 80% / refuted 10% (= 別 attractor) / inconclusive 5% / blocked 5%。

### S8: drop_source via chat + permission ask

**1-line goal**: indexed source に対して chat 経由で削除依頼 → LLM が `drop_source` tool invoke、 permission gate (= ADR-0029 mirror) が ask を発火、 user 答えで destructive action 完遂。

**Driver**: 事前に `test_drop` source seed、 `reyn chat` で「Remove the test_drop source from the index」 prompt、 ask_user intervention に `y` 投入、 tool_call が `drop_source(source="test_drop")` であること + sources.yaml entry 削除 + SQLite ファイル消滅 confirm。
**観測**: router LLM が drop_source tool 認識 (= description が source 名 prompt し示している)、 permission ask UI 表示、 yes 入力で完了、 events log に `index_dropped` event 記録。
**Sample**: N=3。
**予測**: verified 50% / refuted 30% (= LLM が tool invoke せず CLI 案内 text-reply) / inconclusive 15% / blocked 5%。

### S9: Cost preflight gate (= UX gap fix B 観測)

**1-line goal**: `cost_warn_threshold` 超の path を index_docs に渡し、 Phase 1 LLM が cost.threshold_exceeded フラグを見て abort 判断。

**Driver**: reyn.local.yaml で `embedding.cost_warn_threshold: 5` (= 極小値) 設定、 `reyn run index_docs --source large --path "src/reyn/**/*.py" --description "All Reyn Python source"` 実行、 Phase 1 で cost preflight が threshold_exceeded=True になり LLM が `decision: "abort"` 出力、 Phase 2 (= postprocessor) 走らないこと + sources.yaml に entry 不在 confirm。
**観測**: gather_samples + cost_preflight preprocessor が完了、 Phase 1 LLM context に `cost.threshold_exceeded: true` 視認、 LLM 出力 `control.type: "abort"` + reason 言及、 SQLite ファイル不在。
**Sample**: N=3。
**予測**: verified 40% / refuted 40% (= LLM が threshold を ignore して strategy 出力) / inconclusive 15% / blocked 5%。

### S10: CLI smoke (= LLM-free)

**1-line goal**: `reyn source list/describe/rm` CLI が permission gate + audit event 含めて期待動作。

**Driver**: scripts で seed 3 source、 `reyn source list` (= 表 + JSON 両 format)、 `reyn source describe <name>`、 `reyn source rm <name> --yes`、 `reyn source rm <missing>` (= exit 1)、 `reyn source rm <name>` (= no -y、 stdin "n")。
**観測**: CLI 全 subcommand 期待 exit code、 permission gate (= ask default、 --yes で skip)、 events log に `index_dropped` event。
**Sample**: N=3 (= 全 subcommand combination)。
**予測**: verified 90% / refuted 5% / inconclusive 5%。 LLM 介在なしで failure rate 低い。

---

## 6. Aggregate prediction summary

|  | verified | refuted | inconclusive | blocked |
|---|---|---|---|---|
| S1 (N=3) | 80% | 10% | 10% | 0 |
| S2 (N=3) | 60% | 20% | 15% | 5% |
| S3 (N=3) | 55% | 25% | 15% | 5% |
| S4 (N=3) | 50% | 30% | 15% | 5% |
| S5 (N=5) | 45% | 40% | 10% | 5% |
| S6 (N=5) | 30% | 50% | 15% | 5% |
| S7 (N=3) | 80% | 10% | 5% | 5% |
| S8 (N=3) | 50% | 30% | 15% | 5% |
| S9 (N=3) | 40% | 40% | 15% | 5% |
| S10 (N=3) | 90% | 5% | 5% | 0 |
| **mean** | **58%** | **26%** | **12%** | **4%** |

**Headline expectation**: ~58% verified average は batch 14 milestone (= 100% chain 完走) より低いが、 batch 16 (= plan tool 0%) より大幅良い水準。 RAG infra は具体的な user prompt (= "search docs") から tool invoke が motivated されるので、 plan-mode (= meta abstraction tool) より良い見込み。

予測 Brier (= scenario 平均): ~0.40 想定 (= batch 16 = 0.96 から大幅改善見込みだが production-grade phase 1 batch 14 = 0.18 には届かない、 RAG narrative の novelty 込み)。

---

## 7. R-attractor 候補 (= batch 1-16 経験ベース)

| ID | Description | 候補 scenario |
|---|---|---|
| **R-RAG1** | Recall tool invoke 忘れ (= R1-family) | S5, S6 |
| **R-RAG2** | Phase 1 LLM が ChunkStrategy schema 違反 (= enum 外 boundary 等) | S2, S3, S4, S9 |
| **R-RAG3** | drop_source tool invoke 忘れ → CLI 案内 text-reply | S8 |
| **R-RAG4** | Cost threshold ignore (= LLM が gate 認識せず Phase 2 進む) | S9 |
| **R-RAG5** | Multi-source picks 失敗 (= 1 source だけで満足) | S6 |
| **R-RAG6** | Empty state hint hallucinate (= 0 source なのに「memory にあるよ」 等) | S1 |

---

## 8. 並列実行構成

10 sonnet sub-agents、 worktree isolation、 各 agent が 1 scenario 担当 (= S1〜S10)。
LiteLLM proxy + FakeEmbeddingProvider 経由、 共有 state なし。

各 agent が finding を `findings/S<N>-<slug>.md` に書く、 main agent が aggregate して
`findings.md` + `retrospective.md` に統合。

---

## 9. Calibration discipline (= batch 14 から確立)

各 finding は 4 区分 (verified / refuted / inconclusive / blocked)、 修正分類は仕様変更 vs
不具合修正の明示ラベル。 retest は同 batch 内で完結、 carry-over は明示。

ヘッドライン scenario S5 (= recall via chat) で **N=5 で R-RAG1 attractor base rate 測定**
が本 batch の核。 結果次第で:
- 80%+ → production-grade phase 1 milestone (= batch 14 mirror)
- 50-80% → fix wave 候補 (= router system prompt の recall guidance 強化等)
- < 50% → ADR-0033 の structural 課題 (= recall tool description 再設計、 phase 1.5 拡張等)
