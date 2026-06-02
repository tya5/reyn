---
type: reference
topic: dsl
audience: [human, agent]
applies_to: [profile.yaml]
---

# `profile.yaml`

`.reyn/agents/<name>/profile.yaml` に格納される agent ごとのメタデータ。`reyn agent new` で作成され、すべての `reyn chat` 起動時に読み込まれます。

## スキーマ

```yaml
name: researcher                       # 必須（== ディレクトリ名）
role: |                                # 省略可能、デフォルト ""
  Deep technical research, prefers
  primary sources (arxiv, RFCs).
created_at: 2026-05-01T12:00:00+00:00  # ISO-8601 UTC、`reyn agent new` が設定
allowed_skills:                        # 省略可能、デフォルト null（無制限）
  - web_search
  - recall_docs
```

## フィールド

### `name`（文字列、必須）

agent 名。`^[a-z0-9][a-z0-9_-]{0,31}$` に一致し、親ディレクトリ名と等しくなければなりません。`default` agent は予約済みで、最初の `reyn chat` 時に自動作成されます。

### `role`（文字列、デフォルト `""`）

agent の LLM システムプロンプトに `━━━ AGENT ROLE ━━━` ブロックとして注入される自由形式のテキスト。短く、行動的に具体的に書いてください。Skill を変更せずに agent をピアと差別化するのがこれです。

空のロールでも問題ありません。その場合、agent は追加のペルソナなしのジェネラリストとして振る舞います。

### `created_at`（文字列、デフォルト `""`）

`reyn agent new` が実行されたときに設定される ISO-8601 UTC タイムスタンプ。装飾的; ランタイムでは参照されません。

### `allowed_skills`（`list[str]` | `null`、デフォルト `null`）

Skill の allowlist。3 つの状態とそれぞれの意味:

| 値 | 意味 |
|-------|---------|
| なし / `null` | **無制限。** すべてのプロジェクト + stdlib Skill がルーター LLM に提供されます。 |
| `[]`（空リスト） | **ルーターのみ。** Skill の起動は行われません。ルーターは直接返信またはピアへの委任はできます。 |
| `[a, b, c]` | **Allowlist。** リストされた Skill 名のみが提供されます。 |

stdlib ルーター（`skill_router`）、コンパクター（`chat_compactor`）、ナレーター（`skill_narrator`）は**常に**有効であり、このリストの対象外です。これらはシステム Skill であり、agent が選択する Skill ではありません。

二重レイヤーの強制:

1. **ルーター側のフィルター** — `_invoke_router` は LLM がカタログを見る前に `available_skills` を allowlist に絞り込みます。
2. **多層防御** — `_spawn_skill` は起動時に再チェックします。ブロックされた起動はアウトボックスの `error` と `reason="allowlist"` の `skill_spawn_refused` イベントとして現れます。

## 編集

`reyn agent new --role` が新しいプロファイルを書き込みます。その後に `allowed_skills`（または他のフィールド）を変更するには、ファイルを直接編集してください。まだ `reyn agent set-skills` CLI はありません（残課題）。フォーマットは順序や末尾のキーについて寛容です。

## 関連情報

- [リファレンス: agent CLI](../cli/agent.md)
- [コンセプト: multi-agent](../../concepts/multi-agent/multi-agent.md)
- [リファレンス: skill_router](../stdlib/skill_router.md) — `available_skills` が LLM に届く方法
