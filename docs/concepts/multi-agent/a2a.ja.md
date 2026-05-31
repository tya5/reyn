---
type: concept
topic: integration
audience: [human, agent]
---

# A2A (Agent2Agent Protocol)

Reyn は登録済みの各 agent を A2A アドレス可能なピアとして公開します。これにより、他の agent フレームワーク（LangGraph、CrewAI、カスタム A2A スピーカーなど）が標準のワイヤープロトコルを通じて Reyn agent を探索し、通信できます。

## A2A とは

A2A は自律型 agent 向けのピアツーピアプロトコルで、もともと Google が提案しました。各 agent は **Agent Card** をウェルノウン URL で公開し、そのアイデンティティ、capabilities、JSON-RPC エンドポイントを記述します。ピアはこのカードを取得して通信方法を確認し、JSON-RPC 2.0 でメッセージを送信します。仕様: <https://google.github.io/A2A/>

MCP との比較対比：

| Protocol | Reyn の役割 | ピアの役割 |
|---|---|---|
| **MCP** | ツールプロバイダー — `list_agents` / `send_to_agent` を公開 | Reyn をツールソースとして扱う外部 LLM クライアント |
| **A2A** | アドレス可能なピア — 各 agent が独自のエンドポイントを持つ | Reyn agent と直接会話する別の自律 agent |

どちらも同じ Reyn ウェブゲートウェイ（`reyn web`）上で動作し、同じバッキング実装（レジストリ、budget、permissions、履歴）を共有します。したがって選択する必要はありません — Reyn は両プロトコルを同時に受け付けます。

## Reyn が agent を公開する仕組み

`reyn web` が動作中の場合、登録済みのすべての Reyn agent（`.reyn/agents/` 以下の各ディレクトリ）は次の URL で自動的に公開されます：

```
GET  /a2a/agents/<name>/.well-known/agent-card.json
POST /a2a/agents/<name>                            ← JSON-RPC エンドポイント
GET  /a2a/agents                                   ← 一覧取得ヘルパー
```

Agent Card に含まれる情報：

- agent の名前（= アドレス可能なアイデンティティ）
- agent の `role` テキスト（`profile.yaml` から）を `description` として
- `capabilities` — ワイヤー上でサポートされる内容（streaming、プッシュ通知、タスクライフサイクル）
- `defaultInputModes` / `defaultOutputModes` — 現時点では `text/plain`
- `skills` — 粗粒度の `chat` capability 1 つ。Reyn 内部の Skill カタログは A2A ピアから不透明なままです（P7）。各受信メッセージに対してどの Reyn Skill を呼び出すかは agent が内部で決定します。

## サポート状況

| メソッド / 機能 | 状態 | 備考 |
|---|---|---|
| `message/send`（同期返信） | ✅ | デフォルトモード。ピアは最終返信テキストまで待機します。 |
| `message/send`（`async_mode: true` による非同期） | ✅ | A2A `Task` envelope を返し、ピアはポーリングまたは購読します。下記 [タスクライフサイクル](#タスクライフサイクルと非同期実行-fp-0001) 参照。 |
| `GET /a2a/tasks/{run_id}`（ステータスポーリング） | ✅ | `running` / `input-required` / `completed` / `failed` / `cancelled` を返します。 |
| `POST /a2a/tasks/{run_id}/cancel` | ✅ | 内部 `asyncio.Task` をキャンセル（idempotent）。 |
| `GET /a2a/tasks/{run_id}/events`（SSE ストリーム） | ✅ | Reyn ネイティブのストリーミング窓口。終了状態でクローズ。 |
| 実行中の `ask_user` 介入 | ✅ | タスクは `input-required` に遷移し、ピアは `task_id` 付き `message/send` で応答します。 |
| プッシュ通知（`params.webhook_url`） | ✅ | 各ステータス遷移で Reyn が JSON を POST。 |
| Agent Card discovery（`.well-known/agent-card.json`） | ✅ | agent ごとと multi-agent index の両エンドポイント。 |
| マルチターン履歴の永続化 | ✅ | MCP と同じ backing。agent ごとに `ChatSession.history`。 |
| `message/stream`（単独 JSON-RPC メソッド） | ❌ | 代替として上記 `/events` SSE エンドポイントを使用。 |
| 認証（bearer トークン / OAuth） | ❌ | v1 では対象外。ネットワーク層のアクセス制御に依存。 |
| テキスト以外のパーツ（`file`、`data`） | ❌ | 現状はファイルを Reyn workspace 経由で交換。 |

`message/send` が MVP の中心機能です。最も一般的な interop パターン（ピア agent が Reyn agent に質問し、最終返信テキストを受け取る）をカバーするためです。マルチターンの履歴は呼び出し間で保持されます。Reyn の `ChatSession.history` は agent ごとに永続化されており — MCP パスと同じ特性です。上に重ねた非同期タスクライフサイクル（FP-0001、下記詳細）により、ピアは長時間実行 skill を駆動し、実行中の `ask_user` に応答し、キャンセルできます。`message/send` のワイヤー形状は変わりません。

## MCP と A2A の両方を使う理由

MCP と A2A は、どちらも「外部 LLM が Reyn と通信する」という形をとりながら、異なる問題を解決します：

- **MCP** はツール呼び出しを中心に設計されています。外部 LLM のランタイムが `send_to_agent` を呼び出すタイミングを決定します。LLM の視点からは同期ツール呼び出しです。
- **A2A** はピアアドレッシングを中心に設計されています。外部 agent は Reyn agent を独自のカード、capabilities、会話状態を持つ別の自律エンティティとして扱います。ピアは Reyn を「ツール」ではなく「同僚」としてモデル化します。

Reyn にとってこれは主にワイヤーフォーマットの選択であり、基盤となるエンジンは同じです。外部システムがツール呼び出しを持つ LLM であれば MCP を選択してください。外部システム自体が agent であれば A2A を選択してください。

## タスクライフサイクルと非同期実行 (FP-0001)

A2A ピアは実行中に `ask_user` を発する skill と対話できるようになりました。
以前のバージョンは同期実行のみをサポートしており、`message/send` は
最終返信またはタイムアウトのプレースホルダーを返すだけで、実行中の回答を
注入する経路がありませんでした。

### 非同期モード

`params.async_mode: true`（または `params.webhook_url` の設定）でリクエストを
送信すると、同期待機ではなくバックグラウンドタスクとして実行されます：

```json
{
  "jsonrpc": "2.0", "id": 1, "method": "message/send",
  "params": {
    "message": {"parts": [{"kind": "text", "text": "PR をレビューして"}]},
    "async_mode": true
  }
}
```

レスポンス（= A2A Task エンベロープ）：

```json
{
  "jsonrpc": "2.0", "id": 1,
  "result": {"kind": "task", "id": "<run_id>", "status": "running", "agent_name": "..."}
}
```

### ポーリング

`GET /a2a/tasks/{run_id}` で現在の状態を取得します：

```json
{"run_id": "...", "status": "running" | "input-required" | "completed" | "failed" | "cancelled",
 "question": "...", "result": "...", "error": "..."}
```

### 実行中の ask_user

実行中の skill が `ask_user` を発すると、タスクは `input-required` に
遷移し、プロンプトテキストが `question` として公開されます。回答するには：

```json
{
  "jsonrpc": "2.0", "id": 2, "method": "message/send",
  "params": {
    "task_id": "<run_id>",
    "message": {"parts": [{"kind": "text", "text": "はい、進めてください"}]}
  }
}
```

skill が再開され、その後のポーリングでは再度 `status: "running"` が返されるか、
次の `input-required`、または最終状態が返されます。

### SSE ストリーミング

`GET /a2a/tasks/{run_id}/events` はタスクが発した events の `text/event-stream`
を返します。タスクが終端状態に達するとクローズされます。

### プッシュ通知

最初の `message/send` に `params.webhook_url` が設定されている場合、Reyn は
各ステータス遷移（`running` → `input-required` → `running` →
`completed`/`failed`/`cancelled`）時に JSON ペイロードを URL に POST します。
webhook への通信エラーはログに記録されますが、例外は raise されません — タスクは
問わず進行します。

### キャンセル

`POST /a2a/tasks/{run_id}/cancel` で基盤となる asyncio.Task をキャンセルします。
すでに終端状態にあるタスクに対してはべき等です。

### Agent Card capabilities

Agent Card は以下の capabilities を広告するようになりました：

```json
{"capabilities": {"streaming": true, "pushNotifications": true, "stateTransitionHistory": false}}
```

## 参考

- [MCP integration](../tools-integrations/mcp.md) — 対称的なケース
- [Multi-agent](../multi-agent/multi-agent.md) — Reyn 内部の agent トポロジー
