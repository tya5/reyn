---
type: concept
topic: architecture
audience: [human, agent]
---

# Workspace

Workspace は、Reyn がランの実行中に生成するすべてのもの（中間ファイル、ツール出力、イベントログ）の唯一の信頼できる情報源です。すべての書き込みは OS を経由し、イベントを発行します。

## Workspace に格納されるもの

| 種別 | 場所 |
|------|-------|
| `file.write` Control IR op が書き込んだファイル | エージェントが指定した Workspace ルート配下 |
| イベントログ | `.reyn/events/<run_id>.jsonl` |
| Eval レポート | `.reyn/eval-results/<skill>/<timestamp>.json` |

## 単一ソースである理由

1 つの Workspace を持つことから、2 つの帰結が生まれます:

- **再現性。** すべての書き込みが OS を経由し、イベントを発行するため、イベントログだけでワークフローが何を参照したかを再構築できます。OS が見落とす可能性のある「隠れた状態」は存在しません。
- **決定論的な境界。** 何か問題が発生した場合、「ランは正しい入力を受け取りましたか？」という問いは、Workspace の `cat` 1 回で答えられます。照合すべき第 2 のソースは存在しません。

## 関連情報

- [リファレンス: control-ir](../../reference/runtime/control-ir.md)
- [../runtime/events.md](../runtime/events.md)
