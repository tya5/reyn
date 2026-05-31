---
type: concept
topic: architecture
audience: [human, agent]
---

# Events

reyn のすべての状態変化は event を発行します。event ログはランタイムの日誌です。何が起きたかを順番に、実行を再生するのに十分な詳細を持つ JSONL ストリームとして記録します。

## なぜすべてが event なのか

独立したロガー、トレーサー、テレメトリーフックは存在しません。同じチャンネルが次のすべてを担います：

- **ライブデバッグ出力。** コンソールレポーターが event ストリームをサブスクライブし、各 event が到着するたびに描画します。
- **再生。** `reyn events <log_file>` は保存されたログをコンソールに再描画します。LLM の再呼び出しは不要です。
- **Eval 分析。** Eval レポートは（トークン使用量、Phase 数、バリデーションエラーなどの）event データをケースごとに集計します。
- **将来のチェックポイント/再開。** 完全な event ログは実行の完全な記述です。N からの再開は、N 番目の event までログを再生するだけです。

OS が唯一のミューテーターであり（P3）、すべてのミューテーションが event を発行するなら、ログだけで十分です。「他に何が起きたのか」を追う必要はありません。

## 何が記録されるか

主な 3 つのバケット、加えていくつかの小さなもの：

- **ライフサイクル** — `workflow_started`、`phase_started`、`phase_completed`、`workflow_finished`、`phase_failed`、`loop_limit_exceeded`。
- **LLM とコンテキスト** — `context_built`、`llm_called`、`validation_error`、`normalization_error`。
- **Control IR** — op の種類ごとに 1 つの event（`read_file`、`write_file`、`shell_started`、`run_skill_started` など）と `permission_denied`。
- **チャットライフサイクル** — `chat_started`、`chat_stopped`、`user_message_received`、`skill_run_spawned`、`skill_spawn_refused`。
- **Agent 間メッセージング** — `agent_message_sent`、`agent_request_received`、`agent_response_received`、`agent_message_refused`。各 event は `chain_id` を持つため、1 つのユーザーリクエストを複数のホップにまたがって追跡できます。

完全な分類は [events リファレンス](../../reference/runtime/events.md) にあります。

## event とは何か

すべての event は安定したエンベロープを持ちます：

```
ts        — ISO-8601 タイムスタンプ
kind      — event の種類（リファレンス参照）
phase     — 発行時の現在 Phase
run_id    — 実行の uuid
... kind 固有のペイロードフィールド
```

安定した形状により、コンシューマーごとのカスタムパーサーなしにログをマシン読み取り可能にします。

### 監査フィールド付き event (FP-0021)

8 種類の event に対して、`data` dict に特定の監査フィールドが
必須となりました。権威ある registry は `src/reyn/events/event_schema.py`
（`EVENT_AUDIT_REQUIREMENTS`）にあります。Tier 2 不変条件テスト
（`tests/test_event_audit_invariants.py`）が CI の各実行で各 event 種に宣言済みの
フィールドが含まれていることを検証します。

| Event 種                        | 必須フィールド                          |
|---------------------------------|-----------------------------------------|
| `workflow_started`              | `run_id`、`skill`                       |
| `workflow_finished`             | `run_id`、`skill`                       |
| `llm_called`                    | `run_id`、`skill`                       |
| `llm_response_received`         | `run_id`、`skill`                       |
| `permission_granted`            | `run_id`、`skill`、`phase`              |
| `permission_denied`             | `run_id`、`skill`、`phase`              |
| `user_intervention_requested`   | `run_id`、`skill`、`intervention_id`   |
| `user_intervention_received`    | `run_id`、`skill`、`intervention_id`   |

enforcement はテスト時のみ（`emit()` ランタイムではなし）で、本番オーバーヘッドをゼロに保ちます。

## event ではないもの

- **アプリケーションログではありません。** Skill の作成者は自由形式の event を発行すべきではありません。セットは OS が定義します。
- **Memory ではありません。** Events は実行ごとのランタイム記録です。Memory は実行をまたがる知識です。[../data-retrieval/memory.md](../data-retrieval/memory.md) を参照してください。
- **artifact のシングルソースオブトゥルースではありません。** Artifact は workspace チャンネルを通過します。Events はその通過を記録します。

## デバッグツールとして events を読む

何かおかしいと思ったら：

1. 実行出力の最終行（`events saved → ...`）から実行 ID を見つける。
2. `reyn events .reyn/events/<run_id>.jsonl --conversation` で、各 LLM 呼び出しのコンテキストと返答を確認する。
3. または `--filter validation_error --filter normalization_error` で OS が出力を拒否した箇所に直接ジャンプする。

デバッガーは不要です。ログにすでに必要な情報があります。

## 参考

- [Reference: events](../../reference/runtime/events.md) — 完全な event 分類
- [Reference: events CLI](../../reference/cli/run.md) — `reyn run` の `--events` フラグ
- [How-to: debug with events](../../guide/for-skill-authors/operations/debug-with-events.md)

Events は監査証跡であるだけでなく、タイムトラベルデバッグの基盤でもあります。
`--mode replay`（過去の実行をステップごとに追う）と `--mode compare`（2 つの実行を
並べて差分確認する）については [reference/dogfood-tracing.md](../../reference/dogfood-tracing.md)
を参照してください。
