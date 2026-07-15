---
type: concept
topic: architecture
audience: [human, agent]
---

# Evaluation

agent の出力が実際に良いかどうかを採点すること — スキーマとして valid かどうかだけではありません。目標は「型チェックだけでなく、判断が必要な重要な決定にゲートをかけられること」です。

## Reyn の実装方法

### `agent` step + `schema`

専用のスコアラー op はありません。出力を rubric に対して採点するのは、通常の pipeline 合成です: `schema:` に小さなスキーマ(例: `{score: number, reason: string}`)を指定した pipeline `agent` step の後に、パースされたスコアを閾値と比較する素の `transform` step を続けます。

```yaml
pipeline: self_review
steps:
  - agent:
      prompt: "Self-review {ctx.draft} against your own checklist: ... Give a score in [0.0, 1.0] and a short reason."
      schema: Verdict
      output: verdict
  - transform: {value: "ctx.verdict.score >= 0.6", output: passed}
---
schema: Verdict
fields:
  score: {type: number}
  reason: {type: string}
```

ここでの OS の実質的な貢献は `schema:` です: **agent の生成を制約し**(スキーマから組み立てた `response_format` により、自由形式のテキストではなくスキーマに準拠した JSON で答えさせる)、**パース結果を検証します**(念のための二重チェック — provider の制約を鵜呑みにはしません)。閾値比較は素の `if`(`transform` step)であり、専用の op ではありません。コストは他のあらゆる `agent` step と同じ経路で追跡されます — 別のコスト経路を配線する必要はありません。

OS はチェックリストの内容を一切解釈しません — それは呼び出し側の agent 自身が書いた評価基準であり、自分が書いた prompt の一部です。**これは自己レビューであり、客観性ではありません**: 下書きを作った agent(あるいは同じモデルファミリー)がチェックリストも書き、それに対して採点します — 自分のチェックリストが挙げ、下書きが見落とした要件を拾う点では有用ですが、独立した審判ではありません。そう見せかけないでください。

### `reyn run-once`

ライブの承認プロンプトなしで agent を実行する非対話型の CLI エントリーポイントです(`reyn eval` は phase-graph 時代のコマンドで、その engine と共に削除されました — `reyn run-once` が現行の生きている対応物です)。実行開始前に permission がすでに事前承認されている必要があります — 例えば `--grant-file-write` は対話的プロンプトではなく起動時に特定のケイパビリティを付与します。これが自己レビューでゲートされた pipeline を CI で使えるものにしています: スコアリングループと permission モデルは直交しているため、非対話ランの trust 決定は起動ごとに再検討されるのではなく、事前に一度だけ行われます。

## まだ薄い部分

これは憲章が明示する 2 つの honest thin area の 1 つです(`CLAUDE.md` の Constitution 節と [`docs/concepts/architecture/charter.md`](../architecture/charter.md) の Evaluation 行を参照)。`agent` step + `schema` が評価サーフェスのすべてです — rubric ライブラリも、複数 judge によるコンセンサス/投票も、組み込みの eval-suite ランナーも、run のバッチをまたぐ集計スコアリングもありません。それらを望む作者は、`agent`+`schema` の自己レビュー step と通常の制御フローを組み合わせて自分で構成します。OS が提供するのは typed な生成のプリミティブであり、その上に構築された評価フレームワークではありません。

## 関連情報

- [リファレンス: pipeline-dsl.md § `AgentStep` / `schema`](../../reference/runtime/pipeline-dsl.md)
- [リファレンス: events](../../reference/runtime/events.md) — `agent` step のコスト/完了イベントが記録される audit-event 分類
- [reliability-engineering.md](reliability-engineering.md) — 判断ではなく検証が基準となる場合に何が起こるか
