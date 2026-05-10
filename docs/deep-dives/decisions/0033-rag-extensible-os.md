# ADR-0033: RAG-extensible OS — `embed` / `index_*` / `recall` ops + `index_docs` stdlib + `IndexBackend` protocol

**Status**: Accepted (2026-05-10) — Phase 1 implementation landed
**Track**: Architecture — RAG infrastructure (= 1.0 release narrative core)
**Implementation commits**: `d2db332` (foundational) → `1e6f153` (Wave 2b CLI/Tools); 12 commits total, 2073 → 2204 passed (+131 net new tests). Full chain: schemas + registry → IndexBackend (sqlite) + EmbeddingProvider (LiteLLM) + SourceManifest + embedding config → 5 op handlers + permission gate → router Indexed sources section + empty state hint → index_docs stdlib skill + chunkers + cost preflight → recall/drop_source ToolDefinition + reyn source CLI → user-facing docs (concepts/rag + reference/cli/source).

---

## 1. Context

Reyn 0.x の memory layer は router system prompt への **inline 全件展開** で
"絶対覚えてる" behavior を提供している。 これは効果的だが scale 上限が低く
(= memory 件数 100+ で system prompt 圧迫)、 user の任意ドキュメント (= src
コード / mkdocs / Notion export 等) への retrieval 経路は皆無。

競合 (LangChain / LlamaIndex / MCP RAG servers) は RAG pipeline を Python
コードで構築する pattern が主流。 Reyn 上では skill DSL (= skill.md +
preprocessor + Skill.postprocessor) が既に decomposable な抽象を持っており、
**RAG indexing pipeline を skill.md (= 自然言語 + YAML) で記述・override
できる framework foundation** を 1.0 で ship する判断 (= FP-0002 起源)。

「完成した RAG product を提供」 ではなく、 **「あなたの RAG を skill.md で
書ける OS」** を narrative の核とする positioning。

### 関連 ADR / Plan

- ADR-0023/0024/0025 (plan-mode forward replay / step result spill / sub-loop
  memo) — 既存 idempotency / WAL pattern を embed/index_write で再利用
- ADR-0026 (Unified Tool Registry) — `recall` / `drop_source` を
  ToolDefinition 経由で LLM 直 invoke 可能にする dispatch 経路
- ADR-0029 (mcp_install permission) — `permissions.embed` / `permissions.index_drop`
  の `ask` default + stdlib auto-trust pattern を mirror
- ADR-0030 (universal secret handling) — embedding API key を `~/.reyn/secrets.env`
  + `${VAR}` interpolation 経路で carry
- FP-0002 (`docs/deep-dives/proposals/0002-index-docs-recall-docs.md`) — 起源
  proposal、 status を `proposed` → `in-progress` に更新する
- Plan 詳細: `~/.claude/plans/abstract-knitting-moonbeam.md` の "FP-0002 / ADR-0033"
  entry に confirmed design の全細部記録

---

## 2. Decision

### 2.0 設計不変条件 (= LLM はリストを把握しない)

**LLM context に 100K chunks リスト全体は一度も載らない**。 LLM が見るのは
以下の bounded data のみ:

- **Indexing Phase 1**: samples (= ~10 chunks 抜粋) + summary stats + skill
  input (source / path / description)
- **Indexing Phase 2 (= Skill.postprocessor)**: LLM 不在
- **Recall result 消費 (= caller skill の LLM)**: top-K chunks (= 5-10 件、
  ~5K tokens)

この invariant により attractor surface は構造的にゼロ、 scale 対応は op
handler 層に閉じる (= LLM context 圧迫の経路を持たない)。

### 2.1 Phase 1 (= 1.0 release scope)

#### Op surface (= 5 op + 1 schema)

1. **`embed`** (`OpPurity.external`):
   hybrid inline (`texts: list[str]`) / artifact reference (`input_artifact` +
   `output_artifact`)、 LiteLLM 経由 (= Q3)、 internal stream + batching、
   output_artifact 既存 content_hash skip で idempotent re-run。
   `permissions.embed: ask` default (= ADR-0029 mirror)。

2. **`index_write`** (`OpPurity.side_effect`):
   hybrid inline (`chunks`) / artifact reference (`input_artifact`)、
   IndexBackend.write 経由 (= phase 1 sqlite)、 `mode: append/replace`、
   workspace 内 P5 で permission 不要。

3. **`index_query`** (`OpPurity.world`):
   inline only (= 結果 top-K は小)、 IndexBackend.query 経由、 numpy cosine
   + filters + fallback enumerate (= index 不在時 catalog + size cap)、
   result inline + (args_hash, index_db mtime) で WAL cache OK。

4. **`recall`** (= macro op、 `OpPurity.external`):
   input = query / sources / top_k / filters、 handler が embed →
   iterate(index_query) → merge を sub-op dispatch (= iterate op precedent)、
   ADR-0026 ToolDefinition で LLM 直 invoke 可能。 `recall_docs` skill は
   不要 (= LLM 不在 chain なので op で十分、 phase overhead ゼロ)。

5. **`index_drop`** (`OpPurity.side_effect`):
   input = source name、 IndexBackend.drop + sources.yaml entry 削除 + mem
   cache invalidate + `index_dropped` event emit、 `permissions.index_drop:
   ask` default、 ToolDefinition `drop_source` で LLM 経由 invoke 可能。
   motivation = strategy iteration workflow (= 「sample で試行 → 本番」、
   trial source の cleanup)。

#### Schema (= OS builtin)

**`ChunkMetadata`** (`src/reyn/schemas/models.py`、 op 間 data carrier):

```python
class ChunkMetadata(BaseModel):
    source_path: str                    # generic
    source_type: str                    # 値解釈なし (P7)
    content_hash: str                   # generic
    embedding_model: str                # 互換性 check 用
    chunk_index: int                    # generic
    size_tokens: int                    # generic
    parent_context: str | None = None   # heading / class / 関数名
    extra: dict[str, Any] = {}          # skill 自由領域
```

#### Schema (= skill-level、 NOT OS builtin)

**`ChunkStrategy`** = chunker module-specific (= P7 維持)。 stdlib /
override で field 異なる、 OS は名前だけ知って中身知らない。 同一 artifact
名 `chunk_strategy` で中身だけ override で別物。

#### IndexBackend protocol (= scaling extension point)

```python
# src/reyn/index/backend.py
class IndexBackend(Protocol):
    async def write(source, chunks, mode) -> dict
    async def query(source, query_vector, top_k, filters) -> list[ChunkRecord]
    async def drop(source) -> dict
    async def stat(source) -> dict
```

Phase 1 default and only impl: **`SqliteIndexBackend`** (= stdlib `sqlite3`、
`.reyn/index/<source>/index.db`)。 Phase 2 で Qdrant / FAISS / Weaviate /
Pinecone を `register_backend()` 経由 plugin (= EmbeddingProvider と対称、
LangChain VectorStore / LlamaIndex VectorStoreIndex pattern parity)。
`sources.yaml` 各 entry に `backend: <name>` field 記録。

#### EmbeddingProvider protocol

```python
# src/reyn/embedding/provider.py
class EmbeddingProvider(Protocol):
    async def embed(texts: list[str], model: str) -> list[list[float]]
```

Phase 1 default: **LiteLLM passthrough** (= 既存 LLM 経路と整合、 全主要
provider cover)。 Phase 2 で local (sentence-transformers / ollama) plugin path。

#### Stdlib skill: `index_docs`

構造 = **1 Phase (= LLM strategy 決定) + Skill.postprocessor (= deterministic
chain)**、 既 landed PR-A 機構を full 活用、 「LLM 不在 Phase」 概念は導入せず。

```yaml
# src/reyn/stdlib/skills/index_docs/skill.md
input_schema:
  source: str            # 論理名 (= sources.yaml entry key)
  path: str              # 単一 glob (= "src/**/*.py")
  description: str       # 必須 (= caller LLM / user provide、 retrieval 精度向上)
  mode: append           # default、 replace は明示 escape hatch
entry_phase: strategy
graph:
  strategy: { finish: true }
postprocessor:
  steps:
    - kind: python
      mode: trusted
      module: reyn.stdlib.skills.index_docs.chunkers   # ← override 点
      fn: apply_strategy
      args: { strategy: ${strategy_artifact}, path: ${path}, source: ${source} }
      output_artifact: artifacts/chunks.jsonl
    - kind: embed
      input_artifact: artifacts/chunks.jsonl
      output_artifact: artifacts/chunks_with_vectors.jsonl
    - kind: index_write
      source: ${source}
      input_artifact: artifacts/chunks_with_vectors.jsonl
      mode: ${mode}
permissions:
  embed: allow
  python.trusted: allow
  file.read: { paths: [${path}] }
```

**呼び出し粒度 = strategy 単位**: 1 invocation = 1 source = 1 path = 1
chunker = 1 strategy。 mixed file types を 1 invocation に詰めない (= Phase
1 strategy 決定が発散 + chunkers.py interface 複雑化)。 logical grouping は
recall 時 `sources=[...]` param で合成。

#### Override pattern (= 差別化の核)

```yaml
# reyn/project/index_python_src/skill.md
extends: stdlib/index_docs

phases:
  strategy:
    instructions_override: |
      Python AST chunking — function / class 境界で分割...

  # postprocessor の python step を別 module に差し替え
postprocessor:
  steps:
    - kind: python
      module: reyn.project.index_python_src.ast_chunkers
      fn: apply_strategy
```

Phase 1 LLM の strategy 生成 = **P4 機構流用** (= candidate_outputs schema
injection で enum + description が LLM context、 hallucination 不可)、 Phase
1 instructions で semantic guidance、 schema validation で enum 外 reject。

#### Source manifest

`.reyn/index/sources.yaml` (= file SSoT、 process 跨ぎ persistence) +
`SourceManifest` per-process mem cache singleton (= startup load + index_docs
完了 hook で atomic file write & mem cache update + recall / system prompt
builder は mem cache から read)。 動的 indexing → query loop は per-turn
system prompt rebuild で reflect。 multi-process は phase 2 で file mtime
poll + lock 追加。

```yaml
# .reyn/index/sources.yaml
my_project_code:
  description: "My e-commerce backend (Python + FastAPI)"
  path: "myproj/**/*.py"
  backend: sqlite
  last_indexed: 2026-05-10T14:32:00Z
  chunk_count: 1247
  embedding_model: "text-embedding-3-small"
```

#### Router system prompt (= 「Indexed sources」 section)

```
## Indexed sources (3 available)

- **memory** — User notes / past session memos (142 chunks)
- **reyn_code** — Reyn Python framework code (1247 chunks)
- **reyn_docs** — Reyn bundled mkdocs documentation (89 chunks)

Use the `recall` tool with `sources=[<name>, ...]` to search.
```

Empty state (= 0 source) では getting-started hint inject (= 「No indexed
sources yet. Try `reyn run index_docs --source memory --path ...`」)。

#### `reyn.yaml` `embedding:` section

```yaml
embedding:
  default_class: standard
  classes:
    light:    openai/text-embedding-3-small
    standard: openai/text-embedding-3-small
    strong:   openai/text-embedding-3-large
  batch_size: 100
  max_concurrent_batches: 1     # phase 2 で increase
  max_retries: 3
  retry_backoff: exponential
  tokenizer: cl100k_base        # phase 1 single
  cost_warn_threshold: 10000    # chunk 数閾値、 超で ask_user gate
```

API key は ADR-0030 secrets.env 経由 (= `OPENAI_API_KEY` 等)、 `${VAR}`
interpolation で yaml 内に declarative 記述可。 default 完備で `pip install
reyn` 後 OPENAI_API_KEY 設定だけで動く。

#### CLI

- `reyn source list` — 全 indexed sources 表示
- `reyn source rm <name>` — 削除 (= ask permission、 内部 `index_drop` op invoke)
- `reyn source describe <name>` — 詳細 (= chunk 数 / model / 最終 update)

#### UX gap fix (= phase 1 で塞ぐ 5 件)

A. **Empty state UX**: source 0 件時の system prompt に getting-started hint
B. **Cost preflight**: index_docs Phase 1 preprocessor で chunk count 推定 →
   `cost_warn_threshold` 超で ask_user gate
C. **Progress feedback**: postprocessor python step + embed op handler が
   outbox status messages emit (= "Embedded 5K/100K chunks (5%)、 ETA 25 min")
D. **Concurrent lock**: source-level advisory lock (= `.reyn/index/<source>/.lock`)、
   同 source への並行 invocation を queue / reject
E. **DB corruption recovery hint**: recall / index_query で sqlite read 例外時、
   actionable error message (= 「source corrupted, run `reyn source rm <name>`
   & re-index」)

### 2.2 Phase 1.5 (= 1.1+、 post-1.0 別 wave)

memory layer の inline → `recall(sources=["memory"])` 切替、 `recall_memory`
legacy tool wrapper (= backward compat)、 dogfood retest blocker (= memory
recall behavior 退化なし confirm、 失敗時は 1.1 release postpone で 1.0 安全)。
**dual-mode (= short memo inline / long content indexed)** も検討候補。

### 2.3 Phase 2 (= post-1.1)

- src / docs の advanced support (= file change watcher、 incremental reindex)
- alternative IndexBackend plugins (= Qdrant / FAISS / Weaviate / Pinecone、
  `register_backend()` 経由)
- sqlite-vec / sqlite-vss extension (= sqlite backend 1M+ chunks)
- sub-record cache (= Q1 c hybrid、 cross-skill optimization)、 cross-skill
  cache scope key
- sources.yaml `per_source.backend` 自由化、 backend migration tool (=
  `reyn source migrate <name> --to qdrant`)
- per-class tokenizer / local embedding plugin (= sentence-transformers / ollama)
- advanced retrieval (= rerank / HyDE / contextual retrieval / hierarchical)
- RAG eval framework

---

## 3. WAL invariant

embed / index_write / index_drop は WAL に **events (= started/completed +
summary stats) のみ**、 vectors / chunks / vector blobs は持たない。 大
payload は workspace artifact (= JSONL / SQLite) が SSoT、 crash recovery は
workspace 経由 idempotent re-run (= output_artifact の content_hash dedup +
SQLite content_hash dedup)。

既存 ADR-0023/0024/0025 + R-D16 全 backward-compat、 LLMReplay は LiteLLM 層
で record/playback (= WAL 拡張なし)。

→ Q1 demerit 5 件 (sub-record / spill / truncation / LLMReplay 拡張 / disk
bloat) を phase 1 で全 sidestep。 phase 2 で cross-skill cache demand 観測後
に sub-record 導入判断。

---

## 4. Consequences

### Desirable

- **OSS 1.0 release narrative**: 「framework foundation」 として明確な
  positioning、 LangChain / LlamaIndex の matur/y ecosystem と直接比較
  されない controlled scope
- **Skill DSL 差別化** = HN タイトル / blog post で 1 行表現可
- **既存 OS infra reuse**: PR-A Skill.postprocessor / ADR-0026 unified
  registry / ADR-0023/0024/0025 plan-mode persistence / ADR-0029 permission
  / ADR-0030 secrets — 新 architectural decision を最小化
- **memory layer 不変** (= 1.0 で behavior 変化ゼロ、 既存 0.x user の
  regression risk なし)、 migration risk を Phase 1.5 に局所化
- **scaling extension path** = IndexBackend protocol で phase 2 backend
  plugin 自由化、 1M+ chunks scale enterprise 用件にも応答可能 (= phase 2)
- **P7 維持**: ChunkStrategy が skill-level、 OS は形だけ知る (= ChunkMetadata
  / ToolDefinition と同 pattern)

### Undesirable

- **Phase 1 のみで mature RAG product としては不足** (= advanced retrieval
  / RAG eval / IDE integration / vector store variety なし)、 narrative
  misread risk あり → README で「foundation」 強調必須
- **First-time user 体験は indexing 必要** (= 「触れるけど使えない」 risk →
  empty state hint + cost preflight + progress feedback で緩和)
- **Embedding API 依存** (= phase 2 まで local embedding なし、 オフライン
  / privacy 用件で見劣り)
- **Phase 1.5 migration risk** (= memory inline → fetch 化で behavior 退化
  observed risk、 失敗時 1.1 postpone で 1.0 safe を保証)
- **Sensitive data redaction policy 不在** (= R-D9 wave で phase 2+、 doc
  で warn のみ)

---

## 5. Alternatives considered

### Alt 1: Phase 1 で memory migration も同梱

却下: dogfood retest 失敗 = 1.0 release postpone、 release timing risk 大。
2 段 phasing (= 1.0 framework / 1.1 migration) で risk 局所化。

### Alt 2: ChunkStrategy を OS builtin schema に

却下: P7 違反 (= "boundary" / "max_chunk_size_tokens" 等 indexing-specific
vocabulary)、 override 柔軟性失われる。 ChunkMetadata と異なり ChunkStrategy
は 1 skill 内部 artifact なので skill-level で十分。

### Alt 3: `recall_docs` を skill として実装

却下: LLM 不在 deterministic chain なので op で十分、 skill machinery の
phase overhead ゼロ。 user 指摘で macro op に collapse、 recall_docs skill
廃止。

### Alt 4: per-text WAL cache (= Q1 c hybrid) を Phase 1 で導入

却下: sub-record / spill / truncation / LLMReplay 拡張の 5 件 demerit、
architectural cost 大。 Phase 1 は WAL cache せず workspace idempotent
re-run で carry、 phase 2 で cross-skill demand 観測後判断。

### Alt 5: 単一 vector store hardcode (= IndexBackend protocol なし)

却下: scaling extension path を将来塞ぐ、 競合 LangChain VectorStore /
LlamaIndex VectorStoreIndex pattern parity を捨てる、 enterprise 1M+ chunks
用件で詰む。 Phase 1 sqlite only ship + protocol 抽象で +0.5 day cost、
将来全 RAG layer を future-proof。

### Alt 6: deterministic-only chunker (= Phase 1 LLM 削除)

却下: adaptive strategy decision の novelty 失われる、 narrative 弱る。
Phase 1 LLM call cost は 1 invocation あたり ~$0.0003 (= 無視可)、
override で `instructions_override` で trivial 化 path も用意済。

---

## 6. Acceptance criteria (= 1.0 release blocker)

- [ ] ADR-0033 status: Accepted (= 本 ADR finalize)
- [ ] FP-0002 status: done (commit 記録)
- [ ] 5 op (embed/index_write/index_query/recall/index_drop) + ChunkMetadata
      schema が op_runtime / schemas に landed
- [ ] IndexBackend protocol + SqliteIndexBackend + EmbeddingProvider +
      LiteLLM impl が landed
- [ ] SourceManifest singleton + sources.yaml file SSoT + per-turn router
      system prompt rebuild が動作
- [ ] index_docs stdlib skill (= 1 Phase + postprocessor + chunkers.py +
      chunk_strategy.yaml) が landed
- [ ] `recall` / `drop_source` ToolDefinition が unified registry に登録、
      LLM 直 invoke 可能
- [ ] `reyn source {list,rm,describe}` CLI 動作
- [ ] `reyn.yaml` `embedding:` section parse + default 完備
- [ ] UX gap fix 5 件 (= empty state / cost preflight / progress / concurrent
      lock / corruption hint) landed
- [ ] Tier 2 tests (= per-op ~5 tests × 5 = ~25) + Tier 3 LLMReplay (=
      index_docs e2e fixture) pass
- [ ] mkdocs strict pass、 新 doc page (= `docs/concepts/rag.md` (+ja) +
      `docs/reference/cli/source.md` (+ja)) landed
- [ ] memory inline behavior 不変 confirm (= 既存 dogfood scenario 1 retest)
- [ ] Manual dogfood: Reyn 自身の src/docs を index、 recall query で動作確認
- [ ] LiteLLM proxy 経由 (= `${LITELLM_API_BASE}`) で動作 confirm

---

## 7. References

- FP-0002: `docs/deep-dives/proposals/0002-index-docs-recall-docs.md`
- Plan: `~/.claude/plans/abstract-knitting-moonbeam.md` (= "FP-0002 / ADR-0033"
  entry に確定済 design 全細部)
- ADR-0023: plan-mode forward replay (= idempotency pattern reuse)
- ADR-0024: plan step result spill (= JSONL artifact pattern reuse)
- ADR-0026: unified tool registry (= recall / drop_source ToolDefinition)
- ADR-0029: mcp_install permission (= ask default + auto-trust pattern)
- ADR-0030: universal secret handling (= API key carry)
- CoALA (arXiv:2309.02427) — Episodic / Semantic / Procedural classification
- Anthropic Contextual Retrieval (2024) — Phase 2 enhancement candidate
- LangChain VectorStore / LlamaIndex VectorStoreIndex — IndexBackend pattern parity
