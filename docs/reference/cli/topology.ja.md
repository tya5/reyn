---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn topology]
---

# `reyn topology`

agent の通信 Topology を管理します。どの agent がどれに送信できるかを制限する宣言的な構造です。

3 種類がサポートされています: `network`（完全グラフ）、`team`（リーダー中心のスター）、`pipeline`（有向パス）。自動管理の `_default` network は、ユーザー宣言の Topology に属さないすべての agent をカバーします。空の状態では自由に動作しますが、宣言された Topology はそのルールを即座に強制します。モデルについては [コンセプト/topology](../../concepts/topology.md) を参照してください。

## 概要

```
reyn topology <subcommand> [args]
```

サブコマンド: `list`、`new`、`show`、`rm`、`add-member`、`rm-member`。

## `reyn topology list`

ユーザー宣言の Topology を先に（アルファベット順）、`_default` を最後に表示します。

```bash
reyn topology list
```

```
NAME      KIND      MEMBERS
team1     team      default*, alpha
_default  network   beta, gamma
```

`*` は `team` 種別のリーダーを示します。`_default` は自動管理されます。メンバーシップ = 他のどの Topology にも属さないすべての agent。

## `reyn topology new <name> --kind KIND --members A,B,C [--leader LEADER]`

`.reyn/topologies/<name>.yaml` 配下にユーザー宣言の Topology を作成します。

```bash
reyn topology new team_research --kind team \
    --members default,researcher,writer --leader default

reyn topology new pipe_publish --kind pipeline \
    --members researcher,editor,publisher
```

バリデーション:

- `<name>` は `[a-z0-9][a-z0-9_-]{0,31}` に一致し、`default` ではない（予約済み）。`_` プレフィックスは正規表現で拒否されるので `_default` も作成できません。
- `--members` の agent はすでに存在している必要があります（`reyn agent list`）。
- `--kind team` は `--leader` が必要で、リーダーは `--members` に含まれる必要があります。
- `--kind pipeline` は `--members` の順序が重要です: エッジは `members[i] → members[i+1]` のみ流れます。
- `--kind network` は任意の順序を受け入れます。すべてのメンバーペアが両方向で許可されます。

## `reyn topology show <name>`

Topology と許可される有向エッジの完全なセットを表示します:

```bash
reyn topology show team_research
```

```
name:        team_research
kind:        team
leader:      default
members:     default*, researcher, writer
created_at:  2026-05-01T12:00:00+00:00

permitted edges (4):
  default → researcher
  default → writer
  researcher → default
  writer → default
```

`reyn topology show _default` も機能し、自動管理として注釈されます。

## `reyn topology rm <name> [--yes]`

ユーザー宣言の Topology を削除します。`_default` は削除できません。試みると明確なエラーが表示されます。

```bash
reyn topology rm team_research --yes
```

## `reyn topology add-member <topology> <agent>`

`agent` を `members` に追加します。`pipeline` 種別では、新しいメンバーが新しいテールになります。

```bash
reyn topology add-member team_research editor
```

`_default` を直接変更しようとすると拒否されます。そのメンバーシップは「他のどの Topology にも属さない agent」から計算され、ユーザー宣言の Topology が変わると自動的に調整されます。

## `reyn topology rm-member <topology> <agent>`

`agent` を `members` から削除します。`team` 種別では、リーダーの削除は拒否されます（代わりに Topology を削除してください）。削除後、その agent がユーザー宣言の Topology に属さなくなった場合、`_default` に戻ります。

```bash
reyn topology rm-member team_research editor
```

## `reyn agent rm` からのカスケード

`reyn agent rm` で agent を削除すると、それがメンバーだったすべての Topology から自動的に除外されます。リーダーが削除された `team` は完全に削除されます。空になった Topology も削除されます。

## 関連情報

- [コンセプト: topology](../../concepts/topology.md) — kind のセマンティクス、`_default`、許可ルール
- [リファレンス: topology-yaml](../dsl/topology-yaml.md) — ディスク上のスキーマ
- [リファレンス: agent CLI](agent.md)
