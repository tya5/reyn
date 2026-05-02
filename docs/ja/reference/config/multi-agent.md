---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# `multi_agent` 設定

`reyn.yaml` のトップレベルブロックで、agent 間メッセージング動作を制御します。

## スキーマ

```yaml
multi_agent:
  max_hop_depth: 3              # デフォルト: 3
  chain_timeout_seconds: 60.0   # デフォルト: 60.0; 0 は無効化
```

## `max_hop_depth`（整数、デフォルト `3`）

ランタイムがそれ以上の送信を拒否する前に、agent 間メッセージチェーンが何ホップ深くトラバースできるかを制限します。LangGraph の再帰制限に倣っています。

**depth の意味**:

- `depth = 0` — 元のユーザー入力
- `depth = 1` — 最初の agent 間送信（例: `default → researcher`）
- `depth = 2` — researcher がさらに委任（例: `researcher → archivist`）
- `depth = N` — N 番目のホップ

`depth > max_hop_depth` の送信は拒否されます。発信元はアウトボックスに `error` メッセージ（「agent message depth N exceeds limit M; chain refused」）を受け取り、`agent_message_refused` イベントが `reason="max_hop_depth"` で記録されます。上流の保留チェーンは `chain_timeout_seconds`（以下参照）が経過するまで登録されたままとなり、その時点で合成されたエラーレスポンスで解決されます。したがって、ツリーの途中でのホップ拒否はハングするのではなくグレースフルに劣化します。

デフォルトの `3` は `user → A → B → C`（= 3 ホップ）を許可しますが、`user → A → B → C → D` は停止します。深い階層 Topology（例: 重複するチームとして表現された 5 レベルのツリー）では増やしてください。

## `chain_timeout_seconds`（float、デフォルト `60.0`）

委任 agent の保留チェーンのウォールクロックバジェット。ルーターの決定が `messages_to_agents` を出力すると、ランタイムは `chain_id` をキーとする `_PendingChain` を登録し、監視タスクを起動します。すべてのデリゲートが応答すればチェーンが解決したときに監視タスクはキャンセルされます。そうでなければ、`chain_timeout_seconds` 後にランタイムは上流に合成エラーレスポンスを生成します:

```
chain timeout: 1 delegate(s) (gamma) did not respond within 60s
```

そして `chain_timeout` イベントを `chain_id`、`waiting_on`、`timeout_seconds`、`origin_agent` と共に発行します。保留チェーンはクリアされ、上流 agent のループはブロックされなくなります。

`chain_timeout_seconds: 0`（または任意の正でない値）を設定すると監視タスクを無効化します。遅いデリゲートが想定されるテストや実験に有用です。無効化されたチェーンはデリゲートが応答しない場合、無限にハングする可能性があります。

デフォルトの `60.0` は妥協点です: 大半のチェーンは light/strong モデルを使った典型的な 3 ホップツリーで 10〜30 秒で完了します。本当に時間がかかる Skill チェーン（大規模な Web リサーチの fan-out、長いコンパクションパス）では増やしてください。より厳しい SLA には下げてください。

## 例

```yaml
multi_agent:
  max_hop_depth: 5
  chain_timeout_seconds: 120.0
```

## 読み込まれる場所

- `cli/commands/chat.py` が `reyn chat` 起動時に両フィールドを読み取り、`max_hop_depth=` と `chain_timeout_seconds=` として `ChatSession.__init__` に渡します。
- プロセスごとのスコープ。エージェントごとではありません。プロセス内のすべての agent が同じ上限を共有します。

## 検討したが採用しなかったもの

- `topology_policy` — 検討したが、自動管理の `_default` Topology を優先して拒否しました（[コンセプト/topology](../../concepts/topology.md) を参照）。

## 関連情報

- [コンセプト: multi-agent](../../concepts/multi-agent.md)
- [リファレンス: chat CLI](../cli/chat.md)
- [リファレンス: events](../runtime/events.md) — `agent_message_*` イベントは `chain_id` と `depth` を持つ
