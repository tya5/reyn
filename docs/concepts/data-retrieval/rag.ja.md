---
type: concept
topic: rag
audience: [human, agent]
---

# RAG（Retrieval-Augmented Generation）

reyn は内部 RAG **framework foundation** を提供します — `embed` / `index_query` / `index_drop` / `semantic_search` / `index_update` の control-IR op 群、拡張可能な `IndexBackend` protocol、`EmbeddingProvider` protocol です。**FP-0066 P1c 以降、in-core index の source を作成・変更・削除するのは OS-internal のみ**です: ユーザー向け/agent 向けにそれを行う手段は一切ありません。読み取りは汎用検索より狭いものの、もうゼロではありません — `search_knowledge`（FP-0066 P3c、landed）が 4 つの OS-curated source を横断して読みます。詳細は下記を参照。これは長い retire の経緯を閉じるものです:

- **FP-0066 P1b** が、in-core store に乗っていた agent 向け LLM ツール 4 つ（`semantic_search` / `index_update` / `drop_source` / `list_rag_sources`）を retire しました。
- **FP-0066 P1c（現状）** が、同じ store への残り 2 つのユーザー向けエントリーポイント — safe-mode `index_update()` python 呼び出し（`reyn.api.safe.index_update`）と CLI `reyn source list / describe / rm` コマンド群 — を retire しました。

この 3 つの surface はいずれも、user RAG と in-core RAG が別システムに分割される（proposal 0063）以前の残留物 — reyn 自身の内部 store に user-RAG のセマンティクスが乗っていた形でした。retire の根拠と、in-core index を OS 内部から再び到達可能にした後続フェーズ（`search_actions` は現在稼働、`search_knowledge` も FP-0066 P3c 以降 稼働 — どちらも自分で作った source を汎用に検索する agent ツールではありません）については [proposal 0066 §9](../../deep-dives/proposals/0066-retrieval-two-groups-two-axes.md) を参照してください。

> **agent に自分のドキュメントを検索させたい場合は、builtin user RAG**（proposal 0063）を使ってください: ドキュメントフォルダ（pdf / xlsx / pptx / docx / txt / md）を **自分で名付けた外部 sqlite vector store** に取り込む 2 つのバンドル済み pipeline を、MCP server 経由で使い、クエリします — 書くべき Python step は無く、agent 呼び出しで end-to-end に完結します。[Build a RAG corpus](../../guide/for-users/build-a-rag-corpus.md) を参照。これは本ページが説明する in-core index とは *別の* store・*別の* セットアップです — 両者が共有するのは `embed` primitive と、下記の `embedding:` class 設定だけです。

## in-core index は OS-internal only

in-core store に対して operator/agent が追加・削除する手段は、もはや一切ありません — safe-mode python 呼び出しも、CLI コマンドも、書き込み用の LLM ツールもありません。生の `semantic_search`/`index_update`/`index_drop` op kind 自体も agent から呼び出せません（architect 裁定、#5495: これは意図であって取りこぼしではありません — `src/` にはこの 3 つを LLM ツールとして登録している箇所が 0 件です）。substrate（`IndexUpdateIROp` / `SemanticSearchIROp` / `SqliteIndexBackend` / `EmbeddingProvider`）が残されているのは、後続の FP-0066 フェーズ（§8 ingest、§5 search）がこの上に reyn 自身の内部検索を構築するためです — action-catalog 検索（`search_actions`）は既にそうしていますし、skill/memory/repo 検索（`search_knowledge`、FP-0066 P3c、landed）も同様です: どちらも、OS 自身が index する固定の閉じた source 集合に対する OS-curated な読み取り surface であって、自分で作った source をどちらかのツールに指し示す方法ではありません。これは「何でも index して LLM に検索させる」汎用 surface ではありません — そのためには上記の FP-0063 plugin を使ってください。

`index_update` は **reconcile** であり、追記/置換の切り替えではありません: internal caller は、(再)index する `source_path` の現在の chunk 集合すべてを 1 回の呼び出しで渡します（`content_hash` に対する add/update/remove/skip — 同じ chunk での再実行は re-embed せず、変更されたハッシュを持つ既存パス配下の chunk はその chunk だけを re-embed して古いものを破棄します）。この契約は retire 前から変わっていません — 変わったのは誰が呼べるかだけです。

## 「source」とは何か

**source** とは、ファイル集合から取られた chunk の名前付きコレクションで、以下で識別されます:

| フィールド | 用途 |
|-------|---------|
| `source` | この chunk コレクションの論理名 |
| `path` | index 対象となったすべてのファイルにマッチした glob パターン |
| `description` | source の自由記述ラベル |

source のメタデータは `.reyn/config/index/sources.yaml` に永続化されます。

## ストレージの場所

すべての index データはワークスペースの `.reyn/` ディレクトリ内に保存されます:

```
.reyn/
  config/
    index/
      sources.yaml                 # Source manifest — 名前、path、モデル、chunk 数
  cache/
    index/
      <source>/
        index.db                   # この source の SQLite vector store
      memory/
        index.db
```

`sources.yaml` は何が index されているかの単一の信頼できる情報源で、operator が編集可能な状態なので `config/` 配下にあります。SQLite の index データは派生・再構築可能な状態なので `cache/` 配下にあります。recovery-core / cache / audit の分割の詳細は [`.reyn/` ディレクトリレイアウト](../../reference/runtime/reyn-dir-layout.md) を参照してください。SQLite ファイルには chunk テキストと embedding ベクトルが含まれます。スキーマは internal です。

## コスト

embedding コストは（add/update の dedup 後の）to-embed chunk 数に線形です — 変更の無い chunk は skip され、re-embed されません。to-embed バッチが大きい場合（`embedding.cost_warn_threshold` を超える場合。[§Embedding の設定](#embedding-の設定)参照）、`index_update_cost_warning` audit-event と、`index_update` の返り値 envelope の `cost_warning` フィールドで警告が表示されます。

## Embedding の設定

embedding モデルとバッチ処理の挙動は `reyn.yaml` の `embedding:` 配下で設定します — この設定は in-core index（internal caller）と `search_actions` の両方を統べます。デフォルトで 3 つの built-in class が出荷され、すべて OpenAI backed です:

```yaml
embedding:
  enabled: true
  default_class: standard
  classes:
    light:      openai/text-embedding-3-small
    standard:   openai/text-embedding-3-small
    strong:     openai/text-embedding-3-large
  batch_size: 100
  max_retries: 3
  timeout: 60.0
  cost_warn_threshold: 10000
```

`embedding.enabled`（デフォルト `false`、opt-in）は embed op 自体をゲートします — [proposal 0066 §7](../../deep-dives/proposals/0066-retrieval-two-groups-two-axes.md#7-opt-in-embeddingenabled-symmetric-model) を参照。#4156 は後に、このゲートが実際にどのワークロードを有効化するかを分割しました: `embedding.index.actions`（デフォルト **on**）は `search_actions` が読む約 10 件の action catalog を構築し、`embedding.index.repo_knowledge`（デフォルト **off**）は別枠の、はるかに大きい FP-0066 P3b repo 全体の knowledge index です — [`embedding.index`](../../reference/config/reyn-yaml.md#embedding-fields) を参照。

`timeout` は 1 試行あたりの締切（秒）です — 1 回の embedding 試行を reyn がどれだけ待つか。これが存在するのは、停止した embedding endpoint が litellm 自身の `request_timeout` デフォルト（1 試行あたり 6000 秒）でしか制限されず、operator にはハングと区別できないからです。`<= 0` で opt-out します。

**コストの制御ではありません。** `timeout` は待機を制限するのであって送信を制限するのではありません — OpenAI SDK クライアントはこの下でリトライするため、1 試行が最大 3 リクエストを wire に乗せ、`max_retries: 3` により最大 9 リクエストになります — デフォルトの 60.0 秒の締切内で、9 件すべてが約 7.6 秒で配達されることが実測されており、この締切自体は作動しません。`timeout` を下げても provider 側の計算量は減りません。[reyn.yaml § `embedding` fields](../../reference/config/reyn-yaml.md#embedding-fields) と [#3047](https://github.com/tya5/reyn/issues/3047) を参照。

Reyn は embedding を litellm に **排他的に** 依存します — in-process のモデル backend はありません（#3128 が、FP-0043 で出荷された sentence-transformers backed の `local-mini` / `local-e5` class を削除しました）。各 class の `model` 文字列は LiteLLM-routable な名前で、dispatch は LiteLLM を通して provider 自身の API に直接届くか、`LITELLM_API_BASE` 環境変数が設定されていれば **litellm proxy** を経由します（`call_llm` が読むのと同じ変数です）。

OpenAI API key は `~/.reyn/secrets.env` から `${OPENAI_API_KEY}` 経由で読まれます — `reyn.yaml` にリテラル値は書きません。`reyn secret set OPENAI_API_KEY` で設定してください。

### ローカル/オフラインの embedding モデル

Reyn は in-process のローカル embedding backend を出荷していません。ローカルモデル（API key 不要、またはオフライン/air-gapped なセットアップ）を使いたい operator は、それを **litellm proxy** の裏で動かして reyn をそこに向けます — proxy が、ローカルサーバー（Ollama / HuggingFace `text-embeddings-inference` / `infinity`）を reyn が既に期待している OpenAI 互換 endpoint に変換します。reyn 自身がローカルサーバーと直接話すことはありません。`embedding.classes` 配下に、proxy 経由のモデルを指すエントリを追加してください。例:

```yaml
embedding:
  classes:
    local:
      model: openai/nomic-embed-text   # LITELLM_API_BASE が provider/ 接頭辞を剥がした後の名前
```

その上で reyn を起動する前に `export LITELLM_API_BASE=http://localhost:4000`（自分の proxy のアドレス）としてください。セットアップの完全な手順（サーバー選択、proxy の `config.yaml`、`provider/` 名前剥がしのルール、事前検証）は [Guide: enable semantic search § Case B](../../guide/for-users/enable-semantic-search.md#case-b-no-embedding-api-contract-litellm-proxy-a-local-model) にあります — `search_actions` 向けに書かれていますが、同じ仕組みが in-core index の internal caller にも使えます。

チャット側の action retrieval（= `search_actions`）については、[Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md) と、キャッシュ管理のための [`reyn embeddings`](../../reference/cli/embeddings.md) CLI を参照してください。

## Phase の経緯

**FP-0066 P1c（現状）**: in-core store への残り 2 つのユーザー向けエントリーポイント — safe-mode `index_update()` python 呼び出しと CLI `reyn source` コマンド群 — が clean-break・shim 無しで retire されました。in-core store に **書き込む** operator/agent 向け手段は、もはや一切ありません。読み取りは別軸です — 下記 FP-0066 P3c を参照。

**Retire 前に landed していたもの（historical）:**

- **FP-0066 P1b**: agent 向け layer-1 ツール（`semantic_search` / `index_update` / `drop_source` / `list_rag_sources`）が retire されました。
- **FP-0043** が、chat 側 action retrieval（`search_actions`）向けのローカル embedding パスを追加しました。当初は in-process の `sentence-transformers` backend として出荷されましたが、**#3128 がその in-process backend を削除**しました — reyn は今や embedding を litellm に排他的に依存し、「ローカル」が欲しければ operator 自身が動かす litellm proxy 経由で到達します — [§ローカル/オフラインの embedding モデル](#ローカルオフラインの-embedding-モデル)を参照。
- **FP-0057 Phase 2a/2b**: `recall` が `semantic_search` に rename（後に FP-0066 P1b で retire）。safe-mode の ingestion エントリーポイント `index_update()`（後に FP-0066 P1c で retire）が、retire された `embed_and_index()`（`reyn.api.safe.embed_index`、clean-break・shim 無し）を置き換え、incremental/delta-reconcile（source の現在の index に対する add/update/remove/skip）を追加しました。
- **#3026** が `list_rag_sources`（後に FP-0066 P1b で retire）を、index 済みコーパスを列挙する discovery verb として追加しました。

**Retire 後に landed したもの（読み取りのみ、書き込みではない）:**

- **FP-0066 P3c** が `search_knowledge` を追加しました — skill/memory/repo-doc/repo-src の 4 つの OS-curated source を横断して読む LLM ツールで、entity 単位に集約されます。読み取り専用で、その 4 つの固定 source に限定されます — 自分で作った source を検索したり指し示したりする手段ではありません。[上記「in-core index は OS-internal only」](#in-core-index-は-os-internal-only)を参照。

**将来フェーズへの延期:**

- 代替 vector store backend（Qdrant、FAISS、Pinecone）
- Advanced retrieval（rerank / HyDE / contextual retrieval）
- 追加のローカル backend（ollama、ONNX、GGUF）
- RAG 評価 framework

## 制限事項

- **source あたり推奨最大 100K chunk**（SQLite backend）。より大きいコーパスも動作しますが、クエリ遅延が増加します。
- **フルリビルドモードなし。** `index_update` は reconcile 専用です（現在の index に対する add/update/remove/skip）— `mode="replace"` のような全消去・再構築呼び出しはありません。ゼロからの再構築は、まず対象 source に `index_drop` を実行し、空になった source に対して `index_update` を再実行してください。
- **user-facing なエントリーポイントが一切ない。** FP-0066 P1c 以降、in-core index には safe-mode python 呼び出しも、CLI コマンドも、LLM ツールもありません。自分のドキュメントに対する agent 駆動検索が必要な場合は FP-0063 user RAG plugin を使ってください。
- **Advanced retrieval なし。** Cosine 類似度のみ — reranking、HyDE、contextual retrieval はありません。
- **機微データ。** reyn は index 前に機微な内容を redact しません。その影響を理解していない限り、secret・credential・PII を index しないでください。
- **Embedding には API key か、自前で動かす litellm proxy のどちらかが必要です。** built-in class（`light` / `standard` / `strong`）は `OPENAI_API_KEY` を必要とします。credential 不要な経路が欲しい場合は、operator がローカル embedding server を litellm proxy の裏に立て、それを指す `embedding.classes` エントリを追加する必要があります（[§ローカル/オフラインの embedding モデル](#ローカルオフラインの-embedding-モデル)参照）。[§Embedding の設定](#embedding-の設定)も参照してください。

## 関連項目

- [Guide: Build a RAG corpus](../../guide/for-users/build-a-rag-corpus.md) — agent 呼び出し可能な user RAG: 外部 sqlite store 上の builtin pipeline（proposal 0063）
- [Proposal 0066: retrieval redesign](../../deep-dives/proposals/0066-retrieval-two-groups-two-axes.md) — in-core のユーザー向け surface がなぜ retire されたか、何がそれに代わるか
- [ADR-0033](../../deep-dives/decisions/0033-rag-extensible-os.md) — 設計根拠と完全な技術仕様（internal、historical）
- [Concepts: workspace](../runtime/workspace.md) — `.reyn/` state の構造
- [Concepts: secret handling](../runtime/secret-handling.md) — embedding API key の管理
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — `embedding:` セクションのスキーマ
