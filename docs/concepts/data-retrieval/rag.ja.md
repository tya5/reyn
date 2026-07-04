---
type: concept
topic: rag
audience: [human, agent]
---

# RAG（Retrieval-Augmented Generation）

reyn は RAG **framework foundation** を提供します — 5 つの primitive op、 拡張可能な `IndexBackend` protocol、 `EmbeddingProvider` protocol、 safe-mode の `embed_and_index()` エントリーポイント。 任意のドキュメントコーパスを index し、 クエリ時に LLM が関連する chunk を取得できます。コーパス全体をコンテキストウィンドウに展開する必要はありません。

**差別化: 検索は組み込みツール、ライブラリ呼び出しではない。** LangChain や LlamaIndex は自分のドライバーコードから呼び出す Python pipeline を提供しますが、reyn の `recall`/`drop_source` は通常の `reyn chat` セッション中に LLM 自身が呼び出す組み込みツールです — 検索側にオーケストレーションコードは不要です。

**Phase 1 scope (= 1.0 release)** で出荷されるのは framework foundation、 SQLite default backend (≤100K chunks、 sub-second query)、 LiteLLM embedding passthrough。 vector store plugin variety (Qdrant / FAISS / Weaviate / Pinecone)、 advanced retrieval (rerank / HyDE / contextual retrieval)、 RAG eval framework、 IDE integration は post-1.0 (= phase 2) territory ([../architecture/care-boundary.md](../architecture/care-boundary.md) 参照)。 これらの mature ecosystem が今すぐ必要なら、 LangChain / LlamaIndex の方が fit します。

**TL;DR:** 検索は自動 — LLM が必要な情報を組み込みの `recall` ツールで自動的に取得します。source を作るには自分のファイルを読んで `embed_and_index()` を呼ぶ短い safe-mode Python step が必要です（一発コマンドの indexing skill はもうバンドルされていません）。

## クイックスタート

コーパスの indexing は一度だけ実行する小さなスクリプト — ファイルを読んで chunk に分割し、`embed_and_index` に渡します（`python` step、デフォルトは safe mode）：

```python
# my_project/index_docs.py — python step として一度だけ実行(mode: safe がデフォルト)
from reyn.api.safe import file, embed_index

paths = file.glob("docs/**/*.md")
chunks = []
for path in paths:
    text = file.read(path)
    # 単純な段落分割 — コーパスに合わせて置き換えてください
    for i, para in enumerate(text.split("\n\n")):
        if not para.strip():
            continue
        chunks.append({
            "text": para,
            "metadata": {"content_hash": f"{path}:{i}", "source_path": path},
        })

embed_index.embed_and_index(
    chunks,
    source="my_docs",
    model="text-embedding-3-small",
    mode="replace",
    description="プロジェクトドキュメント",
    path="docs/**/*.md",
)
```

```bash
# チャットを開始する — LLM は必要に応じて chunk を recall する
reyn chat
> 認証設計の概要をドキュメントから要約して
```

実 `gemini-embedding-001` を LiteLLM proxy 経由でエンドツーエンド検証済み: EN concept doc 21 本 → chunk 418 個を index (~$0.001)、自然な概念クエリ（"What is X in Reyn?"、"Explain Reyn's permission model"）が chat 3 回中 3 回で index 済みのセマンティック回答を返しました（= batch 22、2026-05-10）。`docs/deep-dives/journal/dogfood/2026-05-10-batch-22-affordance-bias-fix/findings.md` 参照。（この検証は `embed_and_index()` エントリーポイント以前・当時削除前の `index_docs` skill を使用したものですが、embed/index/recall の内部メカニズムは変わっていません。）

裏側では LLM が `recall` を呼び出し、上位の chunk を取得します：

```
LLM internally calls: recall(query="認証設計", sources=["my_docs"], top_k=5)
```

同じスクリプトパターンで任意のファイル glob を index できます — ユーザーノート、ソースコード、JSONL ログなど。`file.glob()` のパスと `source` 名を変えるだけです。

## source とは何か

**source** は、一連のファイルからの chunk の名前付きコレクションです。次の情報を指定します：

| フィールド | 例 | 目的 |
|----------|-----|------|
| `source` | `my_docs` | `recall` 呼び出しと `reyn source` コマンドで使用する論理名 |
| `path` | `docs/**/*.md` | 単一の glob パターン — マッチしたすべてのファイルがまとめて index される |
| `description` | `"プロジェクトドキュメント"` | 必須。LLM がいつこの source を検索するかを判断するために使用 |

1 回の indexing 実行で 1 source、1 path、1 chunking 方式をカバーします。異なる chunking で複数のファイル種類を index したい場合は、source ごとに indexing スクリプトを実行し、クエリ時に `sources=[...]` で組み合わせます：

```
recall(query="...", sources=["python_src", "my_docs", "memory"], top_k=5)
```

source のメタデータは `.reyn/config/index/sources.yaml` に保存されます。一度 index された source は、すべてのチャットターンで LLM のコンテキストに自動的に表示されます：

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

LLM はインデックス時に指定した source の description に基づいて、どの source を検索するかを判断します。どの source にアクセスできるかをワークフローごとに設定する必要はありません。

内部的に `recall` はインデックス時と同じモデルを使ってクエリを embed し、各 source の SQLite index に対してコサイン類似度検索を行い、スコア順でマージします。処理全体は決定論的です。LLM が受け取るのはテキストとしての上位 K 件の chunk のみで、生のベクトルは渡りません。

もう 1 つの組み込みツール `drop_source` を使うと、chunking を試行錯誤するときなどに LLM がインデックスを削除できます：

```
drop_source(source="my_docs")
```

## Chunking は自分のコードで書く

バンドルされた chunker や LLM 主導の戦略選択はもうありません — [クイックスタート](#クイックスタート)の chunking ロジック（段落分割）は自分で書いてコーパスに合わせて調整する plain Python です。専門的なコーパス（Python ソースコード、SQL スキーマ、構造化 YAML）には、`embed_and_index` を呼ぶ前にその corpus に合った分割ロジック（例: ソースコード用の AST ベース分割、Markdown 用の見出しベース分割）を差し込んでください。

chunking ステップは自分の `python` step 内で決定論的に実行されます。LLM の関与はなく、attractor surface もありません。`embed_and_index` が embedding と index 書き込みを処理し、それより上流（ファイル読み込み、chunk 分割）はすべて普通の Python です。

## ストレージの場所

すべての index データはプロジェクトの `.reyn/` ディレクトリ内に保存されます：

```
.reyn/
  config/
    index/
      sources.yaml                 # Source manifest — 名前、path、モデル、chunk 数
  cache/
    index/
      my_docs/
        index.db                   # この source の SQLite vector store
      memory/
        index.db
```

`sources.yaml` は何が index されているかの単一の信頼できる情報源であり、operator が編集可能な状態なので `config/` 配下にあります。SQLite の index データは派生・再構築可能な状態なので `cache/` 配下にあります。recovery-core / cache / audit の分割詳細は [`.reyn/` ディレクトリレイアウト](../../reference/runtime/reyn-dir-layout.md) を参照。SQLite ファイルには chunk テキストと embedding ベクトルが含まれます。任意の SQLite クライアントで閲覧できますが、スキーマは内部仕様です。

Phase 1 では SQLite のみをストレージバックエンドとして使用します。Phase 2 では `register_backend()` 拡張ポイントを通じて、Qdrant、FAISS、Pinecone などのプラグインバックエンドが追加されます。

## パーミッション

RAG 操作を保護するパーミッションゲートは 1 つです（LLM 向け側）：

| パーミッション | デフォルト | トリガー |
|------------|----------|---------|
| `permissions.index_drop` | `ask` | `drop_source` ツール呼び出しまたは `reyn source rm` |

`embed_and_index()` 自体には専用のパーミッションゲートはありません — それを呼ぶ safe-mode `python` step は、RAG 固有のゲートではなく、呼び出し元 phase の通常の python-step パーミッションの下で実行されます。

## コスト

embedding コストは chunk 数に比例し、コーパスサイズと embedding モデルによって異なります — デフォルトは `text-embedding-3-small` です。手書きの indexing step にはコスト事前チェックや進捗表示は組み込まれていません（削除された `index_docs` skill のラッパーとは異なります）— コストが気になる場合は、大規模な indexing を実行する前に自分でファイル glob から chunk 数を見積もってください。

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

- indexing 用の `embed_and_index()` safe-mode エントリーポイント（`reyn.api.safe.embed_index`）
- すべてのチャットセッションで LLM が利用できる `recall` ツール
- クリーンアップ用の `drop_source` ツール
- SQLite vector store バックエンド
- `reyn source list / describe / rm` CLI
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
- **差分 indexing なし。** `embed_and_index` の `mode="append"` デフォルトは既に index 済みの `content_hash` を持つ chunk をスキップしますが、削除・変更されたファイルの検出はしません。ファイルが変わったら `mode="replace"` で source を作り直してください。
- **Phase 1 では memory layer は変更なし。** セッション memory は引き続きインラインのシステムプロンプト展開を使用します。このリリースでは `recall` ツールと memory は独立したシステムです。
- **高度な retrieval なし。** Phase 1 はコサイン類似度のみを使用します。rerank、HyDE、contextual retrieval はありません。
- **機密データについて。** reyn は index 前に機密コンテンツを削除しません。シークレット、認証情報、個人情報を index する場合はその影響を理解した上で行ってください。削除ポリシーは Phase 2 で予定されています。
- **Embedding API が必要。** Phase 1 にはローカル embedding のパスがありません。OpenAI 互換の API キーが必要です。

## Operational Intelligence — イベントへの `recall`

同じ `recall` op は、他のコーパスと同じ `embed_and_index()` パターンで index された Reyn 自身の P6 実行イベントログにも動作します（source 名は慣習的に `"events"`）。チャンクメタデータの形、クエリ例、この indexing 経路の現状は [コンセプト: Operational Intelligence](operational-intelligence.ja.md) を参照。

## 関連項目

- [Reference: `reyn source`](../../reference/cli/source.md) — CLI から index 済み source を管理する
- [ADR-0033](../../deep-dives/decisions/0033-rag-extensible-os.md) — 設計の根拠と完全な技術仕様（内部向け）
- [コンセプト: workspace](../runtime/workspace.md) — `.reyn/` の状態構造
- [コンセプト: パーミッションモデル](../runtime/permission-model.md) — `index_drop` パーミッションゲート
- [コンセプト: シークレット管理](../runtime/secret-handling.md) — embedding API キー管理
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — `embedding:` セクションのスキーマ
