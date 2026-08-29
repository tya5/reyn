---
type: concept
topic: architecture
audience: [human, agent]
---

# Retrieval Engineering

適切なコンテキストを適切なタイミングで agent に渡すこと — 過去のやり取りの記憶、プロジェクト固有の知識、外部ドキュメント、検索結果。検索品質は多くの場合、モデルの選択よりも出力品質に大きく影響します。これは憲章が明示する 2 つの honest thin area の 1 つです(`CLAUDE.md` の Constitution 節を参照)— 以下の記述は、ギャップを取り繕うのではなく、存在するものを率直に述べる方向に寄せています。

## Reyn の実装方法

### `semantic_search` — OS 内部専用、LLM が呼び出すものではない

`semantic_search`(FP-0057 Phase 2a; `recall` から rename)は、プラガブルな `IndexBackend`(デフォルトは SQLite、≤100K チャンク、サブ秒クエリ)上で動作する Control-IR op です — クエリを埋め込み、設定された source ごとに `index_query` を実行し、top-K 結果をマージします(異なる埋め込み空間間でスコアを比較することはありません)。**FP-0066 P1b/P1c 以降、これは OS 内部の substrate であり、agent/ユーザー向けの surface ではありません**: この store に対して source を作成・検索できる LLM ツール、safe-mode Python エントリーポイント、CLI コマンドは、もはや一切残っていません。retire の全経緯と、substrate 上に現在構築されているもの(`search_actions` は現在稼働、`search_knowledge` も FP-0066 P3c 以降 稼働)については [Concepts: RAG](../data-retrieval/rag.md) を参照してください。`semantic_search`/`index_update`/`index_drop` の op kind 自体は意図して残されています(取り残しではありません)——後続の FP-0066 §8 ingest phase が これを基盤として使うためで、今日 agent 向けツールが無いにもかかわらず permission vocabulary に残っている理由もそこから従います(#5495)。

**agent に自分のドキュメントを検索させたい場合**は、代わりに組み込みの user RAG(proposal 0063)を使用してください — フォルダ内のドキュメントを、名前を付けた外部 vector store に取り込み、agent が end-to-end で呼び出せる、バンドル済みの2つのパイプラインです。Python ステップを書く必要はありません。[Build a RAG corpus](../../guide/for-users/build-a-rag-corpus.md) を参照してください。これは本節で説明している in-core index とは別の store・別のセットアップです — 両者が共有するのは `embed` プリミティブのみです。

### Memory — RAG 検索とは別の仕組み

プロジェクトおよび agent スコープの Memory(ユーザーの好み、プロジェクトの決定事項、agent 固有の習慣)は `semantic_search` の特殊ケースではなく **別の** 仕組みです: Memory は各チャットターンで router がインラインで読みます(shared レイヤーと agent スコープレイヤーからマージされた `MEMORY.md` インデックス)。ツール呼び出しでオンデマンドに問い合わせるものではありません。read/write パスは [Memory](../data-retrieval/memory.md) を参照してください。

### Web 検索

`web_search` と `web_fetch` はバンドル済みの Tier-1 default-allow ツールです — ワークフロー作者が自分で用意する必要はありません。

## まだ薄い部分

スコープを取り繕わず正直に:

- **ドキュメントのみ、user RAG プラグイン経由。** agent が呼び出せる、agent 向けの検索の物語は FP-0063 プラグインのバンドル済み ingest/query パイプラインです — フォルダ内のドキュメントを取り込み、名前を付けた外部 vector store に入れ、end-to-end でクエリできます。高度な検索(rerank / HyDE / contextual retrieval)やバンドル済みの RAG eval framework は出荷されていません。パイプライン自身の YAML が意図された拡張ポイントです(コピーして chunker/vector-store server を差し替える)、別の plugin API ではありません。
- **「何でもインデックスして LLM に検索させる」汎用 surface はありません。** この役割を担っていた OS 内部の store(`semantic_search`/`index_update`)は agent/ユーザー向けの仕組みとしては retire 済みです(FP-0066 P1b/P1c、上記参照)。ドキュメント以外のコーパス(実行トレース、独自ログ)には、現在サポートされているインデックス化経路がありません。
- **user RAG プラグイン向けにも、バンドル済みのコーパスインデックス skill はありません。** バンドルされているのはドキュメントのフォルダを渡すことだけで、任意の source をその形に合わせるのは利用者の作業です。

## 関連情報

- `CLAUDE.md`(§ Constitution)— Retrieval レンズの pass-line と、その明示的な thin-area 宣言
- [`docs/concepts/architecture/charter.md`](../architecture/charter.md) — 7 つの feature family すべてで grounded された Retrieval 行
- [`docs/concepts/data-retrieval/rag.md`](../data-retrieval/rag.md) — in-core index の完全な retire 経緯と user RAG プラグインのスコープ
- [`docs/guide/for-users/build-a-rag-corpus.md`](../../guide/for-users/build-a-rag-corpus.md) — user RAG プラグインのセットアップ
- [`docs/concepts/data-retrieval/memory.md`](../data-retrieval/memory.md) — 別の仕組みである Memory
