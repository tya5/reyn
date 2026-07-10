---
type: concept
topic: architecture
audience: [human, agent]
---

# Evaluation

agent の出力が実際に良いかどうかを採点すること — スキーマとして valid かどうかだけではありません。目標は「型チェックだけでなく、判断が必要な重要な決定にゲートをかけられること」です。

## Reyn の実装方法

### `judge_output`

typed な Control IR op です: 現在の workspace artifact 内の `target` dot-path を解決し、呼び出し元が指定した `rubric` で LLM を呼び出し、スコア(`0.0`–`1.0`)と `threshold`(デフォルト `0.8`)に対する `passed` フラグを返します。

```json
{
  "kind": "judge_output",
  "target": "artifact.data.summary",
  "rubric": "Score 0.0-1.0: is the summary concise, accurate, and complete?",
  "threshold": 0.8,
  "on_fail": "transition"
}
```

OS は `rubric` の内容を一切解釈しません — これは skill 作者自身の評価基準であり、検査なしで LLM にルーティングされます。`on_fail`(`"transition"` / `"abort"` / `"continue"`)は呼び出し元が対応するために結果に記録されます — op ハンドラー自体はこの値で分岐しません。`on_fail` を実際の制御フロー決定に解釈するのは、OS ではなく呼び出し側の agent 自身の責任です。

`judge_output` の呼び出しごとに P6 audit-event(`op=judge_output`、`target`、`score`、`passed`、`threshold`、`reason` を伴う `tool_executed`)が発行されます — スコアリングされた決定も他の op と同様に監査可能です。

### `reyn run-once`

ライブの承認プロンプトなしで agent を実行する非対話型の CLI エントリーポイントです(`reyn eval` は phase-graph 時代のコマンドで、その engine と共に削除されました — `reyn run-once` が現行の生きている対応物です)。実行開始前に permission がすでに事前承認されている必要があります — 例えば `--grant-file-write` は対話的プロンプトではなく起動時に特定のケイパビリティを付与します。これが `judge_output` でゲートされたランを CI で使えるものにしています: スコアリングループと permission モデルは直交しているため、非対話ランの trust 決定は起動ごとに再検討されるのではなく、事前に一度だけ行われます。

## まだ薄い部分

これは憲章が明示する 2 つの honest thin area の 1 つです(`CLAUDE.md` の Constitution 節と [`docs/concepts/architecture/charter.md`](../architecture/charter.md) の Evaluation 行を参照)。`judge_output` が評価サーフェスのすべてです — rubric ライブラリも、複数 judge によるコンセンサス/投票も、組み込みの eval-suite ランナーも、run のバッチをまたぐ集計スコアリングもありません。それらを望む skill 作者は、`judge_output` の呼び出しと通常の制御フローを組み合わせて自分で構成します。OS が提供するのはスコアリングのプリミティブであり、その上に構築された評価フレームワークではありません。

## 関連情報

- [リファレンス: control-ir.md § `judge_output`](../../reference/runtime/control-ir.md)
- [リファレンス: events](../../reference/runtime/events.md) — `judge_output` の結果が記録される audit-event 分類
- [reliability-engineering.md](reliability-engineering.md) — 判断ではなく検証が基準となる場合に何が起こるか
