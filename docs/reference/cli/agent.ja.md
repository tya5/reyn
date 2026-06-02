---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn agent]
---

# `reyn agent`

永続的な agent を管理します。それぞれ独自のプロファイル、履歴、Memory レイヤー、受信トレイを持つ長命な ChatSession インスタンスです。

自動作成された `default` agent は常に存在します。`reyn agent new` で追加の名前付き agent を作成します。モデルについては [コンセプト/multi-agent](../../concepts/multi-agent/multi-agent.md) を参照してください。

## 概要

```
reyn agent <subcommand> [args]
```

サブコマンド: `list`、`new`、`show`、`rm`。

## `reyn agent list`

すべての既知の agent をアルファベット順に表示します。最終アクティビティのタイムスタンプと各プロファイルの `role` の最初の行も表示されます。

```bash
reyn agent list
```

```
NAME        LAST ACTIVITY     ROLE
default     2026-05-01 13:00  
researcher  2026-05-01 12:55  deep technical research, prefers primary sources
writer      2026-04-30 18:20  concise long-form prose
```

## `reyn agent new <name> [--role TEXT]`

`.reyn/agents/<name>/` 配下に新しい agent を作成します。ディレクトリは `profile.yaml` でプロビジョニングされます。`history.jsonl`、`events.jsonl`、`memory/`、`runs/` は最初のアクティビティ時に作成されます。

```bash
reyn agent new researcher --role "deep technical research, prefers primary sources"
```

`<name>` は agent 名の正規表現に一致する必要があります: `[a-z0-9]` で始まる `[a-z0-9_-]` の 1〜32 文字。

`--role` テキストは agent の LLM システムプロンプトに注入されます。短く具体的に書いてください。作成後に `allowed_skills` や他のプロファイルフィールドを設定するには、`profile.yaml` を直接編集してください。[profile-yaml リファレンス](../dsl/profile-yaml.md) を参照してください。

## `reyn agent show <name>`

プロファイルメタデータと解決されたフィールドを表示します:

```bash
reyn agent show researcher
```

```
name:        researcher
created_at:  2026-05-01T12:00:00+00:00
workspace:   /path/to/project/.reyn/agents/researcher
allowed_skills: (unrestricted — all project + stdlib skills)
role:
  deep technical research, prefers primary sources
```

`allowed_skills` は以下のいずれかで表示されます:

- `(unrestricted — all project + stdlib skills)` — フィールドなし / `null`
- `(none — router-only, no skill spawn)` — 空リスト `[]`
- 箇条書きリスト — 設定された allowlist

## `reyn agent rm <name> [--yes]`

agent のディレクトリを再帰的に削除します（履歴、イベント、Memory レイヤー、runs）。この agent をメンバーとしてリストしているすべてのユーザー宣言 Topology にカスケードします。削除される agent がリーダーの team Topology は完全に削除されます。

```bash
reyn agent rm researcher --yes
```

`default` agent は削除できません。

## Workspace レイアウト

各 agent は `.reyn/agents/<name>/` を所有します:

| パス | 目的 |
|------|---------|
| `profile.yaml` | 名前 / ロール / created_at / allowed_skills |
| `history.jsonl` | 追記専用の会話 + agent メッセージログ |
| `events.jsonl` | ランタイムイベントの監査ログ |
| `memory/MEMORY.md` + body ファイル | agent スコープの Memory レイヤー |
| `runs/<run_id>/` | Skill スポーンごとの Workspace |

## 関連情報

- [リファレンス: profile-yaml](../dsl/profile-yaml.md)
- [リファレンス: chat CLI](chat.md)
- [リファレンス: topology CLI](topology.md)
- [コンセプト: multi-agent](../../concepts/multi-agent/multi-agent.md)
