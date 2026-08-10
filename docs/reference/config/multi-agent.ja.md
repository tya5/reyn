---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# マルチエージェント設定

> **移行案内**: `multi_agent:` トップレベル YAML キーは廃止されました。両設定は `reyn.yaml` の統合 `safety:` ブロックに移動しました。既存の `multi_agent:` エントリーを更新してください:
>
> | 旧（`multi_agent:`） | 新（`safety:`） |
> |---|---|
> | `multi_agent.max_hop_depth` | `safety.loop.max_agent_hops` |
> | `multi_agent.chain_timeout_seconds` | `safety.timeout.chain_seconds` |

動作は変わりません。YAML キーのパスのみ変更されました。

## 現在のスキーマ（`safety:` 配下）

```yaml
safety:
  loop:
    max_agent_hops: 3          # デフォルト: 3
  timeout:
    chain_seconds: 60.0        # デフォルト: 60.0; 0 は無効化
```

完全なスキーマは [リファレンス: `reyn.yaml` — `safety` ブロック](reyn-yaml.md#safety-block) を参照してください。

> **多段は恒久的に退役済み。`max_agent_hops` は消えておらず意味が変わった**
> （architect 裁定 + lead-coder 実測、#3978/#4135、2026-08-10）: 現行の agent 間
> producer である `run_prompt(collect="async")`（proposal 0067 P4e）は、常に単一ホップの
> チェーン（`depth=1`、`|waiting_on| == 1`）だけを登録します — 対象がさらに「委任」して
> チェーンを延長する手段は、以下に記述する退役前モデルと違い存在しません。
> `max_agent_hops` の depth 拒否コードは実際に現役です（`run_prompt(async)` の配送は
> 同じ `InterAgentMessaging.send_to_agent` の depth チェック、`inter_agent_messaging.py`
> の `depth > max_agent_hops` を通ります）— しかし `depth` が定数 `1` になったため、
> この設定はもはやチェーンの深さを制限しません（辿るべきチェーンが無いため）。
> `1` 以上の値（デフォルトの `3` を含む）は常に通過し、`max_agent_hops: 0`
> （下限バリデーションは存在せず `int()` を通すだけ）は `1 > 0` により全ての
> `run_prompt(async)` 呼び出しを拒否します。この設定は今日、深さの上限ではなく
> 実質的に async dispatch の 0/1 スイッチです。`chain_seconds`（下記）は完全に
> 現役で意味も変わっていません: 応答が返らない `run_prompt(async)` 呼び出しは
> 実際にこれでタイムアウトします。

## `safety.loop.max_agent_hops`（整数、デフォルト `3`）

ランタイムがそれ以上の送信を拒否する前に、agent 間メッセージチェーンが何ホップ深くトラバースできるかを制限します。LangGraph の再帰制限に倣っています — 上記コールアウトのとおり退役前の多段モデルから継承されたものです。現行の唯一の producer は常に `depth=1` で dispatch するため、正の値であれば agent 間メッセージングは有効なまま — `0`（またはそれ以下）だけが全呼び出しを拒否して無効化します。

**depth の意味**:

- `depth = 0` — 元のユーザー入力
- `depth = 1` — 最初の agent 間送信（例: `default → researcher`）— `run_prompt(async)` は常にここに登録します
- `depth = 2` — researcher がさらに委任（例: `researcher → archivist`）— 現行の producer はここに到達しません
- `depth = N` — N 番目のホップ

`depth > max_agent_hops` の送信は拒否されます。発信元はアウトボックスに `error` メッセージ（「agent message depth N exceeds limit M; chain refused」）を受け取り、`agent_message_refused` イベントが `reason="max_hop_depth"` で記録されます。上流の保留チェーンは `chain_seconds`（以下参照）が経過するまで登録されたままとなり、その時点で合成されたエラーレスポンスで解決されます。したがって、ツリーの途中でのホップ拒否はハングするのではなくグレースフルに劣化します。

デフォルトの `3` は退役前の多段モデルの `user → A → B → C`（= 3 ホップ）向けに設定されたものです。今日は `1` 以上のどの値も同じ挙動（agent 間メッセージング有効）になります。depth 1 を超えるチェーンを作る producer が現れてから `3` より上げてください。`run_prompt(async)` の dispatch 自体を無効化するには `0` を設定してください。

## `safety.timeout.chain_seconds`（float、デフォルト `60.0`）

保留チェーンのウォールクロックバジェット。`run_prompt(collect="async")` が登録するチェーンは、退役前モデルと同じ形でこの監視タスクを起動します。対象が応答しない場合、`chain_seconds` 後にランタイムは上流に合成エラーレスポンスを生成します:

```
chain timeout: 1 delegate(s) (gamma) did not respond within 60s
```

そして `chain_timeout` イベントを `chain_id`、`waiting_on`、`timeout_seconds`、`origin_agent` と共に発行します。保留チェーンはクリアされ、呼び出し元のループはブロックされなくなります。

`chain_seconds: 0`（または任意の正でない値）を設定すると監視タスクを無効化します。遅い相手が想定されるテストや実験に有用です。無効化されたチェーンは相手が応答しない場合、無限にハングする可能性があります。

デフォルトの `60.0` は退役前の多段モデルの 3 ホップツリー向けに設定されたものです。単一の `run_prompt(async)` 呼び出しは通常より早く解決します。本当に時間がかかる呼び出し（大規模な Web リサーチ、長いコンパクションパス）では増やしてください。より厳しい SLA には下げてください。

## 例

```yaml
safety:
  loop:
    max_agent_hops: 5
  timeout:
    chain_seconds: 120.0
```

## 読み込まれる場所

- `chat/session.py` が `reyn chat` 起動時に `safety.loop.max_agent_hops` と `safety.timeout.chain_seconds` を読み取ります。
- プロセスごとのスコープ。エージェントごとではありません。プロセス内のすべての agent が同じ上限を共有します。

## 検討したが採用しなかったもの

- `topology_policy` — 検討したが、自動管理の `_default` Topology を優先して拒否しました（[コンセプト/topology](../../concepts/multi-agent/topology.md) を参照）。

## 関連情報

- [コンセプト: multi-agent](../../concepts/multi-agent/multi-agent.md)
- [リファレンス: chat CLI](../cli/chat.md)
- [リファレンス: events](../runtime/events.md) — `agent_message_*` イベントは `chain_id` と `depth` を持つ
