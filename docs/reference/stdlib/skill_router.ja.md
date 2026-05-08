---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [skill_router]
---

# `skill_router`

ユーザー（またはピア agent）の発話を、適切な Skill・agent・直接返答へルーティングします。`reyn chat` が毎ターン使用します。

## Phase

router は **2 Phase** のワークフローとして動作します:

1. **`classify`** — インテント（chitchat / memory_recall / stable_knowledge / clarification / task / fresh_lookup）を判定し、`routing_decision` で終了するか `match` へ引き渡します。
2. **`match`** — `task` および `fresh_lookup` インテントに対し、特定の Skill（またはピア agent）へディスパッチするか、`web_research` へ遷移します。

classify Phase は**メモリ書き込み**も担当します。毎ターン新しい発話を検査し、ユーザー／フィードバック／プロジェクト／参照情報を永続化するために `file/write` op を発行することがあります。メモリの読み込みは ChatSession が事前にマージします（[concepts/memory](../../concepts/memory.md) を参照）。

## エントリー artifact: `chat_routing_request`

ChatSession が毎ターン構築します。主なフィールド:

| フィールド | 出所 | 説明 |
|-----------|------|------|
| `user_message` | inbox payload | ルーティング対象の最新発話 |
| `chat_id` | session | agent 自身の名前（classify がメモリパス構築時に使用） |
| `history_path` | session | `history.jsonl` へのパス。classify preprocessor がスライスします |
| `compaction` | config | ヒストリースライスの head/tail サイズ |
| `available_skills` | session | プロジェクト + stdlib カタログ（router/compactor/narrator を除外）。`profile.allowed_skills` が設定されている場合はフィルタリング済み |
| `available_agents` | registry | Topology ルールで到達可能な他の agent — `[{name, role}, ...]` |
| `memory_index` | session | 共有 + agent レイヤーをマージ済み（`{status, content}`）。`content` は `(shared)` と `(agent: <name>)` セクションを含む Markdown |

## 最終出力: `routing_decision`

| フィールド | 型 | 役割 |
|-----------|---|------|
| `reply_text` | string（任意） | ユーザーに直接表示するテキスト — 中間通知または最終回答 |
| `skills_to_run` | array（任意） | 今ターン実行するプロジェクト / stdlib Skill |
| `messages_to_agents` | array（任意） | ピア agent へのデリゲーション — `[{to, request}, ...]` |

ChatSession は空でない配列をそれぞれディスパッチします。`reply_text` はユーザー起点のチェーンでは即座にユーザーへ届きます。agent 起点のチェーン（ピア agent がこのリクエストを受信した場合）では、`messages_to_agents` が空でないとき router の `reply_text` は全デリゲートが応答するまで保留される [deferred reply](../../concepts/multi-agent.md#deferred-reply) に入ります。

## Skill 選択ガイダンス

`match` Phase が優先する選択:

- **特定の Skill** — `routing.examples.positive` が明確に一致する場合。明確に定義されたタスクに適しています。
- **agent デリゲーション** — `available_agents` にリクエストに合う `role` を持つピアがいる場合。
- **直接返答** — どちらにも当てはまらずリクエストが小さい場合（または `confidence < 0.6` で clarification が発火する場合）。

同じ decision で Skill と agent の両方を選ぶことはできません — どちらか一方のブランチを選択してください。

## ソース

[`src/stdlib/skills/skill_router/`](https://github.com/tya5/reyn/blob/main/src/stdlib/skills/skill_router/)

## 関連情報

- [コンセプト: memory](../../concepts/memory.md) — 2 層の読み書きコントラクト
- [コンセプト: multi-agent](../../concepts/multi-agent.md) — `messages_to_agents` とチェーンのセマンティクス
- [リファレンス: profile-yaml](../dsl/profile-yaml.md) — `allowed_skills` フィルター
- [リファレンス: chat CLI](../cli/chat.md)
