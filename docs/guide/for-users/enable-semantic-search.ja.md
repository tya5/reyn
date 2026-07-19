# セマンティックなアクション検索を有効にする

`reyn chat` には、LLM が実行可能なことを発見するための 2 つの手段が同梱されています: 高速な **`list_actions`** ブラウザ（= カテゴリ接頭辞による列挙、常時利用可能）と、**`search_actions`** セマンティック検索（= 全アクションの埋め込みインデックスに対する自然言語クエリ）です。本ガイドではセマンティックな経路を有効にする手順を説明します。

> **TL;DR**: `search_actions` は **デフォルトで無効**（semantic search はプロジェクト全体で opt-in の方針）。すでに埋め込み API キーがある場合は `reyn secret set OPENAI_API_KEY` のあと `reyn.yaml` で `action_retrieval.embedding_class: standard` を設定するだけ — proxy も追加インストールも不要です。API キーなしでローカルモデルを使いたい場合は **litellm proxy** の背後にモデルを立て、reyn からそこを指してください（後述の [経路 B](#b-api-litellm-proxy) を参照）。

## どんなときに欲しくなるか

`search_actions` は次の違いを生みます:

- **無い場合**: LLM はあなたの意図がどのカテゴリ（`file` / `mcp` / `memory_operation` / …）に属するかを推測し、`list_actions(category=[...])` を実行して列挙する必要があります。_「PDF をテキストに変換するアクションを探して」_ のような自然言語の依頼では、すぐに一致が見つからないと LLM が試したうえで断ることもあります。
- **有る場合**: LLM は `search_actions(query="PDF to text")` を実行し、全カテゴリ横断で関連度順に並んだ top-K のリストを得ます。その後そのまま `describe_action` や `invoke_action` を実行できます。

`action_retrieval.embedding_class` のデフォルトは `null`（無効）です — semantic search は opt-in なので、明示的な `reyn.yaml` 設定が必要です。クラス未設定の場合、`search_actions` は LLM のツールリストから **除外** されます（[可視性ゲート](../../concepts/tools-integrations/universal-catalog.md#what-stays-out-of-phase-1) を参照）— これは何も試行されないため起動時警告なしで silent に行われます。

## Reyn の埋め込みは litellm 専属

Reyn には **in-process の埋め込みバックエンドはありません**。action retrieval / `semantic_search` / builtin RAG プラグインを含む、すべての埋め込み呼び出しは `litellm` を経由します — プロバイダー自身の API に直接、または環境変数 `LITELLM_API_BASE` が設定されていれば **litellm proxy** 経由（`call_llm` が読む変数と同じ — 1 つの proxy がチャットと埋め込みの両方を担います）。組み込みクラス: `light` / `standard` → `openai/text-embedding-3-small`、`strong` → `openai/text-embedding-3-large`。

セットアップは 2 通りで、どちらを選ぶかは埋め込み API 契約がすでにあるかどうかで決まります。

### Pre-flight: エンドポイントが実際に応答するか確認する（opt-in の前に行う）

opt-in する前に一度 curl で確認します。この確認は **transport に依存しない構造**です — reyn は常に `LITELLM_API_BASE` が指す先の OpenAI 互換 `/embeddings` エンドポイントへ埋め込みリクエストを送るため、litellm proxy でも、直接の埋め込み API でも、ローカルサーバーでも reyn 側からは同じに見え、同じ一行で確認できます:

```bash
curl -s "${LITELLM_API_BASE:-<your-endpoint>}/embeddings" \
  -H "Authorization: Bearer ${OPENAI_API_KEY:-dummy}" \
  -H "Content-Type: application/json" \
  -d '{"model": "<the model name your endpoint expects>", "input": "hello"}' \
  | jq '.data[0].embedding | length'
```

`<your-endpoint>` / モデル名 / キーは実際の値に置き換えてください — これはそのまま使うコマンドではなく、調整すべき形です。**正常**: 正の整数（埋め込み次元、例 `1536`）が出力される — `data[0].embedding` が空でない float 配列として返っています。典型的な失敗パターン:

- **401** — キーが誤り、または未設定。
- **404 / "model not found"** — そのモデル名がこのエンドポイントに登録されていない（proxy の `model_list` 不一致、または直接 API のモデル文字列が誤り）。
- **400, unsupported param** — *proxy 経由のときのみ関連*（経路 B を参照）: proxy に `litellm_settings.drop_params: true` が設定されていません（#1616）。
- **connection refused** — そのエンドポイントに何も listen していない、または `LITELLM_API_BASE` が誤ったアドレスを指しています。

## 経路 A — 埋め込み API キーがある場合 — proxy 不要

これが最短経路で、proxy を一切経由 **しません**:

```bash
reyn secret set OPENAI_API_KEY
# プロンプトが出たら sk-... キーを入力
```

続けて `reyn.yaml` で明示的に opt-in します（デフォルトは `null` / 無効）:

```yaml
action_retrieval:
  embedding_class: standard   # = openai/text-embedding-3-small
```

`LITELLM_API_BASE` が未設定の場合、reyn の litellm クライアントはプロバイダーの API を **直接** 呼び出すため、`standard` は **proxy も `drop_params` 設定も不要**で動作します — クライアントはすべての呼び出しで既に `drop_params=True` を渡しており、これは proxy が間に入っているときにのみ意味を持ちます（経路 B を参照）。上記の pre-flight curl をプロバイダー自身のエンドポイント（例: `https://api.openai.com/v1`）に対して実行して確認し、チャットセッションを開始してください — `search_actions` は次のコールドスタートでインデックスを eager に構築します。

すでに組織の LLM トラフィックが共有 litellm proxy を経由している場合、キーを持っていても実質的には下記の経路 B の状況（経路に proxy が入っている）です — proxy の `drop_params` の注意点はあなたにも当てはまります。

## 経路 B — 埋め込み API 契約なし → litellm proxy + ローカルモデル

キーがなく、取得もしたくない場合 — またはオフライン / エアギャップ環境が必要な場合: litellm proxy の背後でローカル埋め込みモデルを動かします。proxy がそのローカルモデルを reyn が既に期待する OpenAI 互換エンドポイントに変換します。reyn 自身はローカルサーバーと直接会話することはありません。ローカルモデルをキャッシュ済みにしておけば、これは `search_actions` を完全オフラインで動かす方法でもあります — reyn は常に proxy としか話さないため、reyn から Hugging Face への到達は一切発生しません。

**Step 1 — ローカル埋め込みサーバーを起動する。** Ollama が最も軽量なセットアップです（openai 互換の埋め込みを標準搭載）。参考コマンドは以下、**自分のマシンで検証してください。バージョン/ポートは異なる場合があります**:

```bash
ollama pull nomic-embed-text
ollama serve   # バックグラウンドサービスとしてまだ動いていなければ
curl http://localhost:11434/api/embeddings -d '{"model": "nomic-embed-text", "prompt": "hello"}'
```

（代替、それぞれ一行: HuggingFace `text-embeddings-inference`、または `infinity` — どちらも OpenAI 互換の埋め込みエンドポイントを公開します。）

**Step 2 — litellm proxy の `config.yaml` に登録する。** 構文は litellm 自身のドキュメントで確認済み（https://docs.litellm.ai/docs/proxy/embedding、https://docs.litellm.ai/docs/proxy/configs）:

```yaml
model_list:
  - model_name: text-embedding-3-small   # 下記の命名ルールを参照
    litellm_params:
      model: ollama/nomic-embed-text
      api_base: http://localhost:11434

litellm_settings:
  drop_params: true   # 必須 -- 上記の 400 失敗パターンを参照（#1616）
```

編集後は proxy を再起動してください。

**Step 3 — reyn を proxy に向ける:**

```bash
export LITELLM_API_BASE=http://localhost:4000   # あなたの proxy のアドレス
```

**命名ルール（モデル名を選ぶ前に読む）**: `LITELLM_API_BASE` が設定されている場合、reyn は解決済みモデル文字列の先頭 `provider/` セグメントを proxy へ送る前に取り除きます — `openai/foo` は proxy には単なる `foo` として届きます。そのため proxy の `model_list[].model_name` は、使用する reyn 側モデル文字列の**最初の `/` より後ろすべて**と一致させる必要があります:

- **(a) 最もシンプル — `reyn.yaml` の編集は不要。** 組み込みの `standard` クラス（`openai/text-embedding-3-small`）をそのまま使い、proxy の `model_name` を `text-embedding-3-small`（上記 Step 2 のとおり）として登録します — これでローカルモデルが reyn のデフォルトクラス名で応答するようになります:
  ```yaml
  action_retrieval:
    embedding_class: standard
  ```
- **(b) または明示的なクラスを追加**、例えば `reyn.yaml` に:
  ```yaml
  embedding:
    classes:
      local:
        model: openai/nomic-embed-text
  action_retrieval:
    embedding_class: local
  ```
  ここでは proxy の `model_name` は `nomic-embed-text`（`openai/` より後ろすべて）である必要があります。

**Step 4 — end-to-end で確認する。** まず上記の pre-flight curl を再実行し（最も安価な確認）、その後チャットセッションを開始して `search_actions` がツールリストに現れるか（または後述の `reyn embeddings status`）確認してください。空でないインデックス（`ACTIONS > 0`）が本物の signal です。

### ローカルモデルを選ぶ（経路 B） — 一度決めると変更コストが高い

後で埋め込みモデルを切り替えると、そのモデルが生成した埋め込み（アクションインデックス、および同じクラスを使う RAG ソース）はすべて無効になり再埋め込みが必要になります — 本格的に使う前に以下の軸で決めてください:

- **言語。** 英語のみの利用なら、小さな英語専用モデルで十分です。日本語・中国語・混在言語のプロンプトなら多言語モデルを使ってください。英語専用モデルのクロス言語再現率は低くなります。
- **サイズ対再現率。** 小さいモデルはクエリごとの埋め込みが速く計算コストも低い一方、大きいモデルは再現率で優位です。参考値（計測済み、ベンダー公称値ではない）: `all-MiniLM-L6-v2`（22 MB、384 次元、英語専用、最速）対 `multilingual-e5-small`（118 MB、50 言語、クロス言語再現率が良い）対 OpenAI の `text-embedding-3-small` API（`multilingual-e5-small` より MTEB で約 5 pp 高い、API コストあり）。これらの数値は *モデル自体* の特性を表しています — proxy の背後で Ollama/TEI/infinity 経由でローカルに提供する場合でも、経路 A の OpenAI 自身の API として使う場合でも同じで、reyn 固有のバックエンドではありません。
- **サーバーのエコシステム。** Ollama（上記 Step 1）で提供する場合、最も簡単な openai 互換の選択肢は `nomic-embed-text` です。HuggingFace `text-embeddings-inference` や `infinity` で提供する場合は `bge-*` / `e5-*` ファミリーがよく選ばれます。正確なサイズ / 次元 / 言語の数値はモデル自身のカードで確認してください。

まとめると: **英語利用で速さ重視 → 小さな英語モデル（`nomic-embed-text` は Ollama での妥当なデフォルト）。日本語 / 多言語 → 多言語モデル。最良の再現率が欲しく、すでに API キーがある → 経路 B ではなく経路 A を使う。**

## 未設定のときに Reyn がどう知らせるか

経路 A・B の両方をスキップしたまま LLM に「…のアクションを探して」と依頼すると、`list_actions` からの応答に、本ガイドを指す構造化された **hint** フィールドが付きます。LLM がそのヒントをあなたに伝えるため、チャットの途中でインストールが自己発見可能になります。本ガイドを暗記する必要はありません。ヒントは `search_actions` が利用可能になった瞬間に消えます。

## トラブルシューティング

**`search_actions` が LLM のツールリストに現れない** — `action_retrieval.embedding_class` がまだ `null` のままか、インデックスの構築がまだ終わっていません（= コールドスタート、数秒程度）。`reyn embeddings status` を確認してください。設定済みクラスで `ACTIONS = 0` かつ `LAST_BUILT = (never)` なら、構築が完了していません:

```bash
$ reyn embeddings status

NAME      BACKEND  MODEL                           CACHE_PATH                  SIZE_MB  ACTIONS  LAST_BUILT
────────────────────────────────────────────────────────────────────────────────────────────────────────────
light     litellm  openai/text-embedding-3-small   .reyn/cache/index/actions      0.31       87  (never)
standard  litellm  openai/text-embedding-3-small   .reyn/cache/index/actions      0.31       87  2026-05-27T19:02:00+00:00
strong    litellm  openai/text-embedding-3-large   .reyn/cache/index/actions      0.31        0  (never)
```

**Pre-flight curl が失敗する** — 上記 [§ Pre-flight](#pre-flight-opt-in) の失敗パターンを参照してください: 401（キー誤り）、404（モデル名 / proxy `model_list` 不一致）、400 unsupported-param（proxy の `drop_params: true` 未設定、#1616）、connection refused（何も listen していない / `LITELLM_API_BASE` が誤り）。

**クラスを切り替えても古い結果が返る** — Reyn のアクションインデックスは一度に 1 つの埋め込みクラスを保持します。クラスの切り替えは次セッションで自動的に再埋め込みをトリガーしますが、`reyn embeddings rebuild` で先行して強制できます。

**LLM が古い `mcp.server` / `agent.peer` カテゴリに言及する** — LLM の学習データが Reyn の collapse リファクタより前である可能性があります。Reyn 0.4 以降の `list_actions(category=["mcp.server"])` は [レガシー → 現行のマッピングを含む明示的なエラー](../../concepts/tools-integrations/universal-catalog.md#category-validation--legacy-redirect) を返すため、LLM は 1 回のリトライで自己修正します。

## 関連情報

- [`reyn embeddings` CLI リファレンス](../../reference/cli/embeddings.md) — status / rebuild / clear
- [コンセプト: universal catalog](../../concepts/tools-integrations/universal-catalog.md) — `list_actions` / `search_actions` がどう組み合わさるか
- [コンセプト: RAG](../../concepts/data-retrieval/rag.md#embedding-configuration) — 基盤となる `embedding.classes` 設定マップ（ドキュメント検索と共有）
- [Build and query a RAG corpus skill](https://github.com/tya5/reyn/blob/main/src/reyn/builtin/plugins/rag/skills/build_and_query_rag_corpus/SKILL.md) — builtin RAG プラグイン向けに書かれた同じ litellm-proxy 埋め込みセットアップ（経路 A/B）。本ガイドは `search_actions` 向けにこれをミラーしています。
