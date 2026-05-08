---
type: concept
topic: architecture
audience: [human, agent]
---

# Retrieval Engineering

適切なコンテキストを適切なタイミングで agent に渡すこと — 過去のやり取りの記憶、プロジェクト固有の知識、外部ドキュメント、検索結果。検索品質は多くの場合、モデルの選択よりも出力品質に大きく影響します。

## Reyn の実装方法

現在 2 つの検索メカニズムがあり、どちらも通常の stdlib Skill として表現されています:

### `recall_memory`

プロジェクトスコープおよびユーザースコープの Memory ストアからファクトを取得します:

| スコープ | 場所 | 内容 |
|-------|----------|-------|
| Global | `~/.reyn/memory/` | ユーザーに関するファクト（役割、好み） |
| Project | `.reyn/memory/` | 現在のプロジェクトに関するファクト |

両スコープは同じ形式（`MEMORY.md` インデックス + エントリーごとの `<slug>.md`）を共有し、一緒に読み込まれます。プロジェクトエントリーが先に現れます。Skill は preprocessor を通じて検索結果を消費します:

```yaml
preprocessor:
  - run_skill:
      skill: recall_memory
      input:
        type: user_message
        data: { text: "what does the user prefer?" }
      into: relevant_memories
```

Phase は `input.relevant_memories` を通常のフィールドと同様に読み取ります。データが preprocessor から来ていることを知る必要はありません。

### `reyn chat` の自動検索

chat モードでは、各ターンが暗黙的に `recall_memory` を呼び出します（`top-k` は `chat.memory.recall_top_k` で設定可能）。また数ターンごとに `write_memory` が新しい情報を永続化する機会が与えられます。検索のケイデンスは設定によって制御され、手動でオーケストレートしません。

## まだ薄い部分

これは Reyn が現在最も取り組むべき余地があるレンズです。

**`recall_docs` はまだ実装されていません。** 計画では `recall_memory` の対称的な対として、`recall_memory` が Memory から検索するのと同様にプロジェクトのドキュメントから検索する stdlib Skill になる予定です。リリースされるまでは、ドキュメントのコンテキストを必要とする Skill は、Phase の指示に関連する段落を直接転記します。これは機能しますが、手動であり、転記はソースから乖離していきます。

**Memory マッチングはキーワード/インデックスベースであり、ベクターではありません。** `MEMORY.md` はフラットなインデックスであり、`recall_memory` はキーワードとメタデータによるクエリにマッチするエントリーを返します。数十エントリーならこれで問題ありません。数百エントリーになると関連するマッチを見逃し始めます。ベクター検索（またはハイブリッドキーワード + ベクター）が有望な次のステップですが、API サーフェス（型付き入力を持つ stdlib Skill）はすでに後で実装を交換するための正しい形になっています。

**Web 検索や外部検索のプリミティブはありません。** Web からフェッチする必要がある Skill は、設定されていれば MCP 検索ツールを呼び出します。Reyn はデフォルトの Web 検索 Skill をバンドルしていません。意図は OS を Skill に依存させないこと（P7）です。検索の種類は Skill を書くことで追加し、ランタイムを変更しません。

## このレンズが本当に問いかけていること

Retrieval Engineering は「ドキュメントを見つけられたか？」というだけではありません。「決定がそれに依存していた*そのとき*に agent がそのドキュメントを見ていたか？」です。Reyn の preprocessor メカニズムはタイミングの問いへの答えです: 検索は決定論的に、LLM 呼び出しの前に、Phase がすでに期待している場所に結果を置いて実行されます。残る作業はマッチングの問い（より良い検索、より広いソース）にあります。

## 関連情報

- [memory.md](../../../concepts/memory.md) — コンセプト
- [リファレンス: preprocessor](../../../reference/dsl/preprocessor.md)
- [tool-contract-design.md](tool-contract-design.md) — 検索がコントラクトにどう組み込まれるか
- [evaluation-and-observability.md](evaluation-and-observability.md) — 検索品質の測定
