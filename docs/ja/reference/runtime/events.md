---
type: reference
topic: runtime
audience: [human, agent]
---

# Events

Reyn はすべての状態変化に対して構造化イベントを発行します。完全なイベントログは JSONL で、`.reyn/events/<run_id>.jsonl` に書き込まれ、`reyn events <log_file>` でリプレイ可能です。

## イベントエンベロープ

すべてのイベントは以下を持ちます:

```json
{
  "ts": "2026-04-30T10:00:00.123Z",
  "kind": "<event_kind>",
  "phase": "<current_phase>",
  "run_id": "<uuid>",
  ... // kind 固有のペイロード
}
```

## ライフサイクルイベント

| 種類 | タイミング | 主要なペイロード |
|------|------|-------------|
| `workflow_started` | 最初の Phase が入ったとき | `entry_phase`、`input_artifact_type` |
| `workflow_finished` | Skill がクリーンに完了したとき | `phase`、`reason`、`confidence`、`total_phase_count`、`final_output_keys` |
| `phase_started` | 各 Phase 訪問の開始時 | `phase`、`visit_count` |
| `phase_completed` | 各 Phase 訪問の終了時 | `phase`、`next_phase`、`decision` |
| `phase_failed` | Phase が回復不能なエラーを発生させたとき | `phase`、`error` |
| `loop_limit_exceeded` | Phase が `limits.phase.max_visits` を超えたとき | `phase`、`visit_count`、`max` |
| `phase_budget_exceeded` | Phase がウォールクロックバジェット（`limits.phase.max_wall_seconds`）を超えたとき | `phase`、`elapsed`、`budget` |

## LLM とコンテキスト

| 種類 | 主要なペイロード |
|------|-------------|
| `context_built` | `phase`、`candidate_count`、`prompt_token_estimate` |
| `llm_called` | `phase`、`model`、`input_tokens`、`output_tokens`、`latency_ms` |
| `validation_error` | LLM が出力して OS が拒否したもの |
| `normalization_error` | LLM の出力をまったくパースできなかった |

## Control IR

各 Control IR op の種類は独自のイベントを発行します:

| 種類 | タイミング |
|------|------|
| `read_file`、`write_file`、`edit_file`、`delete_file`、`glob_files`、`grep` | `file` op のバリアント |
| `shell_started`、`shell`（完了）、`shell_timeout`、`shell_not_allowed` | `shell` op |
| `run_skill_started`、`skill_run_spawned`、`skill_run_failed` | `run_skill` op |
| `mcp_called`、`mcp_completed`、`mcp_failed` | MCP ツール op |
| `web_search_started`、`web_search_completed`、`web_search_failed`、`web_fetch_started` | 検索 op |
| `control_ir_skipped`、`control_ir_failed`、`control_ir_validation_error` | ディスパッチ失敗（`control_ir_skipped` の理由は `shell_not_allowed`、`handler_not_implemented`、`not_allowed_in_phase` を含む） |
| `permission_denied` | op がリゾルバーに拒否されたとき |

## ユーザーインタラクション

| 種類 | タイミング |
|------|------|
| `user_message_received` | 新しいユーザーターンがランタイムに入ったとき。`chain_id`（`submit_user_text` がミントし、このターンが生成するすべての agent 間メッセージに伝播される uuid）を持つ |
| `user_intervention_received` | `ask_user` op が回答を受け取ったとき |
| `chat_started`、`chat_stopped` | chat セッションのライフサイクル |

## Skill スポーニング（chat）

| 種類 | タイミング |
|------|------|
| `skill_run_spawned` | ルーターの決定から Skill が起動されたとき（`run_id`、`skill`） |
| `skill_spawn_refused` | `_spawn_skill` が agent の `allowed_skills` にない Skill を拒否したとき。ペイロード: `reason="allowlist"`、`skill`、`agent` |

## agent 間メッセージング

| 種類 | タイミング | 主要なペイロード |
|------|------|-------------|
| `agent_message_sent` | `_send_to_agent` または `_send_agent_response` がペイロードを届けたとき | `kind=agent_request\|agent_response`、`from_agent`、`to_agent`、`depth`、`chain_id` |
| `agent_request_received` | 受信 agent が受信トレイから `agent_request` を取り出したとき | `from_agent`、`depth`、`chain_id` |
| `agent_response_received` | 発信元 agent が受信トレイから `agent_response` を取り出したとき | `from_agent`、`depth`、`chain_id` |
| `agent_message_refused` | 送信が拒否されたとき（例: `multi_agent.max_hop_depth` を超えた） | `reason`、`to_agent`、`depth`、`chain_id` |
| `chain_timeout` | 保留中のチェーンが `multi_agent.chain_timeout_seconds` を超え、上流の合成エラーレスポンスで強制解決されたとき | `chain_id`、`waiting_on`（返信していなかった agent のソート済みリスト）、`timeout_seconds`、`origin_agent` |

`chain_id` は uuid4 hex。トップレベルのユーザー送信ごとに 1 つ、すべてのホップを通じて変更されずに伝播します。クロス agent の再構築は各 agent の `events.jsonl` と `history.jsonl` に対する `grep <chain_id>` です。

## Workspace

| 種類 | タイミング |
|------|------|
| `workspace_updated` | 任意の artifact が書き込まれたとき |
| `tool` / `tool_executed` | 汎用ツールディスパッチ |

## リプレイ

```bash
reyn events .reyn/events/<run_id>.jsonl
```

保存されたログをライブランと同じフォーマットでコンソールに再レンダリングします。LLM は再呼び出しされません。リプレイは検査のみを目的としています。

## すべてがイベントである理由

「すべての状態変化が発行する」から 2 つの帰結が生まれます:

- **再現性。** 保存されたログは実行の完全な記録です。将来のチェックポイント/再開の設計（ロードマップ参照）はこれに基づいて構築されます。
- **ボルトオンなしの Observability。** 個別のロガー、トレーサー、テレメトリフックなし。同じチャネルがデバッグ出力、リプレイ、（将来的には）eval アナリティクスを動かします。

## 関連情報

- [run.md](../cli/run.md) — `--events` フラグ
- [control-ir.md](control-ir.md) — Control IR op
- [コンセプト: events](../../concepts/events.md)
