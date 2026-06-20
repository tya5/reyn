# セマンティックなアクション検索を有効にする

`reyn chat` には、LLM が実行可能なことを発見するための 2 つの手段が同梱されています: 高速な **`list_actions`** ブラウザ（= カテゴリ接頭辞による列挙、常時利用可能）と、**`search_actions`** セマンティック検索（= 全アクションの埋め込みインデックスに対する自然言語クエリ）です。本ガイドではセマンティックな経路を有効にする手順を説明します。

> **TL;DR**: `pip install 'reyn[local-embed]'` を一度実行すれば、`search_actions` は認証情報なしで使えるようになります。OpenAI の埋め込み API（わずかに高品質）を使いたい場合は、`reyn secret set OPENAI_API_KEY` のあと `reyn.yaml` で `action_retrieval.embedding_class: standard` を設定してください。

## どんなときに欲しくなるか

`search_actions` は次の違いを生みます:

- **無い場合**: LLM はあなたの意図がどのカテゴリ（`file` / `mcp` / `memory_entry` / …）に属するかを推測し、`list_actions(category=[...])` を実行して列挙する必要があります。_「PDF をテキストに変換するアクションを探して」_ のような自然言語の依頼では、すぐに一致が見つからないと LLM が試したうえで断ることもあります。
- **有る場合**: LLM は `search_actions(query="PDF to text")` を実行し、全カテゴリ横断で関連度順に並んだ top-K のリストを得ます。その後そのまま `describe_action` や `invoke_action` を実行できます。

`action_retrieval.embedding_class` のデフォルトは `local-mini` なので、必要な手順は `local-embed` extras のインストールだけです。extras が無い場合、Session はこれを暗黙に「クラス未設定」として扱い、`search_actions` は LLM のツールリストから **除外** され（[可視性ゲート](../../concepts/tools-integrations/universal-catalog.md#what-stays-out-of-phase-1) を参照）、`list_actions` が本ガイドを指す hidden-state ヒントを提示します。

## 経路 A — ローカルの sentence-transformers（初めての方に推奨）

```bash
pip install 'reyn[local-embed]'
```

これだけです。`local-embed` extras は `sentence-transformers` + `torch` をインストールします。デフォルトの `action_retrieval.embedding_class` はすでに `local-mini`（= `all-MiniLM-L6-v2`、22 MB、384 次元、英語）なので、import が成功した時点で配線が有効になります。`reyn.yaml` の編集は不要です。

`reyn chat` が初めて `search_actions` に到達したとき、モデルがダウンロードされ（一般的な接続で約 5〜10 秒）、埋め込みインデックスが構築されます。TUI の Memory タブはダウンロード中に `⟳ loading…` 行を、完了時に `✓ loaded · all-MiniLM-L6-v2 · 384d` 行を表示します。以降のセッションはローカルキャッシュから 1 秒未満でウォームスタートします。

### 得られるもの

- **認証情報ゼロ** — API キー不要。すべてローカルで動作します。
- **クエリごとのコストゼロ** — `local-mini` モデルは一般的なラップトップ CPU で約 30〜80 ms でクエリを埋め込みます。
- **オフライン対応** — モデルがキャッシュされれば、ネットワークアクセスなしでセマンティック検索が動作します。
- **`reyn embeddings status`** でいつでもキャッシュ状態を確認できます:

```bash
$ reyn embeddings status
NAME        BACKEND                MODEL                                  ACTIONS  LAST_BUILT
local-mini  sentence-transformers  sentence-transformers/all-MiniLM-L6-v2     87  2026-05-27T19:02:00+00:00
```

### 多言語コンテンツ

プロンプトに日本語 / 中国語 / ヨーロッパ言語が含まれる場合は、`local-e5`（= `multilingual-e5-small`、118 MB、50 言語、クロス言語の再現率が向上）に切り替えてください:

```yaml
# reyn.yaml
action_retrieval:
  embedding_class: local-e5
```

切り替え後、`reyn embeddings rebuild` で古いキャッシュを破棄すると、次のセッションが新しいモデルで再埋め込みします。

## 経路 B — OpenAI 埋め込み（わずかに高品質）

わずかに良い再現率のためにクエリごとに課金してよい場合（= OpenAI の text-embedding-3-small モデルは MTEB で `multilingual-e5-small` より約 5 pp 高いスコア）:

```bash
reyn secret set OPENAI_API_KEY
# プロンプトが出たら sk-... キーを入力
```

そして `reyn.yaml` で:

```yaml
action_retrieval:
  embedding_class: standard   # = openai/text-embedding-3-small
```

`pip` インストールは不要です。LiteLLM クライアントはすでに基本依存に含まれています。HTTP のラウンドトリップはローカル経路に比べてクエリごとに約 150〜300 ms 増えます。埋め込みコストはチャットセッションあたり約 $0.00002（= ごくわずか）です。

## GPU アクセラレーション（任意）

CUDA / Apple Silicon GPU があり、sentence-transformers にそれを使わせたい場合:

```bash
export REYN_EMBED_DEVICE=mps    # macOS Apple Silicon
export REYN_EMBED_DEVICE=cuda   # NVIDIA GPU
```

デフォルトは `cpu` です。`mps` ではエンコードのレイテンシがクエリごとに約 5〜15 ms まで下がり、長いチャットセッションで体感できるほどの段差になります。無効な値は警告を出して失敗せず `cpu` にフォールバックします。

## 未設定のときに Reyn がどう知らせるか

経路 A・B の両方をスキップしたまま LLM に「…のアクションを探して」と依頼すると、`list_actions` からの応答に、上記のインストール / 設定経路を列挙した構造化された **hint** フィールドが付きます。LLM がそのヒントをあなたに伝えるため、チャットの途中でインストールが自己発見可能になります。本ガイドを暗記する必要はありません。ヒントは `search_actions` が利用可能になった瞬間に消えます。

## トラブルシューティング

**`search_actions` が LLM のツールリストに現れない** — 埋め込みインデックスの構築がまだ終わっていない（= コールドスタート、約 5〜10 秒）か、設定されたクラスが extras 未インストールのバックエンドを指しています。`reyn embeddings status` を確認してください。設定済みクラスで `ACTIONS = 0` かつ `LAST_BUILT = (never)` なら、構築が完了していません。

**TUI / events に「failed to load \<model>」** — 部分的なキャッシュ状態です。`reyn embeddings clear` で消去してやり直してください。次のチャットセッションがクリーンに再ダウンロードします。

**クラスを切り替えても古い結果が返る** — Reyn のキャッシュは一度に 1 つの `model_class` を保持します。クラスの切り替えは次セッションで自動的に再埋め込みをトリガーしますが、`reyn embeddings rebuild` で先行して強制できます。

**LLM が古い `mcp.server` / `agent.peer` カテゴリに言及する** — LLM の学習データが Reyn の collapse リファクタより前である可能性があります。Reyn 0.4 以降の `list_actions(category=["mcp.server"])` は [レガシー → 現行のマッピングを含む明示的なエラー](../../concepts/tools-integrations/universal-catalog.md#category-validation--legacy-redirect) を返すため、LLM は 1 回のリトライで自己修正します。

## 関連情報

- [`reyn embeddings` CLI リファレンス](../../reference/cli/embeddings.md) — status / rebuild / clear
- [コンセプト: universal catalog](../../concepts/tools-integrations/universal-catalog.md) — `list_actions` / `search_actions` がどう組み合わさるか
- [コンセプト: RAG](../../concepts/data-retrieval/rag.md#embedding-configuration) — 基盤となる `embedding.classes` 設定マップ（ドキュメント検索と共有）
