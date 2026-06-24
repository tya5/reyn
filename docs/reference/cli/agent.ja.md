---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn agent]
---

# `reyn agent`

永続的な agent を管理します。それぞれ独自のプロファイル、履歴、Memory レイヤー、受信トレイを持つ長命な Session インスタンスです。

自動作成された `default` agent は常に存在します。`reyn agent new` で追加の名前付き agent を作成します。モデルについては [コンセプト/multi-agent](../../concepts/multi-agent/multi-agent.md) を参照してください。

## 概要

```
reyn agent <subcommand> [args]
```

サブコマンド: `list`、`new`、`show`、`rm`。

## `reyn agent list`

すべての**アクティブな**（アーカイブ済みでない）agent をアルファベット順に表示します。最終アクティビティのタイムスタンプと各プロファイルの `role` の最初の行も表示されます。アーカイブ済みの agent はこの一覧に表示されません。`--all` を渡すとアーカイブ済み agent も `<name> (archived)` の形式で表示され、確認・復元・パージできます。

```bash
reyn agent list          # アクティブな agent のみ
reyn agent list --all    # アーカイブ済み agent も表示
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

## `reyn agent rm <name> [--purge] [--yes]`

デフォルトでは agent を**アーカイブ**します（ソフトデリート — データは保持され、破棄されません）。`--purge` を指定すると agent ディレクトリとすべての rewind 履歴を永続的に破棄するハードデリートになります。

```bash
reyn agent rm researcher            # アーカイブ（確認プロンプトあり）
reyn agent rm researcher --yes      # アーカイブ、プロンプトをスキップ
reyn agent rm researcher --purge    # ハードデリート（確認プロンプトあり、不可逆）
reyn agent rm researcher --purge --yes
```

`default` agent は削除できません。

### アーカイブ（デフォルト）

agent の `.reyn/agents/<name>/` ディレクトリは**そのまま保持**されます — データは破棄されません。これが `--purge` との主な違いです:

- **PITR 世代が保持**されます: WAL 由来のチェックポイント履歴が残るため、データは回復可能です。
- **Topology メンバーシップが保持**されます: カスケードは発火しません。agent のチーム / ネットワーク所属は削除されません。
- アーカイブ WAL seq を記録したトゥームストーンマーカーが書き込まれます（WAL ウィンドウ GC のヒンジ）。
- agent は**アクティブなサーフェスから非表示**になります: `reyn agent list`、TUI Agents タブ、デフォルトトポロジールーティング、A2A `can_send` チェックのいずれもアーカイブ済み agent をスキップします。データが残っているだけで、破棄はされていません。

**WAL ウィンドウ自動パージ**: WAL 保持ウィンドウがアーカイブ seq を超えると、アーカイブ済み agent のディレクトリは自動的にハードデリートされます（ソフトデリートが rewind ウィンドウを外れたため、データはもはや回復不可能）。この時点で Topology カスケードが発火し、全 Topology から agent が削除されます。

### パージ (`--purge`)

`.reyn/agents/<name>/` をただちにハードデリートし、すべての PITR 世代を破棄します。パージ前へのタイムトラベルは意図的に非サポートです。Topology カスケードがただちに発火します（全 Topology から agent が削除され、リーダーがパージされた team Topology は完全に削除されます）。

復元ウィンドウが不要な場合、クリーンな完全削除として `--purge` を使用します。

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
- [コンセプト: time-travel](../../concepts/runtime/time-travel.md) — rewind + PITR メカニクス
