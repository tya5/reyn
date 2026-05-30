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

## Agent ID フィールド（全イベント共通）

`reyn.yaml` で `agent.id` が設定されているセッションから発行されるすべてのイベントのペイロードには、自動的に `agent_id` フィールドが付与されます。デフォルト値は `reyn/<hostname>` です。これにより、SOC2 / ISO 27001 / METI v1.1 要件に準拠した RBAC およびマルチエージェント監査証跡が実現されます。

詳細は [コンセプト: マルチエージェント](../../concepts/multi-agent.md) の「Agent ID 伝播」を参照してください。

## ライフサイクルイベント

| 種類 | タイミング | 主要なペイロード |
|------|------|-------------|
| `workflow_started` | 最初の Phase が入ったとき | `entry_phase`、`input_type`、`default_model` |
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
| `read_file`、`write_file`、`edit_file`、`delete_file`、`glob_files`、`grep`、`regenerate_index` | `file` op のバリアント — すべて `tool_executed`（`op=<sub_op>`）経由 |
| `shell_started`、`shell`（完了）、`shell_timeout`、`shell_not_allowed` | `shell` op |
| `sandboxed_exec_started`、`sandboxed_exec_completed` | `sandboxed_exec` op — `started`: `argv`、`backend`; `completed`: `argv`、`backend`、`returncode` |
| `run_skill_started`、`skill_run_spawned`、`skill_run_failed` | `run_skill` op — `run_skill_started` は `skill_version_hash: str`（実行時の `skill.md` 内容の sha256 hex。`skill.md` が存在しない場合は `"unknown"`）を持つ |
| `mcp_called`、`mcp_completed`、`mcp_failed` | MCP ツール op |
| `mcp_server_installed` | `mcp_install` op — `name`、キー名のみ（値は含まない） |
| `web_search_started`、`web_search_completed`、`web_search_failed`、`web_fetch_started` | 検索 op |
| `embed_progress` | `embed` op（Form B artifact 参照のみ）— バッチごとの `embedded: int`、`skipped: int` 累積カウント |
| `recall_embed_failed` | `recall` op — embed サブ op が失敗したとき: `query`、`error` |
| `index_dropped` | `index_drop` op — `source`、`chunks_dropped: int` |
| `skill_resolve_completed` | `skill_resolve` op — `name`、`resolved: bool`、`source: "local"\|"project"\|"stdlib"\|null` |
| `control_ir_skipped`、`control_ir_failed`、`control_ir_validation_error` | ディスパッチ失敗（`control_ir_skipped` の理由は `shell_not_allowed`、`handler_not_implemented`、`not_allowed_in_phase` を含む） |
| `permission_denied` | op がリゾルバーに拒否されたとき |

## クレデンシャルと OAuth

| 種類 | トリガー | 主要なペイロード |
|------|---------|-------------|
| `sub_skill_credential_scope` | `run_skill` op ハンドラーがサブスキル入口で、OS が有効なクレデンシャルスコープ（サブスキルの `required_credentials` と親スコープの交差）を算出した後に発行されます。 | `skill: str` — サブスキル参照（`op.skill` と同じ値）; `allowed_keys: list[str]` — 許可されたシークレットキーのソート済み重複排除リスト。有効スコープが無制限の場合は `["*"]`。 |
| `token_refreshed` | `reyn.secrets.get_valid_token(key)` がプロバイダーのトークンエンドポイント（RFC 6749 §6）に対して OAuth リフレッシュに成功した後に発行されます。 | `key: str` — OAuth トークンキー（`~/.reyn/oauth_tokens.json` エントリと同じ）; `expires_at: str` — 新しいアクセストークンの有効期限の ISO-8601 タイムスタンプ。 |
| `token_refresh_failed` | `get_valid_token` がトークンエンドポイントから非 2xx レスポンスを受け取るか、レスポンスペイロードが不正な形式の場合に発行されます。`OAuthRefreshError` を raise します。 | `key: str`; `error: str` — 短いエラー説明（HTTP ステータス + 利用可能な場合はプロバイダーエラーコード）。 |

**注記:**
- `sub_skill_credential_scope` は監査グレードのイベントで、ネストされたスキル実行間のクレデンシャル認可チェーンを再構築するために使用されます。同じ `skill` 名の `run_skill_started` とペアになります。
- `token_refresh_failed` は `token_refreshed` とペアになります — ネットワークリフレッシュを実行する `get_valid_token` 呼び出しごとに、どちらか一方のみ発行されます。

関連情報: [コンセプト: シークレット処理](../../concepts/secret-handling.md) — OAuth ライフサイクルとクレデンシャルスコープ; [コンセプト: パーミッションモデル](../../concepts/permission-model.md) — スキルごとのクレデンシャルスコープ; [DSL リファレンス: `required_credentials`](../dsl/skill-md.md)。

## アクションカタログルーティング

| 種類 | トリガー | 主要なペイロード |
|------|---------|-------------|
| `routing_decided` | アクションラッパー（`list_actions` / `search_actions` / `describe_action` / `invoke_action`）がリクエストをルーティングするとき、ユニバーサルアクションカタログのディスパッチパスから発行されます。 | `action_name: str`; `source: str` — `"catalog"` \| `"hot_alias"` \| `"direct"`; `outcome: str` — `"dispatched"` \| `"deflected"` \| `"error"`; `chain_id: str` — クロスコール相関用リクエストチェーン識別子。 |

**注記:** ラッパーオンリーのルーティングパスの監査が可能です。`chain_id` を使って、アクションのダウンストリームイベントとのクロスコリレーションが行えます。

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
| `agent_message_refused` | 送信が拒否されたとき（例: `safety.loop.max_agent_hops` を超えた） | `reason`、`to_agent`、`depth`、`chain_id` |
| `chain_timeout` | 保留中のチェーンが `safety.timeout.chain_seconds` を超え、上流の合成エラーレスポンスで強制解決されたとき | `chain_id`、`waiting_on`（返信していなかった agent のソート済みリスト）、`timeout_seconds`、`origin_agent` |

`chain_id` は uuid4 hex。トップレベルのユーザー送信ごとに 1 つ、すべてのホップを通じて変更されずに伝播します。クロス agent の再構築は各 agent の `events.jsonl` と `history.jsonl` に対する `grep <chain_id>` です。

## Workspace

| 種類 | タイミング |
|------|------|
| `workspace_updated` | 任意の artifact が書き込まれたとき |
| `tool` / `tool_executed` | 汎用ツールディスパッチ |

## スキル管理 {#skill-management}

| 種類 | ペイロードフィールド | 発行タイミング |
|------|-------------|--------------|
| `skill_rolled_back` | `skill: str`、`from_version: int`、`to_version: int`、`reason: str`（デフォルト `"user rollback via CLI"`） | `reyn skill rollback` が以前のバージョンを復元したとき。`.reyn/events/direct/cli/<YYYY-MM-DD>.jsonl` に書き込まれます。[リファレンス: CLI — `reyn skill rollback`](../cli/skill.md) を参照。 |

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
