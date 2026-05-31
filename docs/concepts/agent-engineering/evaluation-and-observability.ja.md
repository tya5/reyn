---
type: concept
topic: architecture
audience: [human, agent]
---

# Evaluation and Observability

本格的な agent システムが答えなければならない 2 つの問い: *機能しているか?*（Evaluation）と *なぜそのような動作をしているのか?*（Observability）。Reyn は同じチャネル（Events）を通じて両方に答えます。さらに、ルーブリック基準を採点する stdlib Skill（`eval`）がその Events を使用します。

## Reyn の実装方法

### Events: ランタイムの日記

すべての状態変化が構造化イベントを発行します。完全なセットは `.reyn/events/<run_id>.jsonl` の JSONL として記録されます:

- **ライフサイクル。** `workflow_started`、`phase_started`、`phase_completed`、`workflow_finished`、`phase_failed`、`loop_limit_exceeded`。
- **LLM とコンテキスト。** `context_built`、`llm_called`（トークン数とレイテンシ付き）、`validation_error`、`normalization_error`。
- **Control IR。** op kind ごとに 1 イベント、加えて `permission_denied`。
- **ユーザーインタラクション。** `user_intervention_requested`、`user_intervention_received`、`chat_started`、`chat_stopped`。

個別のロガー、トレーサー、テレメトリフックはありません。同じチャネルがライブコンソール出力、リプレイ、eval アナリティクスを動かします。

### リプレイ

```bash
reyn events .reyn/events/<run_id>.jsonl
reyn events <log> --conversation       # ターンごとの LLM コンテキスト + 生のレスポンスを表示
reyn events <log> --filter validation_error --skip context_built
```

リプレイは LLM を再呼び出ししません。保存されたログをライブランと同じフォーマットでコンソールに再レンダリングします。ログだけで事後分析に十分です。

### Eval — Phase をキーとしたルーブリック採点

`eval` stdlib Skill は、ルーブリック（基準ごとに 1 項目）に対して fan-out された `judge_phase` を使用して、ターゲット Skill の出力を Phase ごとの基準で採点します。結果は構造化レポートです: ケースごとの合否、最も弱い Phase、全体スコア、トークン使用量、コスト。

```bash
reyn eval reyn/local/my_skill/eval.md
```

関連する 2 つの stdlib Skill:

- **`eval_builder`** Skill の説明からルーブリックのドラフトを生成します。
- **`judge_phase`** は `eval` がオーケストレートする基準ごとの採点者です。

Phase をキーとした構造が重要です: 「要約がフレンドリーである」という基準は、ラン全体ではなく*要約* Phase の出力に対して採点されます。これにより、最終出力が悪化したことを単に記録するのではなく、後退の*原因となる* Phase を見つけることが可能になります。

### コストとトークンの Observability

すべての `llm_called` イベントには入出力トークン数が含まれます。`reyn run` と `reyn eval` は最後にランごとのトータル（トークン + USD コスト）を出力します。eval レポートはケースごとにそれらを永続化します。これにより「コストが下がったか？」がランをまたいで測定可能になります。

## まだ薄い部分

採点 judge 自体が LLM であるため、eval スコアは judge が継承するバイアスと分散を持ちます。緩和策としては、出力のみから検証可能な基準（数値閾値、構造チェック）の記述と、テスト対象システムより強力なモデルを judge に使用することが挙げられます。ランタイムは現在、決定論的なチェックのみの基準（例: 「出力にちょうど 3 つの箇条書きがある」）を個別のコードパスとしてサポートしていません。これらは judge が評価する基準として書かれます。

組み込みのコストダッシュボードや縦断的な eval 傾向ビューはありません。消費者はランごとのレポートを自分でパースします。データは既存の Observability ツールにプラグインするのに十分な構造を持っています。

## 関連情報

- [../runtime/events.md](../runtime/events.md) — コンセプト
- [リファレンス: events](../../reference/runtime/events.md)
- [リファレンス: stdlib/eval](../../reference/stdlib/eval.md)
- [リファレンス: stdlib/eval_builder](../../reference/stdlib/eval_builder.md)
- [リファレンス: cli/eval](../../reference/cli/eval.md)
- [ハウツー: events によるデバッグ](../../guide/for-skill-authors/operations/debug-with-events.md)
- [reliability-engineering.md](reliability-engineering.md) — 障害のための Events
- [product-think.md](product-think.md) — CLI アフォーダンスを通じた Observability の表面化
