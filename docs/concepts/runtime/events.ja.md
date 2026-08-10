---
type: concept
topic: architecture
audience: [human, agent]
---

# Events

reyn のすべての状態変化は audit-event を発行します。audit-event ログはランタイムの日誌です。何が起きたかを順番に、実行を再生するのに十分な詳細を持つ JSONL ストリームとして記録します。

## なぜすべてが audit-event なのか

独立したロガー、トレーサー、テレメトリーフックは存在しません。同じチャンネルが次のすべてを担います：

- **ライブデバッグ出力。** コンソールレポーターが audit-event ストリームをサブスクライブし、各 audit-event が到着するたびに描画します。
- **再生。** `reyn events <log_file>` は保存されたログをコンソールに再描画します。LLM の再呼び出しは不要です。
- **Eval 分析。** Eval レポートは（トークン使用量、バリデーションエラーなどの）audit-event データをケースごとに集計します。
- **クラッシュリカバリ（実装済み）。** クラッシュリカバリは WAL（`.reyn/state/wal.jsonl`）と seq 付きスナップショットを基盤として agent 状態を再構築します — audit-event ログではありません。ユーザー向け rewind/resume（PITR + グローバル rewind）は別の設計です。[Time-travel](time-travel.ja.md) を参照してください。

OS が唯一のミューテーターであり（P3）、すべてのミューテーションが audit-event を発行するなら、audit-event ログだけで十分です。「他に何が起きたのか」を追う必要はありません。

## 何が記録されるか

主なバケットのいくつか：

- **LLM とコンテキスト** — `llm_called`。
- **Control IR** — op の種類ごとに 1 つの audit-event（`sandboxed_exec_started`、`mcp_called`、`web_search_started`、`semantic_search_embed_failed`）と `permission_denied`。read のような `file` op は独立した kind ではなく、共有の `tool_executed` kind に乗り、具体的な操作は `op` フィールドで名指しされます。
- **ユーザーとのやり取り** — `user_message_received`、`user_intervention_received`、`chat_started`、`chat_stopped`、`turn_cancelled`。
- **Agent 間メッセージング** — `agent_message_sent`、`agent_request_received`、`agent_response_received`、`agent_message_refused`、`chain_timeout`。各 audit-event は `chain_id` を持つため、1 つのユーザーリクエストを複数のホップにまたがって追跡できます。

上記のバケットは例です。`type` フィールドは**閉じた語彙**であり、reyn が発行する kind の全体（それ以外は発行しません）は [events リファレンス](../../reference/runtime/events.md#kind-vocabulary) に列挙されています。列挙は `src/reyn/core/events/event_schema.py` の単一の SSoT から導出され、発行側コードとページの双方に対して CI で検査されます。したがって reyn の外にいる消費者も、受け取りうる type の全体を列挙できます。

### Task subscription event — 過去に設計され、現在は削除済みの WAL 機構

Task↔session の紐付け変更（assignee/requester）は、P6 audit-event ログではなく **WAL** kind（`task_subscribed`、`task_rebound` — StateLog、`.reyn/state/wal.jsonl`）として記録する*設計*でした（#2187）——この機構が生きているなら探す先は audit-event ログではなくこちらだったはずです。しかし書き込み箇所は一度も実装されず、#3436 でこの2つの kind は WAL の語彙から削除されました（書き手のいない死んだ宣言）。Task↔session の紐付け変更は現在どこにも記録されていません。WAL は一般にクラッシュリカバリと time-travel の基盤であり、audit-event ログは実行ごとのトレースです。両者は耐久性契約の異なる別々のログです（[Time-travel](time-travel.ja.md) の「WAL vs audit-event 分離」を参照）。

## audit-event とは何か

すべての audit-event は安定したエンベロープを持ちます：

```
type      — event の種類（リファレンス参照）
timestamp — ISO-8601 タイムスタンプ
data      — 種類ごとのペイロードフィールドを持つフラットな dict
```

多くの audit-event に存在する主要フィールド（`data` 内）：

```
run_id    — 実行の uuid（run スコープの audit-event の多くに存在）
```

注: `run_id` は run スコープの audit-event の多く（`llm_called`、
`permission_denied` などが例 — 網羅的な列挙ではない。#3410 参照）に
存在しますが、run コンテキスト外で発行される一部の audit-event（例:
`chat_started`）には存在しません。

### 必須フィールド付き audit-event (FP-0021)

拡大しつつある audit-event 種の集合が、`data` dict に特定の監査フィールドを
持つことを要求されています（必須フィールドは種ごとに異なります — 例:
`llm_called` は `model` を要求し、`permission_granted`/`permission_denied`
は `run_id`、`actor`、`phase` を要求します）。権威ある最新の registry は
`src/reyn/core/events/event_schema.py`（`EVENT_AUDIT_REQUIREMENTS`）にあります
— このリストは時間とともに増え続けているため、ここには複製しません。
各 feature 専用の不変条件テスト(例: `tests/core/test_session_lifecycle_events_1800.py`、
`tests/runtime/test_mcp_search_tool_invariants.py`、
`tests/dev/test_chat_turn_completed_inline.py`)が、それぞれの event 種がここに
正しい必須フィールドで宣言されていることを CI の各実行で検証します。

enforcement はテスト時のみ（`emit()` ランタイムではなし）で、本番オーバーヘッドをゼロに保ちます。

安定した形状により、コンシューマーごとのカスタムパーサーなしにログをマシン読み取り可能にします。

## audit-event ではないもの

- **アプリケーションログではありません。** ワークフローの作成者は自由形式の audit-event を発行すべきではありません。セットは OS が定義します。
- **Memory ではありません。** Audit-event は実行ごとのランタイム記録です。Memory は実行をまたがる知識です。[../data-retrieval/memory.md](../data-retrieval/memory.md) を参照してください。
- **artifact のシングルソースオブトゥルースではありません。** Artifact は workspace チャンネルを通過します。Audit-event はその通過を記録します。

## デバッグツールとして audit-event を読む

何かおかしいと思ったら：

1. 実行出力の最終行（`events saved → ...`）から実行 ID を見つける。
2. `reyn events .reyn/events/<run_id>.jsonl --conversation` で、各 LLM 呼び出しのコンテキストと返答を確認する。
3. または `--filter permission_denied` で OS が op を拒否した箇所に直接ジャンプする。

デバッガーは不要です。ログにすでに必要な情報があります。

## 参考

- [Reference: events](../../reference/runtime/events.md) — 完全な audit-event 分類

Audit-event は実行ごとのトレースであり、クラッシュリカバリや time-travel の
基盤ではありません — それらは WAL ベースです（[Time-travel](time-travel.ja.md)
を参照）。ペイロードレベルのトレース検査については
[reference/dogfood-tracing.md](../../reference/dogfood-tracing.md) を参照してください。
