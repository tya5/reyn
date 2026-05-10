---
type: concept
topic: rag
audience: [human, agent]
---

# RAG（Retrieval-Augmented Generation）

reyn は RAG **framework foundation** を提供します — 5 つの primitive op、 拡張可能な `IndexBackend` protocol、 `EmbeddingProvider` protocol、 stdlib `index_docs` skill。 任意のドキュメントコーパスを index し、 クエリ時に LLM が関連する chunk を取得できます。コーパス全体をコンテキストウィンドウに展開する必要はありません。

**差別化: skill-driven indexing.** LangChain や LlamaIndex は Python pipeline を提供しますが、 reyn は `skill.md` を提供します。 postprocessor chain の python step 1 つを差し替えるだけで chunker を per-source で override 可能。 Phase 1 では LLM が chunking 戦略を選びますが、 trainging memory からの open-ended な選択ではなく、 戦略 skill で defined された closed candidate set から選びます。

**Phase 1 scope (= 1.0 release)** で出荷されるのは framework foundation、 SQLite default backend (≤100K chunks、 sub-second query)、 LiteLLM embedding passthrough、 stdlib `index_docs` skill。 vector store plugin variety (Qdrant / FAISS / Weaviate / Pinecone)、 advanced retrieval (rerank / HyDE / contextual retrieval)、 RAG eval framework、 IDE integration は post-1.0 (= phase 2) territory ([care-boundary.md](care-boundary.md) 参照)。 これらの mature ecosystem が今すぐ必要なら、 LangChain / LlamaIndex の方が fit します。

**TL;DR:** `reyn run index_docs` で一度 index する。LLM は必要に応じて組み込みの `recall` ツールを自動的に呼び出す。chunking 戦略は source ごとに `skill.md` 1 ファイルで上書きできる。

## クイックスタート

```bash
# 1. ドキュメントを index する
reyn run index_docs '{"source": "my_docs", "path": "docs/**/*.md", "description": "プロジェクトドキュメント"}'

# 2. チャットを開始する — LLM は必要に応じて chunk を recall する
reyn chat
> 認証設計の概要をドキュメントから要約して
```

裏側では LLM が `recall` を呼び出し、上位の chunk を取得します：

```
LLM internally calls: recall(query="認証設計", sources=["my_docs"], top_k=5)
```

ユーザーノートや任意のファイル glob を index することもできます：

```bash
reyn run index_docs '{"source": "memory", "path": ".reyn/memory/*.md", "description": "ユーザーメモとセッション記録"}'
```

## source とは何か

**source** は、一連のファイルからの chunk の名前付きコレクションです。次の情報を指定します：

| フィールド | 例 | 目的 |
|----------|-----|------|
| `source` | `my_docs` | `recall` 呼び出しと `reyn source` コマンドで使用する論理名 |
| `path` | `docs/**/*.md` | 単一の glob パターン — マッチしたすべてのファイルがまとめて index される |
| `description` | `"プロジェクトドキュメント"` | 必須。LLM がいつこの source を検索するかを判断するために使用 |

1 回の実行で 1 source、1 path、1 chunking 戦略をカバーします。異なる戦略で複数のファイル種類を index したい場合は、source ごとに `index_docs` を実行し、クエリ時に `sources=[...]` で組み合わせます：

```
recall(query="...", sources=["python_src", "my_docs", "memory"], top_k=5)
```

source のメタデータは `.reyn/index/sources.yaml` に保存されます。一度 index された source は、すべてのチャットターンで LLM のコンテキストに自動的に表示されます：

```
## Indexed sources (3 available)

- **memory** — User notes / past session memos (142 chunks)
- **reyn_code** — Reyn Python framework code (1247 chunks)
- **my_docs** — Project documentation (89 chunks)

Use the `recall` tool with `sources=[<name>, ...]` to search.
```

## `recall` ツール

`recall` はすべてのチャットセッションで LLM が利用できる組み込みツールです。自然言語クエリを受け取り、指定した source を検索して上位 K 件の chunk を返します：

```
recall(query="plan-mode の議論", sources=["memory"], top_k=5)
```

LLM はインデックス時に指定した source の description に基づいて、どの source を検索するかを判断します。どの source にアクセスできるかを skill ごとに設定する必要はありません。

内部的に `recall` はインデックス時と同じモデルを使ってクエリを embed し、各 source の SQLite index に対してコサイン類似度検索を行い、スコア順でマージします。処理全体は決定論的です。LLM が受け取るのはテキストとしての上位 K 件の chunk のみで、生のベクトルは渡りません。

もう 1 つの組み込みツール `drop_source` を使うと、chunking 戦略を試行錯誤するときなどに LLM がインデックスを削除できます：

```
drop_source(source="my_docs")
```

## Indexing 戦略

`index_docs` を実行すると、LLM がファイルのサンプルを調べて chunking 戦略を決定します。3 つの組み込み chunker が利用できます：

| Chunker | 適したコンテンツ |
|---------|--------------|
| `heading` | Markdown / RST — 見出し境界で分割 |
| `blank_line` | 平文 — 段落区切りで分割 |
| `sentence` | 密度の高いテキスト — 文単位で分割 |

LLM の戦略決定は、すべてのフェーズ遷移に使用されるのと同じ P4 メカニズムによって制約されます。宣言された chunker オプションから選択し、新しいものを作り出すことはできず、選択は postprocessor 実行前にスキーマ検証されます。

chunking ステップは `Skill.postprocessor` で決定論的に実行されます。LLM の関与はなく、attractor surface もありません。LLM の唯一の決定（戦略選択）は Phase 1 で行われ、それ以降のすべてのステップ（分割 → embed → 書き込み）は純粋な計算です。

## chunker を上書きする

デフォルトの chunker は一般的なケースをカバーします。Python ソースコード、SQL スキーマ、構造化 YAML など専門的なコーパスには、カスタム Python モジュールと最小限の `skill.md` オーバーレイで chunking ロジックを完全に置き換えることができます：

```yaml
# reyn/project/index_python_src/skill.md
extends: stdlib/index_docs

phases:
  strategy:
    instructions_override: |
      Python AST chunking — 関数とクラスの境界で分割する。
      各 chunk には関数またはクラスの本体全体を含める。

postprocessor:
  steps:
    - kind: python
      module: reyn.project.index_python_src.ast_chunkers
      fn: apply_strategy
```

`ast_chunkers.py` モジュールは strategy artifact とファイルパス glob を受け取り、chunk のリストを返します。残りのパイプライン（embed → index_write）は変わりません。

これが skill DSL の核心的な差別化点です。chunking ロジックを自然言語と Python で記述すれば、OS が embedding と indexing を処理します。完全なチュートリアルは skill author ガイドを参照してください。

## ストレージの場所

すべての index データはプロジェクトの `.reyn/` ディレクトリ内に保存されます：

```
.reyn/
  index/
    sources.yaml                   # Source manifest — 名前、path、モデル、chunk 数
    my_docs/
      index.db                     # この source の SQLite vector store
    memory/
      index.db
```

`sources.yaml` が何が index されているかの単一の信頼できる情報源です。SQLite ファイルには chunk テキストと embedding ベクトルが含まれます。任意の SQLite クライアントで閲覧できますが、スキーマは内部仕様です。

Phase 1 では SQLite のみをストレージバックエンドとして使用します。Phase 2 では `register_backend()` 拡張ポイントを通じて、Qdrant、FAISS、Pinecone などのプラグインバックエンドが追加されます。

## パーミッション

2 つのパーミッションゲートが RAG 操作を保護します：

| パーミッション | デフォルト | トリガー |
|------------|----------|---------|
| `permissions.embed` | `ask` | skill 実行ごとの最初の embedding 呼び出し |
| `permissions.index_drop` | `ask` | `drop_source` ツール呼び出しまたは `reyn source rm` |

`permissions.embed: ask` は、`index_docs` が embedding API を呼び出そうとするときに最初の一回だけ承認を求めることを意味します。`reyn.yaml` で事前承認することもできます：

```yaml
permissions:
  embed: allow
```

stdlib の `index_docs` skill は自身のパーミッションブロックに `embed: allow` が設定されているため、この設定を継承していないカスタムオーバーライドを実行する場合にのみプロンプトが表示されます。

## コスト

embedding コストは chunk 数に比例します。標準的なドキュメントセットに対する `index_docs` の 1 回の実行コストは、戦略選択の LLM 呼び出し（実行ごとに 1 回、デフォルトモデル使用）で約 **$0.0003** です。embedding コストはコーパスサイズと embedding モデルによって異なります — デフォルトは `text-embedding-3-small` です。

reyn は予期しない高額請求から保護するコスト事前チェックゲートを備えています：

- embedding 開始前に、ファイル glob から chunk 数を推定します。
- 推定値が `cost_warn_threshold`（デフォルト: 10,000 chunk）を超える場合、開始前に確認を求めます。
- `reyn.yaml` でしきい値を調整できます：

```yaml
embedding:
  cost_warn_threshold: 5000    # 5K chunk を超える前に確認
```

長時間の indexing 実行中は進捗フィードバックが表示されます：

```
Embedded 5K / 100K chunks (5%), ETA 25 min
```

## Embedding の設定

embedding モデルとバッチ処理の動作は `reyn.yaml` の `embedding:` セクションで設定します：

```yaml
embedding:
  default_class: standard
  classes:
    light:    openai/text-embedding-3-small
    standard: openai/text-embedding-3-small
    strong:   openai/text-embedding-3-large
  batch_size: 100
  max_retries: 3
  cost_warn_threshold: 10000
```

API キーは `~/.reyn/secrets.env` から `${OPENAI_API_KEY}` 経由で読み込まれます。`reyn.yaml` にリテラル値を記述する必要はありません。`reyn secret set OPENAI_API_KEY` でキーを設定すれば、追加設定なしで indexing が動作します。

## Phase 1 スコープ

**Phase 1（1.0 リリース）に含まれるもの:**

- heading / blank_line / sentence chunker を持つ `index_docs` stdlib skill
- すべてのチャットセッションで LLM が利用できる `recall` ツール
- クリーンアップ用の `drop_source` ツール
- SQLite vector store バックエンド
- `reyn source list / describe / rm` CLI
- コスト事前チェックゲートと進捗フィードバック
- オーバーライドパターン（`extends: stdlib/index_docs` + カスタム Python モジュール）
- チャットシステムプロンプトの empty-state ヒント

**Phase 1.5（1.1+）に延期:**

- memory layer のインライン展開から `recall(sources=["memory"])` への移行。1.0 では memory は従来通り動作します。

**Phase 2（1.1 以降）に延期:**

- 代替 vector store バックエンド（Qdrant、FAISS、Pinecone）
- ファイル変更時の差分 re-indexing
- 高度な retrieval（rerank、HyDE、contextual retrieval）
- ローカル embedding モデル（sentence-transformers、ollama）
- RAG 評価フレームワーク

## 制限事項

- **Phase 1 SQLite バックエンドの推奨最大値は source あたり 100K chunk** です。それ以上のコーパスも動作しますが、クエリレイテンシが増加します。
- **差分 indexing なし。** `mode: replace`（デフォルト）で `index_docs` を再実行すると、source 全体が再 index されます。`mode: append` は新しいファイルが既存の chunk と重複しないことが確実な場合にのみ使用してください。
- **Phase 1 では memory layer は変更なし。** セッション memory は引き続きインラインのシステムプロンプト展開を使用します。このリリースでは `recall` ツールと memory は独立したシステムです。
- **高度な retrieval なし。** Phase 1 はコサイン類似度のみを使用します。rerank、HyDE、contextual retrieval はありません。
- **機密データについて。** reyn は index 前に機密コンテンツを削除しません。シークレット、認証情報、個人情報を index する場合はその影響を理解した上で行ってください。削除ポリシーは Phase 2 で予定されています。
- **Embedding API が必要。** Phase 1 にはローカル embedding のパスがありません。OpenAI 互換の API キーが必要です。

## 関連項目

- [Reference: `reyn source`](../reference/cli/source.md) — CLI から index 済み source を管理する
- [ADR-0033](../deep-dives/decisions/0033-rag-extensible-os.md) — 設計の根拠と完全な技術仕様（内部向け）
- [コンセプト: workspace](workspace.md) — `.reyn/` の状態構造
- [コンセプト: パーミッションモデル](permission-model.md) — `embed` と `index_drop` パーミッションゲート
- [コンセプト: シークレット管理](secret-handling.md) — embedding API キー管理
- [Reference: `reyn.yaml`](../reference/config/reyn-yaml.md) — `embedding:` セクションのスキーマ
