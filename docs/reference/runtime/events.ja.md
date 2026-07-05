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
  "type": "<event_kind>",
  "timestamp": "2026-04-30T10:00:00.123456+00:00",
  "data": {
    ... // kind 固有のペイロード。発行元の EventLog が設定されていれば
        // agent_id / run_id を含むことがある(下記参照)
  }
}
```

## Agent ID フィールド（全イベント共通）

`reyn.yaml` で `agent.id` が設定されているセッションから発行されるすべてのイベントのペイロードには、自動的に `agent_id` フィールドが付与されます。デフォルト値は `reyn/<hostname>` です。これにより、SOC2 / ISO 27001 / METI v1.1 要件に準拠した RBAC およびマルチエージェント監査証跡が実現されます。

詳細は [コンセプト: マルチエージェント](../../concepts/multi-agent/multi-agent.md) の「Agent ID 伝播」を参照してください。

## LLM とコンテキスト

| 種類 | 主要なペイロード |
|------|-------------|
| `llm_called` | `phase`、`model`、`input_tokens`、`output_tokens`、`latency_ms` |

## Control IR

各 Control IR op の種類は独自のイベントを発行します:

| 種類 | タイミング |
|------|------|
| `read_file`、`write_file`、`edit_file`、`delete_file`、`glob_files`、`grep`、`regenerate_index` | `file` op のバリアント — すべて `tool_executed`（`op=<sub_op>`）経由 |
| `sandboxed_exec_started`、`sandboxed_exec_completed` | `sandboxed_exec` op — `started`: `argv`、`backend`; `completed`: `argv`、`backend`、`returncode` |
| `mcp_called`、`mcp_completed`、`mcp_failed` | MCP ツール op |
| `mcp_server_installed` | `mcp_install` op — `name`、キー名のみ（値は含まない） |
| `web_search_started`、`web_search_completed`、`web_search_failed` | web_search op — `started`: `query`、`backend`; `completed`: `result_count` を追加; `failed`: `error` を追加 |
| `web_fetch_started`、`web_fetch_completed`、`web_fetch_failed` | web_fetch op — `started`: `url`; `completed`: `url`、`status_code`、`content_length`、`extractor`; `failed`: `url`、`status`（`"timeout"` または `"error"`）、`error` |
| `recall_embed_failed` | `recall` op — embed サブ op が失敗したとき: `query`、`error` |
| `index_dropped` | `index_drop` op — `source`、`chunks_dropped: int` |
| `control_ir_skipped`、`control_ir_failed` | ディスパッチ失敗（`control_ir_skipped` の理由は `handler_not_implemented`、`not_allowed_in_phase` を含む） |
| `permission_denied` | op がリゾルバーに拒否されたとき |

## MCP

上記の Control IR の `mcp_*` イベント（ツール呼び出し op に紐づく）とは異なり、これらは op ディスパッチとは独立に、MCP 接続 / receive-loop から非同期に発行されます:

| 種類 | トリガー | 主要なペイロード |
|------|---------|-------------|
| `mcp_initialized` | （再）接続のたびに、サーバーの `initialize` ハンドシェイクが完了した時点で発行。 | `server`、`negotiated_version`、`capabilities` |
| `mcp_resource_updated` | 購読中の resource のサーバープッシュ `resources/updated` 通知、またはトランスポート断からの reconnect 後に再購読された URI ごとに発火する合成 resync。フックディスパッチャーにも外部イベントフックポイントとして配線されています — [コンセプト: フック](../../concepts/runtime/hooks.ja.md#_2) 参照。 | `server`、`uri`、`resync`（reconnect resync なら `true`、実際のプッシュなら `false`） |
| `mcp_elicitation_requested` | サーバーが `elicitation/create` 構造化入力要求を発行。 | `server`、`field_keys`（要求されたスキーマのプロパティ*名*のみ — 値は決して含まない） |
| `mcp_elicitation_answered` | 要求が `accept` または `decline` に解決される（人間の選択、または `auto_decline` 設定による `decline`）。 | `server`、`field_keys`、`action`（`"accept"` \| `"decline"`） |
| `mcp_elicitation_timed_out` | `elicitation_timeout_seconds` までに回答が届かなかった。 | `server`、`field_keys` |
| `mcp_elicitation_auto_declined` | プロンプトせずに decline された — `reason` はサーバーが `elicitation: auto_decline` を設定している場合とヘッドレスコンテキスト（ライブの介入リスナーが無い）を区別する。 | `server`、`field_keys`、`reason`（`"server_configured"` \| `"headless"`） |

これらのイベントはいずれも、人間が入力した回答やフィールドの*値*を一切含みません — 要求されたスキーマのプロパティ名のみです。[コンセプト: MCP § Elicitation](../../concepts/tools-integrations/mcp.ja.md#elicitation) で説明されているセンシティブフィールドの扱いと一致します。

## クレデンシャルと OAuth

| 種類 | トリガー | 主要なペイロード |
|------|---------|-------------|
| `token_refreshed` | `reyn.secrets.get_valid_token(key)` がプロバイダーのトークンエンドポイント（RFC 6749 §6）に対して OAuth リフレッシュに成功した後に発行されます。 | `key: str` — OAuth トークンキー（`~/.reyn/oauth_tokens.json` エントリと同じ）; `expires_at: str` — 新しいアクセストークンの有効期限の ISO-8601 タイムスタンプ。 |
| `token_refresh_failed` | `get_valid_token` がトークンエンドポイントから非 2xx レスポンスを受け取るか、レスポンスペイロードが不正な形式の場合に発行されます。`OAuthRefreshError` を raise します。 | `key: str`; `error: str` — 短いエラー説明（HTTP ステータス + 利用可能な場合はプロバイダーエラーコード）。 |

**注記:**
- `token_refresh_failed` は `token_refreshed` とペアになります — ネットワークリフレッシュを実行する `get_valid_token` 呼び出しごとに、どちらか一方のみ発行されます。

関連情報: [コンセプト: シークレット処理](../../concepts/runtime/secret-handling.md) — OAuth ライフサイクルとクレデンシャルスコープ; [コンセプト: パーミッションモデル](../../concepts/runtime/permission-model.md) — スキルごとのクレデンシャルスコープ。

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

## タスク管理

タスク Control IR op（`task.py`）が発行するイベントです。

| 種類 | タイミング | 主要なペイロード |
|------|------|-------------|
| `task_op` | 任意のタスク変更操作が完了したとき（create / update-status / complete / abort） | `op`（op 種類文字列）、`task_id`、op 固有フィールド |
| `task_readiness` | タスクが `ready` または `blocked` に遷移したとき（OS の再導出で readiness が変化） | `task_id`、`to`（`"ready"` または `"blocked"`）、`trigger`（変化を引き起こした op の task_id） |
| `task_disposition` | 中断されたサブツリー内の各タスクが終端状態に達したとき | `task_id`、`disposition`（`"aborted"`）、`requester`、`origin`、`root`（ルート abort op の task_id） |
| `task_dependency_aborted` | タスクの依存先が非完了終端に達し、リクエスターが復旧を決定する必要があるとき（§16） | `task_id`（終端タスク）、`disposition`、`requester`（セッション or タスク id — §16 通知ターゲット）、`dependents`（stuck 状態の task_id リスト） |

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
| `tool_executed` | 汎用ツールディスパッチ |

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

- [control-ir.md](control-ir.md) — Control IR op
- [コンセプト: events](../../concepts/runtime/events.md)
