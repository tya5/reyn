---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [topology.yaml]
---

# `topology.yaml`

`.reyn/topologies/<name>.yaml` に宣言された通信 Topology。`reyn topology new` で作成されます。すべてのプロセス起動時に `AgentRegistry` が読み込みます。

自動管理の `_default` network Topology はディスクに格納されません。メモリ上のみに存在し、「ユーザー宣言の Topology に含まれない agent」から計算されます。[コンセプト/topology](../../concepts/multi-agent/topology.md) を参照してください。

## スキーマ

```yaml
name: team_research                       # 必須
kind: team                                # 必須: "network" | "team" | "pipeline"
members:                                  # 必須、kind=pipeline では順序が重要
  - default
  - researcher
  - writer
leader: default                           # kind=team では必須、members に含まれる必要がある
created_at: 2026-05-01T12:00:00+00:00     # ISO-8601 UTC、`reyn topology new` が設定
```

## フィールド

### `name`（文字列、必須）

Topology 名。`^[a-z0-9][a-z0-9_-]{0,31}$` に一致しなければなりません。`default` と `_default` は予約済みです。

### `kind`（文字列、必須）

以下のいずれか:

- `network` — `members` 間の完全グラフ。`can_send(A, B) = (A != B and A,B ∈ members)`。
- `team` — `leader` を中心とするスター型。`can_send(A, B) = (leader ∈ {A, B} and A != B and A,B ∈ members)`。ピア間通信（リーダーでないメンバー同士）は禁止。
- `pipeline` — 有向パス。`can_send(A, B) = members.index(B) == members.index(A) + 1`。ジャンプ不可、逆方向不可、fan-out 不可。

`tree`、`meeting`、`pair`、`broadcast` の種類は**実装されていません**。`tree` は重複する `team` Topology として表現できます（[コンセプト/topology](../../concepts/multi-agent/topology.md#tree-pattern) を参照）。その他はニーズ待ちの残課題です。

### `members`（文字列のリスト、必須）

参加する agent の名前。`kind: pipeline` では順序が**重要**（有向パスを定義）。`kind: network` と `kind: team` では情報提供目的。各名前は `reyn topology new` / `add-member` 時に既存の agent を参照する必要があります。`reyn agent rm` でのカスケードが参照を自動的に削除します。

`team` は `leader` に一致するメンバーが少なくとも 1 人必要です。`pipeline` は重複するメンバーを拒否します（サイクルが生まれます）。`network` は空の `members` リストを拒否します（許可されるエッジがない）。

### `leader`（文字列、`kind: team` では必須）

チームのリーダーの agent 名。`members` に含まれる必要があります。`kind: network` や `kind: pipeline` では設定してはなりません。

### `created_at`（文字列、デフォルト `""`）

`reyn topology new` が実行されたときに設定される ISO-8601 UTC タイムスタンプ。装飾的。

## 許可ルール（レジストリレベル）

レジストリの `permit(A, B)` は `A` と `B` の両方を `members` として含むすべての Topology をチェックし、そのどれかが `can_send` でエッジを許可すれば True を返します。許可的なフォールバックはありません。`A` と `B` が Topology を共有しない場合（`_default` を含む）、エッジは拒否されます。

`_default` は空の状態をエルゴノミックにするために存在します: ユーザー Topology に agent が含まれなくなった瞬間、その agent は `_default` に再参加し、他の無所属ピアと再び自由に通信できます。

## 変更のカスケード

- `reyn agent rm <name>` はすべての Topology の `members` から `<name>` を削除します。
- リーダーが削除された `team` Topology は完全に削除されます。
- `members` が空になった Topology も削除されます。

## 関連情報

- [コンセプト: topology](../../concepts/multi-agent/topology.md)
- [リファレンス: topology CLI](../cli/topology.md)
- [リファレンス: profile-yaml](profile-yaml.md)
