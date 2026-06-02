---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [skill.md]
---

# `graph` セマンティクス

`skill.md` の `graph` フィールドは、許可される Phase トランジションを宣言します。OS はそれを使って LLM が提案するすべてのトランジションを検証します。

## 構造

```yaml
graph:
  outline: [expand]
  expand:  [end]
```

各キーは Phase 名です。各値は許可される次 Phase 名のリストです。特別なトークン `end` は「このトランジションがワークフローを終了する」を意味します。

## 許可される形状

### 線形

```yaml
graph:
  a: [b]
  b: [c]
  c: [end]
```

### 分岐

```yaml
graph:
  triage:  [draft, escalate]
  draft:   [review]
  review:  [revise, end]
  revise:  [review]
  escalate: [end]
```

`triage` の LLM は `draft` または `escalate` を選べます。どちらの選択もグラフに対して検証されます。

### 自己ループはサポートされない

Phase は自身を次の Phase としてリストできません。修正ループは別の Phase を使います（例: `review → revise → review`）。

## 解決ルール

- `entry`（`skill.md` で宣言）は `graph` のキーでなければなりません。
- すべての値リストのエントリーは `graph` のキーまたは `end` のいずれかでなければなりません。
- `end` は `phases/<name>.md` に `can_finish: true` がある Phase からのトランジションにのみ現れることができます。
- サブ Skill ノード（グラフ内の `@sub_skill`）を持つ Skill は同じルールに従います。OS はコンパイル時に埋め込まれた Skill を解決します。

## サブ Skill（グラフノード）

グラフエントリーは `@` を前置することで別の Skill を参照できます:

```yaml
graph:
  prepare:    [@my_subskill]
  '@my_subskill': [aggregate]
  aggregate:  [end]
```

`run_skill` Control IR op は同じ名前解決を使います: `reyn/project/` → `reyn/local/` → `src/reyn/stdlib/skills/`。

## リンターチェック

`reyn lint <skill_name>` が強制します:

- すべてのグラフキーが `phases/` の Phase ファイルに対応している。
- すべてのグラフ値がキー、サブ Skill 参照、または `end` のいずれかである。
- `entry` が存在する。
- `can_finish: true` の Phase が `end` へのパスを持つ。
- 到達不能な Phase がない。

## 関連情報

- [skill-md.md](skill-md.md) — `entry`、`final_output`
- [phase-md.md](phase-md.md) — `can_finish`
- [コンセプト: principles P2（Skill が構造を定義する）](../../concepts/architecture/principles.md#p2-skill-defines-structure)
