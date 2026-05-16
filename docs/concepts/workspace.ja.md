---
type: concept
topic: architecture
audience: [human, agent]
---

# Workspace

Workspace は、Reyn がランの実行中に生成するすべてのもの（artifact、中間ファイル、ツール出力、イベントログ）の唯一の信頼できる情報源です。Phase はメモリ上のサイドチャネルを通じて通信することはありません。Phase が後続の Phase と何かを共有したい場合は、Workspace を経由します。

## Workspace に格納されるもの

| 種別 | 場所 |
|------|-------|
| 現在の artifact（入力 → 次の Phase） | Phase 間はメモリ上、トランジション時に永続化 |
| `file.write` Control IR op が書き込んだファイル | Skill が指定した Workspace ルート配下 |
| サブ Skill の出力（`run_skill` op から） | 呼び出し元 Phase の入力の名前付きスロットに束縛 |
| イベントログ | `.reyn/events/<run_id>.jsonl` |
| Eval レポート | `.reyn/eval-results/<skill>/<timestamp>.json` |

## 単一ソースである理由

1 つの Workspace を持つことから、2 つの帰結が生まれます:

- **再現性。** すべての書き込みが OS を経由し、イベントを発行するため、イベントログだけでワークフローが何を参照したかを再構築できます。OS が見落とす可能性のある「隠れた状態」は存在しません。
- **決定論的な境界。** 何か問題が発生した場合、「この Phase は正しい入力を受け取りましたか？」という問いは、Workspace の `cat` 1 回で答えられます。照合すべき第 2 のソースは存在しません。

## Phase 間のデータフロー

Phase は artifact を生成します。OS は:

1. artifact を次のターゲットのスキーマに対して検証します。
2. それを次の Phase 訪問の入力として格納します。
3. 必要に応じて Control IR op（ファイル書き込み、サブ Skill 呼び出し）を実行し、Workspace を変更する場合があります。

Phase は互いに直接データを渡しません。連鎖は常に Phase → OS → Phase です。

## ファイルと artifact

ファイルはディスク上に存在し、artifact は Phase 間の OS の型付きチャネル上に存在します。どちらも「Workspace の状態」ですが、トランジションの検証に参加するのは artifact のみです。

ファイルを使う場合:

- データが大きい、またはファイルとして自然な形式である（生成されたレポート、トランスクリプト）
- 複数の Phase がそれぞれ異なる部分を取り出す
- ユーザーがランの後で読む

artifact を使う場合:

- データが構造化されており、下流の Phase が検証する必要がある
- データがグラフの単一のエッジに沿って流れる

## 関連情報

- [principles.md](principles.md) — P3（OS が実行を制御）、P5（Workspace が唯一の信頼できる情報源）
- [リファレンス: artifact.yaml](../reference/dsl/artifact-yaml.md)
- [リファレンス: control-ir](../reference/runtime/control-ir.md)
- [events.md](events.md)
