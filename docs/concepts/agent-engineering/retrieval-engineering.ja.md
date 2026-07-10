---
type: concept
topic: architecture
audience: [human, agent]
---

# Retrieval Engineering

適切なコンテキストを適切なタイミングで agent に渡すこと — 過去のやり取りの記憶、プロジェクト固有の知識、外部ドキュメント、検索結果。検索品質は多くの場合、モデルの選択よりも出力品質に大きく影響します。これは憲章が明示する 2 つの honest thin area の 1 つです(`CLAUDE.md` の Constitution 節を参照)— 以下の記述は、ギャップを取り繕うのではなく、存在するものを率直に述べる方向に寄せています。

## Reyn の実装方法

### `recall` — インデックス済み任意コーパスへのベクトル検索

`recall` は LLM が直接呼び出す typed な Control IR op です: クエリを埋め込み、設定された source ごとに `index_query` を実行し、top-K 結果をグローバルにマージします。プラガブルな `IndexBackend`(デフォルトは SQLite、≤100K チャンク、サブ秒クエリ)上で動作します — キーワード/フラットインデックスマッチではありません。

コーパスのインデックス作成は意図的にバンドル済みのワンコマンド skill にはなっていません: 短い safe-mode Python ステップがファイルを読み、チャンク化し、一度 `embed_and_index()` を呼び出します。**LangChain/LlamaIndex との差別化ポイントは検索呼び出しがどこに存在するか**です — それらは自分のドライバーコードから呼び出すライブラリを提供しますが、Reyn の `recall` は通常の `reyn chat` セッション中に LLM 自身が呼び出す組み込みツールであり、検索側にオーケストレーションコードは不要です。独立した `recall_docs` の仕組みはありません — プロジェクトドキュメントも他のコーパスと同じ方法で検索されます: 一度 `embed_and_index()` でインデックス化すれば、`recall` は他の source と同様にそこに到達します。

### Memory — RAG 検索とは別の仕組み

プロジェクトおよび agent スコープの Memory(ユーザーの好み、プロジェクトの決定事項、agent 固有の習慣)は `recall` の特殊ケースではなく **別の** 仕組みです: Memory は各チャットターンで router がインラインで読みます(shared レイヤーと agent スコープレイヤーからマージされた `MEMORY.md` インデックス)。ツール呼び出しでオンデマンドに問い合わせるものではありません。read/write パスは [Memory](../data-retrieval/memory.md) を参照してください。

### Web 検索

`web_search` と `web_fetch` はバンドル済みの Tier-1 default-allow ツールです — ワークフロー作者が自分で用意する必要はありません。

## まだ薄い部分

スコープを取り繕わず正直に:

- **Phase 1 のみ。** 現在出荷されているのは framework foundation、SQLite デフォルトバックエンド、LiteLLM embedding passthrough です。Vector store のプラグインバリエーション(Qdrant / FAISS / Weaviate / Pinecone)、高度な検索(rerank / HyDE / contextual retrieval)、RAG eval framework は明示的に post-1.0 の領域です — 隠れたギャップではなく、明言された境界です。そのエコシステムが今必要なら、LangChain / LlamaIndex の方が適しています。
- **パイプラインではなく framework。** `recall` + safe-mode Python ステップが直接呼び出せるプラガブルな `IndexBackend` は、検索を構築するための foundation であり、決定論的でフルマネージドな RAG パイプラインではありません。チャンキングロジックは自分で持つ必要があります。
- **バンドル済みのコーパスインデックス skill はありません。** すべてのコーパス(ドキュメントを含む)は `recall` が到達できるようになる前に、それぞれ独自の短いインデックス作成スクリプトが必要です — `reyn index this repo` のようなワンライナーはありません。

## 関連情報

- `CLAUDE.md`(§ Constitution)— Retrieval レンズの pass-line と、その明示的な thin-area 宣言
- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — 7 つの feature family すべてで grounded された Retrieval 行
- [`docs/concepts/data-retrieval/rag.md`](../data-retrieval/rag.md) — 完全な RAG framework、クイックスタート、Phase 1/2 スコープ境界
- [`docs/concepts/data-retrieval/memory.md`](../data-retrieval/memory.md) — 別の仕組みである Memory
- [tool-contract-design.md](tool-contract-design.md) — `recall` が typed op contract にどう組み込まれるか
