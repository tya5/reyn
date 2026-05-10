# S3: Index Reyn docs (medium scale) — finding doc

| フィールド | 値 |
|---|---|
| Date | 2026-05-10 |
| main HEAD | `62fd21b` |
| Agent | `agent-a44a325d66db891eb` |
| Driver | `scripts/s3_driver.py` (programmatic) |
| Sample size | N=3 |
| Target | `docs/concepts/*.md` (42 files、 `.ja.md` 含む) |
| **verdict 分布** | verified: 3/3 (100%、ただし bugs 発見 + workaround 適用後) |
| **判定** | **verified** (boundary=heading 3/3、chunks > 30、parent_context 設定確認) |

---

## 1. サマリー表

| Run | Status | Boundary | max_chunk_size | fresh chunk_count | SQLite count | parent_ctx? | sources.yaml? |
|---|---|---|---|---|---|---|---|
| 1 | finished | heading | 600 | 299 | 425 | YES | YES |
| 2 | finished | heading | 600 | 299 | 425 | YES | YES |
| 3 | finished | heading | 800 | 417 | 425 | YES | YES |

**注意**: SQLite count 425 は artifact ファイル (`chunks_with_vectors.jsonl`) の累積書込みによる。
driver が `.reyn/` を clean するが `artifacts/` は clean しないため、
前の run の embedded chunks が再利用される (embed idempotency が働く)。
fresh chunk_count = `chunk_stats.chunk_count` が真の per-run chunk 数。

---

## 2. Per-run 詳細

### Run 1

```
boundary              : heading
max_chunk_size_tokens : 600
min_chunk_size_tokens : 100
overlap_ratio         : 0.1
preserve_parent_context: True
chunk_stats:
  chunk_count         : 299
  source_lock_acquired: True
  chunks_path         : artifacts/chunks.jsonl
embed_result:
  embedded_count      : 0
  skipped_count       : 299
index_result:
  written             : 425
  skipped             : 1
elapsed               : 6.7s
parent_ctx samples    : ["See also", "Why", "Symmetry with docs",
                         "Workspace に格納されるもの", "What is MCP"]
```

### Run 2

```
boundary              : heading
max_chunk_size_tokens : 600
min_chunk_size_tokens : 100
overlap_ratio         : 0.1
preserve_parent_context: True
chunk_stats:
  chunk_count         : 299
  source_lock_acquired: True
embed_result:
  embedded_count      : 0
  skipped_count       : 299
index_result:
  written             : 425
  skipped             : 1
elapsed               : 6.1s
parent_ctx samples    : ["Type B — 意図的な役割分離", "2 つのレイヤー",
                         "See also", "Type A — 健全な対称性", "Indexing 戦略"]
```

### Run 3

```
boundary              : heading
max_chunk_size_tokens : 800
min_chunk_size_tokens : 50
overlap_ratio         : 0.1
preserve_parent_context: True
chunk_stats:
  chunk_count         : 417
  source_lock_acquired: True
embed_result:
  embedded_count      : 0
  skipped_count       : 417
index_result:
  written             : 425
  skipped             : 1
elapsed               : 6.6s
parent_ctx samples    : ["2. チャットを開始する — LLM は必要に応じて chunk を recall する",
                         "Landscape からの具体例", "非同期 dispatch",
                         "Limitations", "3. Capability comparison matrix"]
```

---

## 3. Observation point 評価

### G1: Phase 1 LLM が `boundary: heading` を選択

✅ **verified** — 3/3 run で `boundary=heading`。 Markdown 構造 (= heading ベースのドキュメント群)
から LLM が適切に推論した。 attractor 強度 3/3 = 100%。

### G2: chunks count > 30

✅ **verified** — fresh chunk_count は 299〜417 (42 ファイル、 max_chunk_size 依存)。
SQLite に最終 425 chunks 書込み。 medium scale 動作確認済み。

### G3: chunks の `parent_context` 設定 (boundary=heading 時)

✅ **verified** — SQLite の `parent_context` column に heading ラベルが格納されていることを
確認。 サンプル: "See also", "Why", "Type B — 意図的な役割分離", "Indexing 戦略" 等。
日本語 heading も正しく記録。

### G4: SQLite `<workspace>/.reyn/index/reyn_docs/index.db` 作成

✅ **verified** — 全 run で確認。

### G5: `sources.yaml` エントリ作成 + `chunk_count` > 30

✅ **verified** — `chunk_count: 425` で entry 作成。 ただし description/path が fallback 値
(= B17-S3-2 参照)。

---

## 4. 発見した bugs

### B17-S3-1: postprocessor_executor が `data` を outer schema に validate (CRITICAL for pipeline)

**症状**: Postprocessor 最終検証で `'data' is a required property; 'type' is a required property`
エラー。 Pipeline は steps 全完了後に崩壊する。

**根本原因**: worktree の `postprocessor_executor.py` が `result.get("data", {})` を
`postprocessor.output_schema` (outer `{type, data}` 形式) に validate。
outer schema に inner data dict を渡すため `type` と `data` が required として失敗。

**main repo 状態**: 未コミットの fix が存在 (lines 286-310 を参照)。

```
# worktree (バグ)
data = result.get("data", {})
validator = jsonschema.Draft7Validator(postprocessor.output_schema)
errors = sorted(validator.iter_errors(data), key=str)

# main repo fix (正)
# rename artifact type
if postprocessor.output_name:
    result = dict(result, type=postprocessor.output_name)
# validate full result
validator = jsonschema.Draft7Validator(postprocessor.output_schema)
errors = sorted(validator.iter_errors(result), key=str)
```

**影響**: postprocessor を使う全 skill (= index_docs) が schema validation で必ず fail。

**修正**: worktree の `src/reyn/kernel/postprocessor_executor.py` に main repo fix を適用済み
(S3 driver 実行のため)。 main repo の uncommitted 変更としてコミット待ち。

### B17-S3-2: `index_write` op が description/path を `sources.yaml` に保存しない (MED)

**症状**: `sources.yaml` の `description` が `"Index of source 'reyn_docs'"` (fallback)、
`path` が `"(unknown)"` (fallback)。 実際の input "Reyn concept documentation" /
"docs/concepts/*.md" が失われる。

**根本原因**: `IndexWriteIROp` が `description` / `path` フィールドを持たないため、
`index_write` handler が `manifest.get(source)` で既存エントリを試みるが、
初回書込みでは未登録なので fallback に落ちる。

**修正候補**: `apply_strategy` postprocessor step でのみアクセス可能な description/path を
`index_write` op に渡す仕組みが必要 (= `args_from` 拡張、または SourceManifest を
`apply_strategy` python step 内で upsert するアプローチ)。

**影響**: `reyn source list/describe` で description/path が表示されない。 索引の発見可能性が
低下。 LOW severity for correctness (chunks は正しく書込まれる)、MED for UX。

### B17-S3-3: embed op が `REYN_EMBEDDING_PROVIDER` env var を参照しない (HIGH)

**症状**: `REYN_EMBEDDING_PROVIDER=fake` を設定しても worktree の `embed.py` が
`get_provider("litellm", config={})` をハードコードしているため FakeEmbeddingProvider
が使われず、litellm API 呼び出しが発生して認証エラー。

**根本原因**: main repo には未コミットの fix:
```python
# main repo fix
_provider_name = _os.environ.get("REYN_EMBEDDING_PROVIDER", "litellm")
provider = get_provider(_provider_name, config={})
```
が存在するが worktree (= committed HEAD `62fd21b`) にはない。

**修正**: worktree の `src/reyn/op_runtime/embed.py` に main repo fix を適用済み
(S3 driver 実行のため)。 main repo の uncommitted 変更としてコミット待ち。

---

## 5. 観測した注目点

### 5-1. boundary 選択の attractor

3/3 run で `boundary=heading`。 LLM (gemini-2.5-flash-lite) が Markdown
structure hint ("Markdown with headings") から consistent に heading を選択。

**R-RAG2 attractor** (= Phase 1 LLM が ChunkStrategy schema 違反) は発生しなかった。

### 5-2. max_chunk_size_tokens の variation

600 / 600 / 800 と run ごとに異なる (= LLM の stochastic 選択)。
chunk_count に影響: 600 → 299 chunks、800 → 417 chunks。
LLM は "600〜800 が技術文書に適切" と判断している。

### 5-3. embed_result.embedded=0 の謎

全 run で `embedded_count=0, skipped_count=299〜417`。 これは driver が
`.reyn/` を clean するが `artifacts/chunks_with_vectors.jsonl` を clean
しないため、前 run の vectors が再利用される。 embed op の idempotency
(content_hash チェック) が正しく機能している証拠でもある。

**本来の観察**: fresh workspace での embedded_count は chunk_count と等しいはず。
driver の `clean_state()` 関数に `artifacts/` 削除を追加すべき (driver bug)。

### 5-4. SQLite count vs chunk_stats.chunk_count の不一致

SQLite count (425) > chunk_stats.chunk_count (299/417)。
累積 artifact による artifacts dir の汚染。同上。

### 5-5. sources.yaml の description/path fallback (B17-S3-2)

UX として「どのパスをどの説明でインデックスしたか」が sources.yaml から
確認できない。 `reyn source describe reyn_docs` が "Index of source 'reyn_docs'"
を返す問題。

---

## 6. Verdict

**verified** — ただし 3 つの bugs 修正後:

| 観測ポイント | 結果 |
|---|---|
| boundary=heading | ✅ 3/3 |
| chunk_count > 30 | ✅ 299〜417 (fresh) |
| parent_context 設定 | ✅ heading labels 確認 |
| SQLite populated | ✅ 425 chunks |
| sources.yaml entry | ✅ (description fallback bug あり) |

---

## 7. Calibration delta

prelude S3 予測:

| 予測 | 実際 |
|---|---|
| verified: 55% | 100% (3/3、bugs 修正後) |
| refuted: 25% | 0% |
| inconclusive: 15% | 0% |
| blocked: 5% | 0% (bugs あるが workaround で継続) |

**予測 vs 実際のギャップ**: verified 率が予測 (55%) を大幅に上回った (100%)。
主因: R-RAG2 (boundary enum 外) が全く発生しなかった。 LLM が Markdown structure hint
から heading を一貫して選択する attractor が強かった。

Brier score 参考 (verified 予測 0.55):
```
B = (0.55 - 1.0)^2 + (0.25 - 0.0)^2 + (0.15 - 0.0)^2 + (0.05 - 0.0)^2
  = 0.2025 + 0.0625 + 0.0225 + 0.0025
  ≒ 0.288
```

---

## 8. Bug サマリー

| ID | 重要度 | 内容 |
|---|---|---|
| **B17-S3-1** | **HIGH** | `postprocessor_executor` が inner data を outer schema で validate → type=finish 全 skill で必ず fail。 main repo に未コミット fix あり |
| **B17-S3-2** | MED | `index_write` が description/path を sources.yaml に保存しない (fallback "Index of source X") → UX 劣化 |
| **B17-S3-3** | HIGH | `embed.py` が `REYN_EMBEDDING_PROVIDER` env var を参照しない (hardcode litellm) → dogfood で fake provider 使用不可。 main repo に未コミット fix あり |

### 未コミット修正 (main repo uncommitted) について

B17-S3-1 と B17-S3-3 の fix は main repo の working tree に存在するが
未コミット。 これらは batch 17 前の先行 dogfood run で発見・修正されたと推定
(commit message は "Dogfood batch 17 S2" を言及)。

**Action required**: 3 ファイルの uncommitted fix を commit して worktree に取り込む。
- `src/reyn/kernel/postprocessor_executor.py`
- `src/reyn/op_runtime/embed.py`
- `src/reyn/stdlib/skills/index_docs/artifacts/index_summary.yaml`

---

## 参照

- `src/reyn/stdlib/skills/index_docs/skill.md` — index_docs skill 定義
- `src/reyn/stdlib/skills/index_docs/phases/strategy.md` — Phase 1 instructions
- `src/reyn/stdlib/skills/index_docs/chunkers.py` — chunking implementation
- `src/reyn/kernel/postprocessor_executor.py` — postprocessor executor (B17-S3-1)
- `src/reyn/op_runtime/embed.py` — embed op handler (B17-S3-3)
- `src/reyn/op_runtime/index_write.py` — index_write handler (B17-S3-2)
- `scripts/s3_driver.py` — S3 driver script
- `scripts/s3_raw_findings.json` — raw per-run JSON data
