# FP-0002: index_docs / recall_docs — 統合ドキュメント検索スキル

**Status**: done (= ADR-0033 Accepted、 Phase 1 landed 2026-05-10、 commit `1e6f153`)
**Proposed**: 2026-05-09
**Landed**: 2026-05-10 (= 12 commits、 d2db332..1e6f153)
**Author**: Research session (eager-shaw-389d9d)
**ADR**: [0033](../decisions/0033-rag-extensible-os.md) (= confirmed design + landed implementation)

---

## Summary

現在の memory 検索はキーワードマッチ + 全件 system prompt インライン展開に留まる。
`index_docs`（チャンク分割 + embedding）と `recall_docs`（catalog filter + semantic top-K）を
stdlib スキルとして実装することで、memory・src・任意ファイルを横断する統合セマンティック検索を実現する。
`recall_memory` の概念は `recall_docs(sources=[{type: "memory"}])` に吸収される。

---

## Motivation

### 現状の制約

```
memory 検索 → keyword substring matching (find_one)
             → 全エントリを system prompt にインライン展開
             → セマンティック検索なし / サイズ上限なし
```

- 言い換え・同義語を完全に見落とす
- セッションが長くなるほど system prompt が肥大化
- docs / src に対する検索手段が存在しない（`recall_docs` は未実装）
- RAG の中核である indexing をコード変更なしにカスタマイズできない

### 設計の核心

> RAG で最も難しい indexing 部分を自然言語（skill.md）で記述・override できるのが差別化ポイント。

LangChain / LlamaIndex はインデックスパイプラインを Python コードで書く。
Reyn では `index_docs` skill の Phase instructions + preprocessor で記述し、
プロジェクト固有のドキュメント構造には skill override だけで対応できる。

---

## Proposed implementation

### 全体構造

```
index_docs  (stdlib skill)
  Phase 1 — strategy   : LLM がサンプルを見てチャンク戦略を決定
  Phase 2 — apply      : Python preprocessor が全ファイルに戦略を適用
                         → embed op でベクトル化 → .reyn/index/ に保存

recall_docs (stdlib skill)
  Phase 1 — retrieve   : Python preprocessor が catalog filter → semantic top-K
  フォールバック       : インデックスなし時は catalog + サイズ上限で直接渡す
```

### ソース種別

`sources` は required（暗黙デフォルトなし）。

| type | パス | チャンク単位 |
|---|---|---|
| `memory` | `.reyn/memory/*.md` | 1エントリ = 1チャンク |
| `src` | `src/**/*.py` 等 | 関数・クラス境界（skill で記述）|
| `files` | 任意パス | Markdown 構造分割（skill で記述）|

**ドキュメント種別ごとの特化実装は skill author が override スキルで行う**:

```
stdlib/index_docs        ← 汎用フレームワーク (stdlib)
project/index_src        ← Python コード特化 (skill author 作)
project/index_design     ← 独自フォーマット特化 (skill author 作)
```

### context 上限対策

ソース全体を1 completion に流すことはしない。

- **`iterate` op**: ファイルリストを取得し、1ファイル = 1 completion でチャンク判断
- **決定論的分割原則**: LLM は戦略を1回決定、適用は Python preprocessor が担当

```
Phase 1 (LLM, 1 completion)
  入力: ファイルリスト + サンプル数件
  出力: ChunkStrategy artifact（boundary_rules, overlap_ratio 等）

Phase 2 (preprocessor + iterate)
  入力: ChunkStrategy
  処理: 全ファイルに適用 → embed op → .reyn/index/ に保存
```

### インデックス保存と P5 / P6

| 保存先 | 内容 |
|---|---|
| `.reyn/index/<source_hash>/` | チャンクベクトル + ChunkMetadata（ファイル保存）|
| WAL | `embed` op 完了 + `content_hash` + `embedding_model` のみ記録 |

ベクトルデータ（数十MB 規模）は WAL（JSONL）に格納しない。
クラッシュリカバリは `content_hash` + `embedding_model` で差分スキップ。

インデックス無効化の2条件:
- `content_hash` 変化 → コンテンツ変更 → 再 embed
- `embedding_model` 変化 → ベクトル空間非互換 → 再 embed

### OS 追加: `ChunkMetadata` モデル

```python
# schemas/models.py に追加
class ChunkMetadata(BaseModel):
    source_path: str          # ファイルパス or memory slug
    source_type: str          # skill が付与するラベル（OS は値を解釈しない）
    content_hash: str         # 変更検知・再インデックス判断
    embedding_model: str      # ベクトル空間の互換性管理
    chunk_index: int          # source 内位置
    size_tokens: int          # コンテキスト予算管理
    parent_context: str | None = None  # heading / class / 関数名（引用用）
    extra: dict = {}          # skill が自由に追加するドメイン固有フィールド
```

OS は `source_type` の値を解釈・分岐しない（P7 準拠）。
カタログフィルタは `recall_docs` スキル側のコードが担う。

### OS 追加: `embed` op

```python
# schemas/models.py
class EmbedIROp(BaseModel):
    kind: Literal["embed"]
    texts: list[str]
    model: str = "text-embedding-3-small"

# op_runtime/registry.py
OP_PURITY["embed"] = OpPurity.external  # WAL キャッシュ対象

# op_runtime/embed.py
async def handle(op: EmbedIROp, ctx: OpContext, ...) -> dict:
    vectors = await embedding_client.embed(op.texts, model=op.model)
    return {"kind": "embed", "vectors": vectors}
```

### recall_docs のフォールバック

```
インデックスあり → catalog filter (ChunkMetadata) → semantic top-K
インデックスなし → catalog enumerate → token 上限でフィルタ → LLM へ直接渡す
```

---

## 未解決の設計判断

| 項目 | 状況 |
|---|---|
| `recall_memory` → `recall_docs` 移行時の `router_system_prompt.py` の扱い | 未定 |

---

## Dependencies

- `src/reyn/schemas/models.py` — `ChunkMetadata` + `EmbedIROp` 追加
- `src/reyn/op_runtime/embed.py` — embed op ハンドラ（新規）
- `src/reyn/op_runtime/registry.py` — `embed` を `OP_KIND_MODEL_MAP` / `OP_PURITY` に追加
- `src/reyn/memory/memory.py` — `recall_docs` 移行後に `find_one()` が legacy 化
- `embedding` ライブラリ（`openai` 等）— 未追加なら新規依存

前提 PR: なし（独立して実装可能）

---

## Cost estimate

**合計: LARGE**

| タスク | コスト | 備考 |
|---|---|---|
| `embed` op 実装 | SMALL | 3 タッチポイント + embedding client |
| `ChunkMetadata` モデル | SMALL | Pydantic モデル追加のみ |
| `.reyn/index/` ストレージ設計 | MEDIUM | ファイル構造 + 差分検知ロジック |
| `index_docs` stdlib スキル | MEDIUM | Phase 1 (strategy) + Phase 2 (iterate + apply) |
| `recall_docs` stdlib スキル | MEDIUM | catalog filter + semantic search + フォールバック |
| `recall_memory` 置き換え | MEDIUM | router との結合が深いため別議論 |

ボトルネックは **ストレージ設計** と **router 移行**。embed op 自体は SMALL。

---

## Related

- `src/reyn/memory/memory.py` — 現行 keyword-only 実装
- `src/reyn/web/routers/a2a.py` — memory 注入の現行フロー参照
- `docs/deep-dives/research/landscape/reyn-strategic-priorities.md` — recall_docs ギャップ記載
- CoALA (arXiv:2309.02427) — Episodic / Semantic / Procedural 分類
- Anthropic Contextual Retrieval (2024) — チャンクへのコンテキスト付加手法
