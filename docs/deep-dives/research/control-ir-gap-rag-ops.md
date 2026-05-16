---
type: research
topic: control-ir / RAG ops documentation gap
status: stable
last_updated: 2026-05-17
audience: [writer, implementer]
---

# Control IR Gap Analysis — ADR-0033 RAG ops 未記載問題

## Summary

`docs/reference/runtime/control-ir.md` および `.ja.md` に、ADR-0033 (RAG-extensible OS) で
実装された 5 つの op が記載されていない。
English 版は 5 op 欠落、Japanese 版はさらに `mcp_install` も欠落しており計 6 op。

## Gap 一覧

| op kind | `OP_KIND_MODEL_MAP` | control-ir.md | control-ir.ja.md |
|---|---|---|---|
| `embed` | ✅ (ADR-0033) | ❌ | ❌ |
| `index_write` | ✅ (ADR-0033) | ❌ | ❌ |
| `index_query` | ✅ (ADR-0033) | ❌ | ❌ |
| `recall` | ✅ (ADR-0033) | ❌ | ❌ |
| `index_drop` | ✅ (ADR-0033) | ❌ | ❌ |
| `mcp_install` | ✅ | ✅ | ❌ |

**Source of truth**: `src/reyn/op_runtime/registry.py` — `OP_KIND_MODEL_MAP` が全 op 種別の
正規リスト。CLAUDE.md ルール: `control-ir.md` は `OP_KIND_MODEL_MAP` と常に同期が必要。

## 調査根拠

- `registry.py` の `OP_KIND_MODEL_MAP` には 17 op kinds が列挙されている。
- `control-ir.md` の section header を確認したところ RAG ops 5 種が欠落。
- `control-ir.ja.md` は `mcp_install` セクションも存在しない（English 版には存在）。
- 各 op の schema / permission は `src/reyn/schemas/models.py` と各 `op_runtime/*.py` で確認済み。

---

## Writer 向け: 追加コンテンツ案

以下は `control-ir.md` の "## `mcp_install`" セクションの直後に挿入する 5 セクションの
proposed content。Writer はこれをそのまま貼り付けて体裁を整えてください。

---

### `embed`

テキストを埋め込みベクトルに変換する（ADR-0033 §2.1）。2 つの入力形式がある。

**Form A — inline** (小ペイロード、クエリ埋め込みなど):

```json
{
  "kind": "embed",
  "texts": ["What is Reyn?", "How does the OS work?"],
  "model": "standard"
}
```

返値: `{"vectors": [[0.1, ...], [0.2, ...]], "model": "text-embedding-3-small"}`

**Form B — artifact reference** (大ペイロード、インデックス構築など):

```json
{
  "kind": "embed",
  "input_artifact": "chunks.jsonl",
  "text_field": "text",
  "output_artifact": "chunks_embedded.jsonl",
  "model": "standard"
}
```

返値: `{"embedded_count": 1024, "skipped_count": 0}`

`model` は `reyn.yaml` の `embedding.classes` マップ内のエントリ名、または
`"openai/text-embedding-3-small"` のような直接指定。デフォルト `"standard"` は
`reyn.yaml` で解決される。

**Permission**: なし（外部 API 呼び出しだが現在は permission gate なし）。
トークンコストは発生する（OpPurity: `external`）。

---

### `index_write`

チャンク（テキスト + ベクトル + メタデータ）をインデックスバックエンドに書き込む
（ADR-0033 §2.1）。2 つの入力形式がある。

**Form A — inline**:

```json
{
  "kind": "index_write",
  "source": "my_docs",
  "chunks": [
    {"text": "Reyn is an agent OS", "vector": [0.1, ...], "metadata": {"file": "intro.md"}}
  ],
  "mode": "append",
  "description": "Project documentation",
  "path": "docs/**/*.md"
}
```

**Form B — artifact reference** (`embed` Form B の出力を直接受け取る):

```json
{
  "kind": "index_write",
  "source": "my_docs",
  "input_artifact": "chunks_embedded.jsonl",
  "mode": "replace",
  "description": "Project documentation",
  "path": "docs/**/*.md"
}
```

`mode: replace` はソース全体を置換し、`append` は追記する。

`description` / `path` を指定すると `SourceManifest` に記録され、
ルーターシステムプロンプトの "Indexed sources" セクションに反映される
（LLM が `recall` 対象を正しく選択するために必要）。

**Permission**: なし（ワークスペース内部 write — P5 の `.reyn/` デフォルトゾーン）。

---

### `index_query`

単一ソースに対してセマンティック検索を実行する（ADR-0033 §2.1）。
直接使用よりも `recall` マクロ op 経由が推奨される。

```json
{
  "kind": "index_query",
  "source": "my_docs",
  "query_vector": [0.1, 0.2, ...],
  "top_k": 5,
  "filters": {"file_type": "md"}
}
```

`query_vector` を省略すると enumerate fallback（空リストを返す）。

返値: `{"chunks": [{"text": "...", "score": 0.92, "metadata": {...}}, ...], "mode": "semantic"}`

**Permission**: なし（読み取り専用、OpPurity: `world`）。

---

### `recall`

`embed` + `index_query` を束ねたマクロ op（ADR-0033 §2.1）。
複数ソースを横断して上位 K チャンクをマージする。
単一の Control IR op として使用でき、サブ op ごとに P6 イベントが記録される。

```json
{
  "kind": "recall",
  "query": "How does crash recovery work in Reyn?",
  "sources": ["reyn_code", "my_docs"],
  "top_k": 5,
  "embedding_model": "standard",
  "filters": {}
}
```

返値: `{"chunks": [{"text": "...", "score": 0.95, "source": "reyn_code", ...}, ...], "sources_queried": 2}`

`sources` は必須（デフォルトなし）。複数ソースのスコアは比較可能ではないため、
返値はスコア降順でソートされるが異なるソース間では参考値として扱うこと。

**Permission**: なし（サブ op の `embed` / `index_query` が個別に処理、OpPurity: `external`）。

---

### `index_drop`

インデックスソースを完全に削除する（ADR-0033 §2.1）。破壊的操作のため同意ゲートあり。

```json
{
  "kind": "index_drop",
  "source": "my_docs"
}
```

`source` のすべてのチャンクとバックエンドエントリを削除する。元に戻せない。

**Permission**: スキルフロントマターに `permissions.index_drop: true` が必要。
設定で `permissions.index_drop: deny` とすると全スキルで無効化できる。

---

## `control-ir.ja.md` 向け追加コンテンツ

上記 5 ops の日本語版セクションは以下の通り。

また `.ja.md` には `mcp_install` セクションも存在しないため、
English 版から翻訳して追加する必要がある（別途 writer 判断）。

---

### `embed`（日本語）

テキストを埋め込みベクトルに変換する（ADR-0033 §2.1）。

**Form A — インライン**（小ペイロード）:

```json
{"kind": "embed", "texts": ["Reyn とは？"], "model": "standard"}
```

返値: `{"vectors": [[0.1, ...]], "model": "text-embedding-3-small"}`

**Form B — artifact 参照**（大ペイロード）:

```json
{
  "kind": "embed",
  "input_artifact": "chunks.jsonl",
  "text_field": "text",
  "output_artifact": "chunks_embedded.jsonl",
  "model": "standard"
}
```

`model` は `reyn.yaml` の `embedding.classes` エントリ名またはモデル直接指定。

**Permission**: なし（OpPurity: `external`）。

---

### `index_write`（日本語）

チャンク（テキスト + ベクトル + メタデータ）をインデックスバックエンドへ書き込む（ADR-0033 §2.1）。

```json
{
  "kind": "index_write",
  "source": "my_docs",
  "chunks": [{"text": "...", "vector": [...], "metadata": {}}],
  "mode": "append",
  "description": "プロジェクトドキュメント",
  "path": "docs/**/*.md"
}
```

artifact 参照形式（Form B）: `input_artifact` フィールドで JSONL パスを指定。

`description` / `path` を設定するとシステムプロンプトの "Indexed sources" に反映される。

**Permission**: なし（ワークスペース内部 write）。

---

### `index_query`（日本語）

単一ソースに対するセマンティック検索（ADR-0033 §2.1）。通常は `recall` を使用する。

```json
{"kind": "index_query", "source": "my_docs", "query_vector": [...], "top_k": 5}
```

**Permission**: なし（読み取り専用）。

---

### `recall`（日本語）

`embed` + `index_query` をまとめたマクロ op（ADR-0033 §2.1）。複数ソース横断検索。

```json
{
  "kind": "recall",
  "query": "クラッシュ回復はどう動くか？",
  "sources": ["reyn_code", "my_docs"],
  "top_k": 5
}
```

**Permission**: なし（OpPurity: `external`）。

---

### `index_drop`（日本語）

インデックスソースを完全削除する（ADR-0033 §2.1）。破壊的操作。

```json
{"kind": "index_drop", "source": "my_docs"}
```

**Permission**: スキルフロントマターに `permissions.index_drop: true` が必要。

---

## 影響範囲

この gap によって生じうる問題:
1. **スキル作成者が RAG ops を使う方法を知れない**: `control-ir.md` が参照先のため、
   index_docs スキルや recall_memory スキルを模倣したカスタムスキルを書けない。
2. **`allowed_ops` 申告漏れ**: スキルフロントマターで `allowed_ops` を正しく申告できない
   （`index_drop` は permission gate があるため特に重要）。
3. **`control-ir.md` と `OP_KIND_MODEL_MAP` の同期ルール違反**: CLAUDE.md の
   「`control-ir.md` must stay synced with `OP_KIND_MODEL_MAP`」ルールに抵触している。

## 推奨アクション

- **Writer**: 上記 proposed content を `control-ir.md`（`## mcp_install` の後）および
  `control-ir.ja.md`（同位置、`mcp_install` セクションも追加）に追記する。
- **実装者**: 追加不要（ops は実装済み、gap はドキュメントのみ）。
- **Issue**: このドキュメントをもとに GitHub issue を発行して追跡する（下記参照）。

## 関連

- `src/reyn/op_runtime/registry.py` — `OP_KIND_MODEL_MAP` 正規リスト
- `src/reyn/schemas/models.py` — IROp スキーマ定義
- `src/reyn/op_runtime/embed.py` / `index_write.py` / `index_query.py` / `recall.py` / `index_drop.py`
- ADR-0033: RAG-extensible OS
- `docs/deep-dives/research/doc-improvement-proposals.md` — 既存 doc gap リスト
