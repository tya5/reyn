---
type: concept
topic: rag
audience: [human, agent]
---

# RAG（Retrieval-Augmented Generation）

reyn は RAG **framework foundation** を提供します — 5 つの primitive op（`embed` / `index_query` / `index_drop` / `semantic_search` / `index_update`）、 拡張可能な `IndexBackend` protocol、 `EmbeddingProvider` protocol、 safe-mode の `index_update()` エントリーポイント（FP-0057 Phase 2b; 旧 `embed_and_index()` を clean-break で retire）。 任意のドキュメントコーパスを index し、 クエリ時に LLM が関連する chunk を取得できます。コーパス全体をコンテキストウィンドウに展開する必要はありません。

**差別化: 検索は組み込みツール、ライブラリ呼び出しではない。** LangChain や LlamaIndex は自分のドライバーコードから呼び出す Python pipeline を提供しますが、reyn の `semantic_search`/`drop_source` は通常の `reyn chat` セッション中に LLM 自身が呼び出す組み込みツールです — 検索側にオーケストレーションコードは不要です。

**Phase 1 scope (= 1.0 release)** で出荷されるのは framework foundation、 SQLite default backend (≤100K chunks、 sub-second query)、 LiteLLM embedding passthrough。 vector store plugin variety (Qdrant / FAISS / Weaviate / Pinecone)、 advanced retrieval (rerank / HyDE / contextual retrieval)、 RAG eval framework、 IDE integration は post-1.0 (= phase 2) territory ([../architecture/care-boundary.md](../architecture/care-boundary.md) 参照)。 これらの mature ecosystem が今すぐ必要なら、 LangChain / LlamaIndex の方が fit します。

**TL;DR:** 検索は自動 — LLM が必要な情報を組み込みの `semantic_search` ツールで自動的に取得します。source を作るには自分のファイルを読んで `index_update()` を呼ぶ短い safe-mode Python step が必要です（一発コマンドの indexing skill はもうバンドルされていません）。

## クイックスタート

コーパスの indexing は一度だけ実行する小さなスクリプト — ファイルを読んで chunk に分割し、`index_update` に渡します（`python` step、デフォルトは safe mode）：

```python
# my_project/index_docs.py — python step として一度だけ実行(mode: safe がデフォルト)
from reyn.api.safe import file, index_update as iu

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

iu.index_update(
    chunks,
    source="my_docs",
    model="text-embedding-3-small",
    description="プロジェクトドキュメント",
    path="docs/**/*.md",
)
```

`index_update` は append/replace の切り替えではなく **reconcile**（差分整合）です — add/update/remove/skip の契約は英語版 [Concepts: RAG](rag.md#chunking-is-your-own-code) を参照してください（同じ chunk での再実行は re-embed せず、`content_hash` が変わった `source_path` はその chunk だけ再 embed して古い hash を削除します）。

```bash
# チャットを開始する — LLM は必要に応じて chunk を semantic_search で取得する
reyn chat
> 認証設計の概要をドキュメントから要約して
```

実 `gemini-embedding-001` を LiteLLM proxy 経由でエンドツーエンド検証済み: EN concept doc 21 本 → chunk 418 個を index (~$0.001)、自然な概念クエリ（"What is X in Reyn?"、"Explain Reyn's permission model"）が chat 3 回中 3 回で index 済みのセマンティック回答を返しました（= batch 22、2026-05-10）。`docs/deep-dives/journal/dogfood/2026-05-10-batch-22-affordance-bias-fix/findings.md` 参照。（この検証は `embed_and_index()` エントリーポイントおよびその FP-0057 Phase 2b 後継 `index_update()` 以前・当時削除前の `index_docs` skill を使用したものですが、embed/index/recall の内部メカニズムは変わっていません。）

裏側では LLM が `semantic_search` を呼び出し、上位の chunk を取得します：

```
LLM internally calls: semantic_search(query="認証設計", sources=["my_docs"], top_k=5)
```

同じスクリプトパターンで任意のファイル glob を index できます — ユーザーノート、ソースコード、JSONL ログなど。`file.glob()` のパスと `source` 名を変えるだけです。

## source とは何か

**source** は、一連のファイルからの chunk の名前付きコレクションです。次の情報を指定します：

| フィールド | 例 | 目的 |
|----------|-----|------|
| `source` | `my_docs` | `semantic_search` 呼び出しと `reyn source` コマンドで使用する論理名 |
| `path` | `docs/**/*.md` | 単一の glob パターン — マッチしたすべてのファイルがまとめて index される |
| `description` | `"プロジェクトドキュメント"` | 必須。LLM がいつこの source を検索するかを判断するために使用 |

1 回の indexing 実行で 1 source、1 path、1 chunking 方式をカバーします。異なる chunking で複数のファイル種類を index したい場合は、source ごとに indexing スクリプトを実行し、クエリ時に `sources=[...]` で組み合わせます：

```
semantic_search(query="...", sources=["python_src", "my_docs", "memory"], top_k=5)
```

source のメタデータは `.reyn/config/index/sources.yaml` に保存されます。LLM は
`list_rag_sources` を呼んで、index 済みの source を名前・説明・チャンク数つきで
取得します：

```
list_rag_sources()
→ {"sources": [
    {"name": "memory",    "description": "User notes / past session memos", "chunk_count": 142},
    {"name": "reyn_code", "description": "Reyn Python framework code",      "chunk_count": 1247},
    {"name": "my_docs",   "description": "Project documentation",           "chunk_count": 89}
  ]}
```

ここで返る name が、`semantic_search` の `sources` 引数に渡す名前です。discovery は
system prompt の常設ブロックではなくツール呼び出しなので、corpus を多数持つ運用者でも
モデルが実際に尋ねたターンでしかコストを払いません。

## `semantic_search` ツール

`semantic_search`（FP-0057 Phase 2a; `recall` から rename）はすべてのチャットセッションで LLM が利用できる組み込みツールです。自然言語クエリを受け取り、指定した source を検索して上位 K 件の chunk を返します：

```
semantic_search(query="plan-mode の議論", sources=["memory"], top_k=5)
```

LLM はインデックス時に指定した source の description に基づいて、どの source を検索するかを判断します。どの source にアクセスできるかをワークフローごとに設定する必要はありません。

内部的に `semantic_search` はインデックス時と同じモデルを使ってクエリを embed し、各 source の SQLite index に対してコサイン類似度検索を行い、スコア順でマージします。処理全体は決定論的です。LLM が受け取るのはテキストとしての上位 K 件の chunk のみで、生のベクトルは渡りません。

もう 1 つの組み込みツール `drop_source` を使うと、chunking を試行錯誤するときなどに LLM がインデックスを削除できます：

```
drop_source(source="my_docs")
```

## Chunking は自分のコードで書く

バンドルされた chunker や LLM 主導の戦略選択はもうありません — [クイックスタート](#クイックスタート)の chunking ロジック（段落分割）は自分で書いてコーパスに合わせて調整する plain Python です。専門的なコーパス（Python ソースコード、SQL スキーマ、構造化 YAML）には、`index_update` を呼ぶ前にその corpus に合った分割ロジック（例: ソースコード用の AST ベース分割、Markdown 用の見出しベース分割）を差し込んでください。

chunking ステップは自分の `python` step 内で決定論的に実行されます。LLM の関与はなく、attractor surface もありません。`index_update` が add/update/remove/skip の reconcile・embedding・index 書き込みを処理し、それより上流（ファイル読み込み、chunk 分割）はすべて普通の Python です。1 回の呼び出しには、(再)index する `source_path` の現在の chunk 集合すべてを渡してください — 削除検出には reconcile がそのパスの完全な集合を見る必要があります。

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

`index_update()` 自体には専用のパーミッションゲートはありません — それを呼ぶ safe-mode `python` step は、RAG 固有のゲートではなく、呼び出し元 phase の通常の python-step パーミッションの下で実行されます。

## コスト

embedding コストは（add/update の dedup 後の）to-embed chunk 数に比例し、コーパスサイズと embedding モデルによって異なります — デフォルトは `text-embedding-3-small` です。削除された `index_docs` skill のラッパーとは異なり、safe-mode エントリーにはインタラクティブなコスト事前チェックはありませんが、to-embed バッチが `embedding.cost_warn_threshold` を超えると `index_update_cost_warning` の audit-event と、戻り値の envelope の `cost_warning` フィールドで警告が表示されるようになりました — indexing スクリプトで反応させたい場合は `result["cost_warning"]` を確認してください。

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

- すべてのチャットセッションで LLM が利用できる `semantic_search` ツール
- クリーンアップ用の `drop_source` ツール
- SQLite vector store バックエンド
- `reyn source list / describe / rm` CLI
- チャットシステムプロンプトの empty-state ヒント

**Phase 1.5（1.1+）に延期:**

- memory layer のインライン展開から `semantic_search(sources=["memory"])` への移行。1.0 では memory は従来通り動作します。

**1.0 以降に landed:**

- **FP-0057 Phase 2a/2b**: `recall` は `semantic_search` に rename。safe-mode の indexing エントリーポイントは `index_update()`（`reyn.api.safe.index_update`）になりました — source の現在の index に対する差分 reconcile（add/update/remove/skip）呼び出しで、retire された `embed_and_index()`（`reyn.api.safe.embed_index`、clean-break・shim なし）を置き換えます。これにより下記「差分 indexing なし」のギャップも解消されました — reconcile が `content_hash` で削除・変更されたファイルを検出するため、通常のファイル変更には別の rebuild モードは不要です。

**Phase 2（1.1 以降）に延期:**

- 代替 vector store バックエンド（Qdrant、FAISS、Pinecone）
- 高度な retrieval（rerank、HyDE、contextual retrieval）
- ローカル embedding モデル（ollama、ONNX、GGUF）
- RAG 評価フレームワーク

## 制限事項

- **Phase 1 SQLite バックエンドの推奨最大値は source あたり 100K chunk** です。それ以上のコーパスも動作しますが、クエリレイテンシが増加します。
- **フルリビルドモードなし。** `index_update` は reconcile 専用です（現在の index に対する add/update/remove/skip）— `mode="replace"` のような全消去・再構築呼び出しはありません。フルリビルドを強制するには、まず `index_drop` ツール（または `reyn source rm`）で source を削除してから、空の source に対して `index_update` を再実行してください。
- **Phase 1 では memory layer は変更なし。** セッション memory は引き続きインラインのシステムプロンプト展開を使用します。このリリースでは `semantic_search` ツールと memory は独立したシステムです。
- **高度な retrieval なし。** Phase 1 はコサイン類似度のみを使用します。rerank、HyDE、contextual retrieval はありません。
- **機密データについて。** reyn は index 前に機密コンテンツを削除しません。シークレット、認証情報、個人情報を index する場合はその影響を理解した上で行ってください。削除ポリシーは Phase 2 で予定されています。
- **Embedding API が必要。** Phase 1 にはローカル embedding のパスがありません。OpenAI 互換の API キーが必要です。

## Operational Intelligence — イベントへの `semantic_search`

同じ `semantic_search` op（FP-0057 Phase 2a; `recall` から rename）は、他のコーパスと同じ `index_update()` パターンで index された Reyn 自身の P6 実行イベントログにも動作します（source 名は慣習的に `"events"`）。チャンクメタデータの形、クエリ例、この indexing 経路の現状は [コンセプト: Operational Intelligence](operational-intelligence.ja.md) を参照。

## 関連項目

- [Reference: `reyn source`](../../reference/cli/source.md) — CLI から index 済み source を管理する
- [ADR-0033](../../deep-dives/decisions/0033-rag-extensible-os.md) — 設計の根拠と完全な技術仕様（内部向け）
- [コンセプト: workspace](../runtime/workspace.md) — `.reyn/` の状態構造
- [コンセプト: パーミッションモデル](../runtime/permission-model.md) — `index_drop` パーミッションゲート
- [コンセプト: シークレット管理](../runtime/secret-handling.md) — embedding API キー管理
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — `embedding:` セクションのスキーマ
