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

| Method | v1 | v2（予定） |
|---|---|---|
| `message/send` | ✅ 同期返信 | — |
| `message/stream` | ❌ | streaming SSE レスポンス |
| `tasks/get` / `tasks/cancel` | ❌ | 長時間実行のタスクライフサイクル |
| プッシュ通知 | ❌ | コールバック形式の結果 |
| 認証 | ❌ | bearer トークン / OAuth |
| テキスト以外のパーツ（`file`、`data`） | ❌ | Reyn workspace 経由のファイルアップロード |

`message/send` が MVP の中心機能です。最も一般的な interop パターン（ピア agent が Reyn agent に質問し、最終返信テキストを受け取る）をカバーするためです。マルチターンの履歴は呼び出し間で保持されます。Reyn の `ChatSession.history` は agent ごとに永続化されており — MCP パスと同じ特性です。

## MCP と A2A の両方を使う理由

MCP と A2A は、どちらも「外部 LLM が Reyn と通信する」という形をとりながら、異なる問題を解決します：

- **MCP** はツール呼び出しを中心に設計されています。外部 LLM のランタイムが `send_to_agent` を呼び出すタイミングを決定します。LLM の視点からは同期ツール呼び出しです。
- **A2A** はピアアドレッシングを中心に設計されています。外部 agent は Reyn agent を独自のカード、capabilities、会話状態を持つ別の自律エンティティとして扱います。ピアは Reyn を「ツール」ではなく「同僚」としてモデル化します。

Reyn にとってこれは主にワイヤーフォーマットの選択であり、基盤となるエンジンは同じです。外部システムがツール呼び出しを持つ LLM であれば MCP を選択してください。外部システム自体が agent であれば A2A を選択してください。

## 参考

- [MCP integration](mcp.md) — 対称的なケース
- [Multi-agent](multi-agent.md) — Reyn 内部の agent トポロジー
